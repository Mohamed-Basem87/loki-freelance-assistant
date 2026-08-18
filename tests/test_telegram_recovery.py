import asyncio
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# ------------------------------------------------------------------
# Startup recovery / live-ingestion race (see app.handlers.telegram
# .start() and ._recover_all_channels()).
#
# Old behavior: the live NewMessage handler was registered only after
# every channel finished recovering. A message arriving after a
# channel's recovery snapshot was taken but before the handler existed
# was silently dropped by both mechanisms -- neither recovery (which
# had already taken its snapshot) nor the live handler (which didn't
# exist yet) would ever see it.
#
# New behavior: every channel's lock is acquired before the live
# handler is registered, so the handler is always "capable of
# receiving events" -- and every lock already held -- before Telethon
# can dispatch a single update. A message arriving during recovery is
# captured (its task is created immediately) but blocks on the
# channel's lock until _recover_all_channels releases it, i.e. until
# that channel's own recovery has finished.
# ------------------------------------------------------------------


def test_live_message_during_recovery_is_captured_and_processed_after(monkeypatch):
    """
    Regression test for the startup race. Simulates the exact sequence
    from the audit:

        startup begins
            -> live handler already capable of receiving events
            -> recovery snapshot begins
            -> a new Telegram message arrives
            -> recovery completes
            -> live processing occurs

    This must fail under the old implementation (the handler wasn't
    registered until after recovery, so the live message would never
    be processed at all -- it isn't in recovery's snapshot and the
    handler doesn't exist yet to catch it live). Under the fix, the
    message is captured immediately and processed right after
    recovery finishes.
    """
    monkeypatch.setattr(telegram, "TARGET_CHANNELS", [-1010])
    monkeypatch.setattr(telegram.state, "get_last_message_id", lambda _: 100)

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    recovery_message_started = asyncio.Event()
    release_recovery_message = asyncio.Event()
    processed_order = []

    async def fake_process_message(message_or_event):
        mid = message_or_event.id

        if mid == 105:
            # This is the in-flight recovery message: signal that
            # recovery is now "in progress" and hold here until the
            # test has had a chance to fire the live event, mirroring
            # a message arriving mid-recovery (e.g. while a slower
            # earlier recovered message is still being classified).
            recovery_message_started.set()
            await release_recovery_message.wait()

        processed_order.append(mid)
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    # Recovery's own snapshot only contains message 105 -- message 106
    # (the "live" one) arrives too late to be in it, exactly as
    # described in the audit.
    recovered_messages = [SimpleNamespace(id=105, chat_id=-1010)]

    async def fake_iter_messages(channel, min_id, reverse, limit):
        for message in recovered_messages:
            yield message

    fake_client = SimpleNamespace(iter_messages=fake_iter_messages)

    async def scenario():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1010: True}

        # Mirrors start(): every channel's lock is acquired *before*
        # the live handler becomes capable of receiving events.
        for channel in [-1010]:
            await channel_locks[channel].acquire()

        # From here on the live handler is "registered" -- represented
        # in this test by being free to invoke _handle_live_message at
        # any time without losing the event, exactly like Telethon
        # dispatching a task to an already-registered handler.
        recovery_task = asyncio.create_task(
            telegram._recover_all_channels(
                fake_client,
                channel_locks,
                recovery_blocked,
            )
        )

        await recovery_message_started.wait()

        # A live message arrives *during* recovery.
        live_event = SimpleNamespace(id=106, chat_id=-1010)
        live_task = asyncio.create_task(
            telegram._handle_live_message(live_event, channel_locks)
        )

        # Let the live task run far enough to try to acquire the lock
        # and block on it -- it must not be lost, and it must not run
        # ahead of recovery.
        await asyncio.sleep(0)
        assert processed_order == [], (
            "the live message must not be processed (or lost) before "
            "recovery for its channel has finished"
        )

        release_recovery_message.set()

        await recovery_task
        await live_task

    asyncio.run(scenario())

    assert processed_order == [105, 106], (
        "recovery's message must be processed first, and the live "
        "message that arrived mid-recovery must still be processed "
        "afterward -- it must never be silently lost"
    )
    assert watermarks == [(-1010, 105), (-1010, 106)]


