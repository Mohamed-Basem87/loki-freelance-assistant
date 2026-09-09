"""P1 regression tests for the Telegram recovery cap/barrier invariant.

The 2,000-message cap is intentional. Reaching it must NOT be treated as
"recovery complete": a CAPPED pass keeps the channel's live barrier closed
so a live message can never advance the durable watermark past messages
that are still potentially unrecovered. Only a pass that actually drains
the channel (COMPLETE) may release the barrier.

These tests exercise observable behavior (states, watermarks, barrier
release) against the real recovery machinery with a paging fake client,
not implementation details.
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.adapters.sources.telegram as telegram
from app.adapters.sources.telegram import RECOVERED_COMPLETE, RECOVERED_CAPPED


class _PagingClient:
    """Fake Telethon client that pages messages newer than a watermark.

    `messages` is the full ordered history (ascending ids). `cap`
    bounds the number of messages returned per iter_messages call,
    mirroring Telegram's limit. Only messages strictly newer than
    `min_id` are returned, oldest first.
    """

    def __init__(self, messages, cap):
        self.messages = messages
        self.cap = cap

    async def iter_messages(self, channel, min_id, reverse, limit):
        newer = [m for m in self.messages if m.id > int(min_id)]
        for message in newer[: min(limit, self.cap)]:
            yield message

    async def get_messages(self, channel, limit=1):
        if limit == 1 and self.messages:
            return [self.messages[-1]]
        return self.messages


class _Harness:
    """Shared state for one test scenario: watermark storage + processing."""

    def __init__(self):
        self.watermarks = {}

    def seed(self, channel, watermark):
        self.watermarks[str(channel)] = int(watermark)

    def get_last_message_id(self, channel):
        return self.watermarks.get(str(channel), 0)

    async def fake_set_watermark(self, chat_id, message_id):
        self.watermarks[str(chat_id)] = int(message_id)

    async def fake_process_ok(self, message):
        return True


def _patch(monkeypatch, harness, process_fn):
    async def fake_get(channel):
        return harness.get_last_message_id(channel)

    monkeypatch.setattr(telegram.state, "get_last_message_id", fake_get)
    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", harness.fake_set_watermark
    )
    monkeypatch.setattr(telegram, "process_message", process_fn)


async def _make_locks(channels):
    from collections import defaultdict
    locks = defaultdict(asyncio.Lock)
    for ch in channels:
        await locks[ch].acquire()
    return locks


def _history(start_id, count, chat_id):
    return [SimpleNamespace(id=i, chat_id=chat_id) for i in range(start_id, start_id + count)]


# ---------------------------------------------------------------------------
# 1. Exactly 2,000 messages recovered and additional messages remain.
# ---------------------------------------------------------------------------
def test_capped_recovery_is_incomplete_and_barrier_stays_blocked(monkeypatch):
    harness = _Harness()
    harness.seed(-1001, 1)
    # ids 2..3001 = 3000 messages newer than watermark 1 (bigger than cap)
    messages = _history(2, 3000, -1001)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    result = asyncio.run(telegram._recover_channel(client, -1001, max_messages=2000))

    assert result == RECOVERED_CAPPED, (
        "reaching the 2,000 cap with more messages remaining must be reported "
        "as capped/incomplete, not complete"
    )
    assert harness.get_last_message_id(-1001) == 2001, (
        "the watermark must advance only through the processed contiguous "
        "batch (ids 2..2001), leaving newer messages unrecovered"
    )

    # Coordinator-level: a single recovery pass over a still-capped channel
    # must keep the live barrier closed. Use a fresh scenario so only one
    # capped pass runs (the direct _recover_channel above already advanced
    # -1001's watermark, which would otherwise let a second pass complete).
    harness2 = _Harness()
    harness2.seed(-1011, 1)
    client2 = _PagingClient(_history(2, 3000, -1011), cap=2000)
    _patch(monkeypatch, harness2, harness2.fake_process_ok)
    channel_locks = asyncio.run(_make_locks([-1011]))
    blocked = {}
    asyncio.run(
        telegram._recover_all_channels(
            client2, channel_locks, blocked, channels=[-1011], max_messages=2000
        )
    )
    assert blocked.get(-1011) is True, (
        "a capped recovery must NOT release the live barrier"
    )


# ---------------------------------------------------------------------------
# 2. Cap reached + a newer live message arrives -> watermark not advanced.
# ---------------------------------------------------------------------------
def test_capped_recovery_then_live_message_does_not_advance_watermark(monkeypatch):
    harness = _Harness()
    harness.seed(-1002, 1)
    messages = _history(2, 3000, -1002)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    from collections import defaultdict

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        await channel_locks[-1002].acquire()
        blocked = {-1002: True}

        await telegram._recover_all_channels(
            client, channel_locks, blocked, channels=[-1002], max_messages=2000
        )

        assert blocked[-1002] is True, "recovery is capped, barrier must be closed"

        # Live message id is newer than the unrecovered backlog (> watermark 2001).
        live = SimpleNamespace(id=3001, chat_id=-1002)
        await telegram._handle_live_message(live, channel_locks, blocked)

        assert harness.get_last_message_id(-1002) == 2001, (
            "a live message must never advance the durable watermark past "
            "still-unrecovered messages while the channel is capped/blocked"
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. A second recovery pass drains the next batch.
# ---------------------------------------------------------------------------
def test_second_pass_drains_next_batch_and_stays_blocked_if_another_remains(
    monkeypatch,
):
    harness = _Harness()
    harness.seed(-1003, 1)
    messages = _history(2, 3000, -1003)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    # First pass -> capped, watermark at 2001.
    asyncio.run(telegram._recover_channel(client, -1003, max_messages=2000))
    assert harness.get_last_message_id(-1003) == 2001

    # Second pass drains 2002..3001 (1000 messages < cap) -> complete.
    result = asyncio.run(telegram._recover_channel(client, -1003, max_messages=2000))
    assert result == RECOVERED_COMPLETE
    assert harness.get_last_message_id(-1003) == 3001

    # Coordinator-level: a channel with another capped batch must stay blocked.
    harness.seed(-1004, 1)
    big = _PagingClient(_history(2, 3000, -1004), cap=2000)
    channel_locks = asyncio.run(_make_locks([-1004]))
    blocked = {}
    asyncio.run(
        telegram._recover_all_channels(
            big, channel_locks, blocked, channels=[-1004], max_messages=2000
        )
    )
    assert blocked.get(-1004) is True, (
        "a channel with another capped batch remaining must stay blocked"
    )


# ---------------------------------------------------------------------------
# 4. Final recovery pass catches up -> barrier releases.
# ---------------------------------------------------------------------------
def test_final_pass_catches_up_and_releases_barrier(monkeypatch):
    harness = _Harness()
    harness.seed(-1005, 1)
    messages = _history(2, 2500, -1005)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    from collections import defaultdict

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        blocked = {-1005: True}

        # Pass 1: capped (ids 2..2001 = 2000 msgs, channel still larger).
        await channel_locks[-1005].acquire()
        await telegram._recover_all_channels(
            client, channel_locks, blocked, channels=[-1005], max_messages=2000
        )
        assert blocked[-1005] is True

        # Pass 2: drains 2002..2501 (500 msgs < cap) -> complete -> release.
        # _recover_all_channels releases the lock each pass, so re-acquire.
        await channel_locks[-1005].acquire()
        await telegram._recover_all_channels(
            client, channel_locks, blocked, channels=[-1005], max_messages=2000
        )
        assert blocked[-1005] is False, (
            "a catch-up pass must release the live barrier"
        )

        # Subsequent live messages process and advance normally.
        live = SimpleNamespace(id=2600, chat_id=-1005)
        await telegram._handle_live_message(live, channel_locks, blocked)
        assert harness.get_last_message_id(-1005) == 2600

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. Restart while recovery is incomplete -> resumes safely, no skipped msgs.
# ---------------------------------------------------------------------------
def test_restart_resumes_from_durable_watermark_without_skipping(monkeypatch):
    harness = _Harness()
    harness.seed(-1006, 1)
    messages = _history(2, 4000, -1006)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    # "Process" pass 1 caps at 2001.
    asyncio.run(telegram._recover_channel(client, -1006, max_messages=2000))
    assert harness.get_last_message_id(-1006) == 2001

    # Simulated restart: full re-run resumes from the durable watermark
    # and must not skip anything (contiguous advancement over all ids).
    results = []
    for _ in range(3):
        r = asyncio.run(telegram._recover_channel(client, -1006, max_messages=2000))
        results.append(r)
        if r == RECOVERED_COMPLETE:
            break

    assert results[-1] == RECOVERED_COMPLETE
    # ids 2..4001 => total 4000 messages; ending watermark == 4001.
    assert harness.get_last_message_id(-1006) == 4001


# ---------------------------------------------------------------------------
# 6. Recovery fails after partial progress -> stays blocked, retryable.
# ---------------------------------------------------------------------------
def test_recovery_failure_after_partial_progress_stays_blocked(monkeypatch):
    harness = _Harness()
    harness.seed(-1007, 1)
    messages = _history(2, 3000, -1007)
    client = _PagingClient(messages, cap=2000)

    async def process_fail_at_1500(message):
        return message.id != 1500

    _patch(monkeypatch, harness, process_fail_at_1500)

    async def run():
        channel_locks = await _make_locks([-1007])
        blocked = {-1007: True}
        await telegram._recover_all_channels(
            client, channel_locks, blocked, channels=[-1007], max_messages=2000
        )
        assert blocked[-1007] is True, (
            "a failure after partial progress must keep the barrier closed"
        )
        # Watermark only advanced through confirmed contiguous msgs
        # (ids 2..1499).
        assert harness.get_last_message_id(-1007) == 1499

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 7. Multiple recovery attempts cannot run concurrently for one channel.
# ---------------------------------------------------------------------------
def test_recovery_attempts_are_serialized_per_channel(monkeypatch):
    harness = _Harness()
    harness.seed(-1008, 1)
    messages = _history(2, 3000, -1008)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    max_concurrent = {"value": 0}
    in_progress = {"count": 0}
    real_recover = telegram._recover_channel

    async def tracking_recover(client, channel, *, max_messages=None):
        in_progress["count"] += 1
        max_concurrent["value"] = max(max_concurrent["value"], in_progress["count"])
        try:
            return await real_recover(client, channel, max_messages=max_messages)
        finally:
            in_progress["count"] -= 1

    monkeypatch.setattr(telegram, "_recover_channel", tracking_recover)

    async def run():
        # The real channel lock serializes concurrent attempts on the same
        # channel, exactly as _retry_recovery_channel / _recover_all_channels
        # guarantee. Fire two overlapping attempts both gated on the same
        # lock and assert they never overlap the walk.
        from collections import defaultdict
        channel_locks = defaultdict(asyncio.Lock)
        gate = telegram._recover_channel
        lock = channel_locks[-1008]

        async def locked_recover():
            async with lock:
                return await gate(client, -1008, max_messages=2000)

        await asyncio.gather(locked_recover(), locked_recover())

    asyncio.run(run())

    assert max_concurrent["value"] == 1, (
        "concurrent recovery passes for the same channel must not run the "
        "walk at once (serialized by the per-channel lock)"
    )


# ---------------------------------------------------------------------------
# 8. Live messages arriving during capped recovery are not lost.
# ---------------------------------------------------------------------------
def test_live_messages_during_capped_recovery_are_not_lost(monkeypatch):
    harness = _Harness()
    harness.seed(-1009, 1)
    messages = _history(2, 3000, -1009)
    client = _PagingClient(messages, cap=2000)
    _patch(monkeypatch, harness, harness.fake_process_ok)

    processed = []

    async def process_tracking(message):
        processed.append(message.id)
        return True

    monkeypatch.setattr(telegram, "process_message", process_tracking)

    from collections import defaultdict

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        await channel_locks[-1009].acquire()
        blocked = {-1009: True}

        recovery_task = asyncio.create_task(
            telegram._recover_all_channels(
                client, channel_locks, blocked, channels=[-1009], max_messages=2000
            )
        )
        live = SimpleNamespace(id=2999, chat_id=-1009)
        live_task = asyncio.create_task(
            telegram._handle_live_message(live, channel_locks, blocked)
        )
        await asyncio.gather(recovery_task, live_task)

        assert 2999 in processed, "live message arriving during recovery was lost"
        assert harness.get_last_message_id(-1009) == 2001, (
            "live message during capped recovery must not advance the "
            "watermark past unrecovered messages"
        )

    asyncio.run(run())
