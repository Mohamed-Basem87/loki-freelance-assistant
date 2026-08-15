import asyncio
from types import SimpleNamespace

import app.handlers.telegram as telegram


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, channel, min_id, reverse, limit):
        for message in self.messages:
            yield message


def test_recovery_does_not_advance_past_failed_message(monkeypatch):
    messages = [
        SimpleNamespace(id=101, chat_id=-1001),
        SimpleNamespace(id=102, chat_id=-1001),
        SimpleNamespace(id=103, chat_id=-1001),
    ]
    processed = []
    watermarks = []

    monkeypatch.setattr(telegram.state, "get_last_message_id", lambda _: 100)

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        processed.append(message.id)
        return message.id != 102

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    asyncio.run(telegram._recover_channel(_FakeClient(messages), "test-channel"))

    assert processed == [101, 102]
    assert watermarks == [(-1001, 101)]


def test_recovery_advances_through_successful_messages(monkeypatch):
    messages = [
        SimpleNamespace(id=201, chat_id=-1002),
        SimpleNamespace(id=202, chat_id=-1002),
    ]
    watermarks = []

    monkeypatch.setattr(telegram.state, "get_last_message_id", lambda _: 200)

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    asyncio.run(telegram._recover_channel(_FakeClient(messages), "test-channel"))

    assert watermarks == [(-1002, 201), (-1002, 202)]


def test_live_handler_serializes_watermark_advance_per_channel(monkeypatch):
    """
    H-1 regression: message N (slow, e.g. awaiting Gemini) and
    message N+1 (fast, e.g. an instant hard_reject) arrive on the
    same channel almost simultaneously and are dispatched as separate
    tasks, mirroring Telethon's real behavior. Without per-channel
    serialization, N+1 finishes first and advances the watermark past
    N even though N hasn't finished (or could still fail). This test
    fires both concurrently and asserts the watermark only ever
    advances in arrival order.
    """
    from collections import defaultdict

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(event):
        # Message 301 ("N") is slow (simulates an in-flight Gemini
        # call); message 302 ("N+1") is instant (simulates a clean
        # hard_reject). Without locking, 302 would finish first.
        if event.id == 301:
            await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    async def run():
        channel_locks = defaultdict(asyncio.Lock)

        event_n = SimpleNamespace(id=301, chat_id=-1003)
        event_n_plus_1 = SimpleNamespace(id=302, chat_id=-1003)

        # Dispatch both as independent tasks, N first, matching how
        # Telethon fires NewMessage handlers.
        task_n = asyncio.create_task(
            telegram._handle_live_message(event_n, channel_locks)
        )
        await asyncio.sleep(0)  # let task_n start and acquire the lock
        task_n_plus_1 = asyncio.create_task(
            telegram._handle_live_message(event_n_plus_1, channel_locks)
        )

        await asyncio.gather(task_n, task_n_plus_1)

    asyncio.run(run())

    # The watermark must advance strictly in arrival order: 301
    # before 302, never the reverse.
    assert watermarks == [(-1003, 301), (-1003, 302)]


def test_live_handler_does_not_serialize_across_different_channels(monkeypatch):
    """
    The per-channel lock must not become a global lock: messages on
    different channels should still be able to interleave/complete
    out of order relative to each other.
    """
    from collections import defaultdict

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(event):
        if event.chat_id == -2001:
            await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    async def run():
        channel_locks = defaultdict(asyncio.Lock)

        slow_event = SimpleNamespace(id=401, chat_id=-2001)
        fast_event = SimpleNamespace(id=501, chat_id=-2002)

        task_slow = asyncio.create_task(
            telegram._handle_live_message(slow_event, channel_locks)
        )
        task_fast = asyncio.create_task(
            telegram._handle_live_message(fast_event, channel_locks)
        )

        await asyncio.gather(task_slow, task_fast)

    asyncio.run(run())

    # The fast channel's message should be able to finish (and
    # advance its own watermark) before the slow channel's, since
    # they're on different channels and shouldn't block each other.
    assert watermarks[0] == (-2002, 501)
    assert watermarks[1] == (-2001, 401)