def test_live_message_after_recovery_failure_does_not_advance_past_failed_message(
    monkeypatch,
):
    """
    Recovery failures are a second ordering boundary that must remain
    safe after the startup-race fix.

    If recovery stops at message 105, a live message 106 can still be
    captured and processed for real-time behavior, but its processing
    must NOT advance the watermark to 106. Otherwise the failed 105
    would be skipped permanently on the next restart.

    This regression protects the shared `recovery_blocked` barrier
    introduced by the startup race fix.
    """
    monkeypatch.setattr(telegram, "TARGET_CHANNELS", [-1011])
    monkeypatch.setattr(
        telegram.state,
        "get_last_message_id",
        lambda _: 100,
    )

    watermarks = []
    processed = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state,
        "async_set_last_message_id",
        fake_set_watermark,
    )

    async def fake_process_message(message_or_event):
        processed.append(message_or_event.id)

        if message_or_event.id == 105:
            return False

        return True

    monkeypatch.setattr(
        telegram,
        "process_message",
        fake_process_message,
    )

    recovered_messages = [
        SimpleNamespace(id=105, chat_id=-1011),
    ]

    async def fake_iter_messages(channel, min_id, reverse, limit):
        for message in recovered_messages:
            yield message

    fake_client = SimpleNamespace(iter_messages=fake_iter_messages)

    async def scenario():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1011: True}

        await channel_locks[-1011].acquire()

        recovery_task = asyncio.create_task(
            telegram._recover_all_channels(
                fake_client,
                channel_locks,
                recovery_blocked,
            )
        )

        # Wait for recovery to finish and release the channel lock.
        await recovery_task

        assert recovery_blocked[-1011] is True

        # This message is newer than the failed recovery message. It
        # should still be processed, but it must not move the
        # watermark beyond the failed point.
        live_event = SimpleNamespace(id=106, chat_id=-1011)

        await telegram._handle_live_message(
            live_event,
            channel_locks,
            recovery_blocked,
        )

    asyncio.run(scenario())

    assert processed == [105, 106]
    assert watermarks == [], (
        "message 105 failed before its watermark could advance, and "
        "live message 106 must not advance past the failed recovery "
        "point"
    )


def test_recovery_and_live_seeing_the_same_message_is_processed_once(
    monkeypatch,
):
    """
    The opposite overlap: recovery's snapshot AND the live handler
    both see the same message (e.g. it arrived just before recovery
    took its snapshot, so it's in both). The existing SQLite job_uuid
    dedup in app.job_processor.process_job must make the second
    observation a harmless no-op: the job is processed at most once,
    the watermark ends up correct, and no duplicate notification is
    sent.

    This exercises the real app.message_processor.process_message ->
    app.job_processor.process_job path (not a fake), since that's
    where the actual dedup guarantee lives.
    """
    from app.job_processor import _make_job_uuid
    from app.message_processor import process_message

    tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")

    from app.logger import logger

    original_log_path = logger.path
    logger.path = Path(tmp_dir) / "test_logs.db"
    logger.initialize()

    private_sends = {"count": 0}
    channel_sends = {"count": 0}

    async def fake_private(**kwargs):
        private_sends["count"] += 1
        return True

    async def fake_channel(**kwargs):
        channel_sends["count"] += 1
        return True

    monkeypatch.setattr("app.job_processor.send_notification", fake_private)
    monkeypatch.setattr(
        "app.job_processor.send_channel_notification", fake_channel
    )

    class FakeChat:
        title = "Race Channel"

    class FakeEvent:
        buttons = []

        def __init__(self, event_id, chat_id, text):
            self.id = event_id
            self.chat_id = chat_id
            self.chat = FakeChat()
            self.raw_text = text

    # notify_directly text so this actually exercises the notification
    # path, not just the "Rejected, nothing to dedup" path.
    text = "Power BI Dashboard Needed\n\nNeed a Power BI dashboard built from sales data."

    try:
        # Recovery "sees" message X first (a plain Message-shaped
        # object, exactly like _recover_channel iterates over).
        recovered = FakeEvent(777, -1020, text)
        recovery_ok = asyncio.run(process_message(recovered))

        # The live handler independently sees the *same* message X
        # (same chat_id + message id) shortly after.
        live = FakeEvent(777, -1020, text)
        live_ok = asyncio.run(process_message(live))

        assert recovery_ok is True
        assert live_ok is True

        job_uuid = _make_job_uuid("-1020", "777")
        row = logger.get_job(job_uuid)

        assert row is not None
        assert logger.count_jobs() == 1, (
            "the same message seen by recovery and the live handler "
            "must be logged as exactly one job"
        )
        assert row["Notification Status"] == "Complete"
        assert private_sends["count"] == 1, (
            "no duplicate private notification for the same message"
        )
        assert channel_sends["count"] == 1, (
            "no duplicate channel notification for the same message"
        )
    finally:
        logger.close()
        logger.path = original_log_path


def test_start_registers_live_handler_before_recovery(monkeypatch):
    """
    Regression test for the actual startup ordering in start().

    The old implementation performed every channel's recovery before
    registering the NewMessage handler. That left a real gap in which
    a message could arrive after recovery's snapshot but before the
    live handler existed.

    This test exercises start() itself (with Telegram/networking
    replaced by fakes) and asserts the required invariant directly:

        acquire channel locks
            -> register live handler
            -> begin recovery

    It also verifies that every channel lock is held while recovery is
    running, so a live event captured by Telethon cannot overtake that
    channel's recovery.
    """
    monkeypatch.setattr(telegram, "TARGET_CHANNELS", [-1101, -1102])

    events = []

    class FakeClient:
        async def start(self, phone):
            events.append(("client_start", phone))

        async def get_me(self):
            return SimpleNamespace(first_name="Test")

        def on(self, event_filter):
            # Real Telethon's client.on(event_builder) returns the
            # decorator to apply to the handler function; the fake
            # events.NewMessage(...) below already *is* that decorator
            # (it records registration and returns the handler
            # unchanged), so this just passes it through.
            return event_filter

        async def run_until_disconnected(self):
            events.append(("run_until_disconnected",))

    monkeypatch.setattr(
        telegram,
        "TelegramClient",
        lambda *args, **kwargs: FakeClient(),
    )

    async def fake_warm_entity_cache(client):
        events.append(("warm_entity_cache",))

    monkeypatch.setattr(
        telegram,
        "_warm_entity_cache",
        fake_warm_entity_cache,
    )

    handler_registered = {"value": False}
    registered_handler = {"handler": None}

    def fake_new_message(**kwargs):
        def decorator(handler):
            handler_registered["value"] = True
            registered_handler["handler"] = handler
            events.append(("handler_registered", tuple(kwargs["chats"])))
            return handler

        return decorator

    monkeypatch.setattr(
        telegram.events,
        "NewMessage",
        fake_new_message,
    )

    async def fake_recover_all_channels(
        client,
        channel_locks,
        recovery_blocked,
    ):
        events.append(
            (
                "recovery_started",
                handler_registered["value"],
                all(
                    channel_locks[channel].locked()
                    for channel in telegram.TARGET_CHANNELS
                ),
                all(
                    recovery_blocked[channel]
                    for channel in telegram.TARGET_CHANNELS
                ),
            )
        )

        assert handler_registered["value"] is True
        assert all(
            channel_locks[channel].locked()
            for channel in telegram.TARGET_CHANNELS
        )
        assert all(
            recovery_blocked[channel]
            for channel in telegram.TARGET_CHANNELS
        )

        for channel in telegram.TARGET_CHANNELS:
            recovery_blocked[channel] = False
            channel_locks[channel].release()

        events.append(("recovery_finished",))

    monkeypatch.setattr(
        telegram,
        "_recover_all_channels",
        fake_recover_all_channels,
    )

    asyncio.run(telegram.start())

    assert registered_handler["handler"] is not None

    event_names = [event[0] for event in events]
    assert event_names.index("handler_registered") < event_names.index(
        "recovery_started"
    )
    assert events[event_names.index("recovery_started")][1:] == (
        True,
        True,
        True,
    )
    assert event_names[-1] == "run_until_disconnected"
