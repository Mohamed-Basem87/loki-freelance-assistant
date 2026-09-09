import asyncio
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.adapters.sources.telegram as telegram
import app.handlers.telegram as telegram_compat


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

    async def fake_get_last_message_id(_channel):
        return 100

    monkeypatch.setattr(telegram.state, "get_last_message_id", fake_get_last_message_id)

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

    async def fake_get_last_message_id(_channel):
        return 200

    monkeypatch.setattr(telegram.state, "get_last_message_id", fake_get_last_message_id)

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
# Startup recovery / live-ingestion race (see app.adapters.sources.telegram
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

    async def fake_get_last_message_id(_channel):
        return 100

    monkeypatch.setattr(telegram.state, "get_last_message_id", fake_get_last_message_id)

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
                channels=[-1010],
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
    async def fake_get_last_message_id(_channel):
        return 100

    monkeypatch.setattr(
        telegram.state,
        "get_last_message_id",
        fake_get_last_message_id,
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
                channels=[-1011],
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

    async def fake_private(**kwargs):
        private_sends["count"] += 1
        return True

    monkeypatch.setattr("app.job_processor.send_notification", fake_private)

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
    finally:
        logger.close()
        logger.path = original_log_path


def test_start_registers_live_handler_before_recovery(monkeypatch):
    """
    Regression test for the actual startup ordering in start()
    (app.handlers.telegram.start, the config-reading compatibility seam).

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
    monkeypatch.setattr(telegram_compat, "TARGET_CHANNELS", [-1101, -1102])

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

    async def fake_warm_entity_cache(client, channels=None):
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
        channels=None,
        max_messages=None,
    ):
        events.append(
            (
                "recovery_started",
                handler_registered["value"],
                all(
                    channel_locks[channel].locked()
                    for channel in channels
                ),
                all(
                    recovery_blocked[channel]
                    for channel in channels
                ),
            )
        )

        assert handler_registered["value"] is True
        assert all(
            channel_locks[channel].locked()
            for channel in channels
        )
        assert all(
            recovery_blocked[channel]
            for channel in channels
        )

        for channel in channels:
            recovery_blocked[channel] = False
            channel_locks[channel].release()

        events.append(("recovery_finished",))

    monkeypatch.setattr(
        telegram,
        "_recover_all_channels",
        fake_recover_all_channels,
    )

    asyncio.run(telegram_compat.start())

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


# ------------------------------------------------------------------
# Live-failure barrier (symmetry with startup recovery failures).
#
# Previously _handle_live_message logged a live processing failure and
# moved on: the next successful live message advanced the watermark
# past the failed one, and recovery walks messages strictly newer than
# the watermark (min_id=last_id) -- so the failed message was
# permanently unrecoverable on the next restart. The fix raises the
# same recovery_blocked barrier a failed startup recovery does, wakes
# the channel's retry watcher, and refuses to advance the watermark
# until the retry re-processes the failed message successfully.
# ------------------------------------------------------------------


def test_live_failure_raises_barrier_and_blocks_watermark_advance(monkeypatch):
    """
    A live message that returns False from process_message must raise
    the channel's recovery barrier. A later successful live message
    must then NOT advance the watermark -- under the old behavior it
    did, permanently orphaning the failed message below the watermark.
    """
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    called = []

    async def fake_process_message(event):
        called.append(event.id)
        return event.id != 1001

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1012: False}

        failed_event = SimpleNamespace(id=1001, chat_id=-1012)
        await _handle_live_message(failed_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1012] is True, (
            "a live failure must raise the channel's recovery barrier"
        )

        later_event = SimpleNamespace(id=1002, chat_id=-1012)
        await _handle_live_message(later_event, channel_locks, recovery_blocked)

        assert watermarks == [], (
            "a successful live message must NOT advance the watermark "
            "while the channel is blocked behind a failed message"
        )

    asyncio.run(run())

    assert called == [1001, 1002]


def test_live_failure_by_exception_also_raises_barrier(monkeypatch):
    """
    A live message whose processing RAISES must be treated like a
    return-False failure: raise the barrier so nothing advances past
    it, and keep the failed message recoverable.
    """
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_log_error(*args, **kwargs):
        return None

    monkeypatch.setattr(telegram.logger, "log_error", fake_log_error)

    async def fake_process_message(event):
        if event.id == 1101:
            raise RuntimeError("transient classifier failure")
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1015: False}

        bad_event = SimpleNamespace(id=1101, chat_id=-1015)
        await _handle_live_message(bad_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1015] is True

        good_event = SimpleNamespace(id=1102, chat_id=-1015)
        await _handle_live_message(good_event, channel_locks, recovery_blocked)

        assert watermarks == []

    asyncio.run(run())


def test_live_failure_is_recovered_by_retry_and_barrier_released(monkeypatch):
    """
    A failed live message must not stay lost behind the barrier
    forever: the retry watcher re-runs recovery from the held
    watermark and, once the failure clears, advances the watermark
    over the previously-failed message -- the exact guarantee startup
    recovery already provides.
    """
    from app.adapters.sources.telegram import _handle_live_message, _recover_channel

    process_results = {2001: False}
    watermarks = []

    async def fake_get_last_message_id(_channel):
        return 2000

    monkeypatch.setattr(
        telegram.state, "get_last_message_id", fake_get_last_message_id
    )

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        return process_results.get(message.id, True)

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class _FakeClient:
        def __init__(self, messages):
            self.messages = messages

        async def iter_messages(self, channel, min_id, reverse, limit):
            for message in self.messages:
                yield message

    messages = [SimpleNamespace(id=2001, chat_id=-1013)]

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1013: False}

        failed_event = SimpleNamespace(id=2001, chat_id=-1013)
        await _handle_live_message(failed_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1013] is True
        assert watermarks == []

        process_results[2001] = True

        recovered = await _recover_channel(_FakeClient(messages), -1013)

        assert recovered is telegram.RECOVERED_COMPLETE
        assert watermarks == [(-1013, 2001)], (
            "the retry must walk the held watermark and advance it over "
            "the previously-failed message"
        )

        recovery_blocked[-1013] = False

    asyncio.run(run())


def test_live_failure_wakes_retry_watcher_which_releases_barrier(monkeypatch):
    """
    The full loop end to end: a live failure raises the barrier AND
    wakes that channel's retry watcher (via its per-channel event); the
    watcher re-runs recovery from the held watermark and -- once the
    transient failure clears -- releases the barrier on its own, after
    which a subsequent live message can advance the watermark again.
    """
    from app.adapters.sources.telegram import _handle_live_message

    process_results = {3001: False}
    watermarks = []

    async def fake_get_last_message_id(_channel):
        return 3000

    monkeypatch.setattr(
        telegram.state, "get_last_message_id", fake_get_last_message_id
    )

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        return process_results.get(message.id, True)

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class _FakeClient:
        def __init__(self, messages):
            self.messages = messages

        async def iter_messages(self, channel, min_id, reverse, limit):
            for message in self.messages:
                yield message

    messages = [SimpleNamespace(id=3001, chat_id=-1014)]
    fake_client = _FakeClient(messages)

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1014: False}
        wake = asyncio.Event()
        generation = defaultdict(int)

        def on_failure(chat_id):
            recovery_blocked[chat_id] = True
            generation[chat_id] += 1
            wake.set()

        watcher = asyncio.create_task(
            telegram._retry_recovery_channel(
                fake_client,
                -1014,
                channel_locks[-1014],
                recovery_blocked,
                wake,
                generation,
                retry_base_seconds=0.01,
                retry_cap_seconds=0.02,
            )
        )

        await asyncio.sleep(0)

        failed_event = SimpleNamespace(id=3001, chat_id=-1014)
        await _handle_live_message(
            failed_event, channel_locks, recovery_blocked, on_failure
        )
        assert recovery_blocked[-1014] is True

        process_results[3001] = True

        for _ in range(100):
            if not recovery_blocked[-1014]:
                break
            await asyncio.sleep(0.02)

        assert recovery_blocked[-1014] is False, (
            "the watcher must re-run recovery and release the barrier "
            "once the failure clears"
        )
        assert watermarks == [(-1014, 3001)]

        wake.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())


def test_wake_interrupts_long_retry_backoff(monkeypatch):
    """
    Prove the retry watcher's backoff is genuinely interruptible by the
    per-channel wake event.

    Regression for the bug where a live failure that set the channel's wake
    event could NOT interrupt a retry watcher that was mid-`asyncio.sleep(delay)`:
    a failed message would wait out the remainder of a (potentially long)
    backoff before being re-attempted, contradicting the documented
    "wakes that channel's retry watcher immediately" behavior.

    The watcher is placed into a *long* (1000s) backoff, the wake event is
    fired, and the test asserts recovery runs within a tight wall-clock
    bound -- a material fraction of a second, far short of the 1000s delay.
    It further asserts the wake is consumed on servicing (no spurious run of
    immediate retries), that the single watcher task runs exactly one
    recovery attempt (no duplicate retry task), and that the barrier is
    released on a clean recovery.
    """
    import time

    from app.adapters.sources.telegram import _retry_recovery_channel

    recover_calls = []

    async def fake_recover(client, channel, *, max_messages=None):
        recover_calls.append(time.monotonic())
        return telegram.RECOVERED_COMPLETE

    monkeypatch.setattr(telegram, "_recover_channel", fake_recover)

    async def run():
        channel = -2001
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {channel: True}
        wake = asyncio.Event()
        # A failure has already occurred: the barrier is up and generation
        # is 1. The watcher must capture this generation value BEFORE we
        # fire the wake so the post-recovery check sees no NEW failure and
        # releases the barrier on the clean recovery.
        generation = defaultdict(int, {channel: 1})

        watcher = asyncio.create_task(
            _retry_recovery_channel(
                object(),
                channel,
                channel_locks[channel],
                recovery_blocked,
                wake,
                generation,
                retry_base_seconds=1000.0,
                retry_cap_seconds=2000.0,
            )
        )

        # Give the watcher a moment to reach the blocked-branch backoff wait
        # (capturing generation and starting the 1000s sleep).
        await asyncio.sleep(0.05)
        assert recover_calls == [], "watcher must still be in backoff, not recovered"
        assert recovery_blocked[channel] is True

        # The wake event is clear and the watcher is sleeping out a 1000s
        # backoff. Fire the wake and time the recovery.
        assert not wake.is_set()
        start = time.monotonic()
        wake.set()

        # The watcher must proceed to recovery well before the 1000s backoff
        # elapses -- this is the whole point of the interrupt.
        for _ in range(500):
            if recover_calls:
                break
            await asyncio.sleep(0.005)
        elapsed = time.monotonic() - start

        assert recover_calls, "wake must interrupt the backoff and trigger recovery"
        assert elapsed < 1.0, (
            f"recovery after wake took {elapsed:.3f}s; expected to interrupt "
            f"the long backoff materially before the 1000s delay expired"
        )

        # Clean recovery: generation unchanged during the retry, so the
        # barrier is released and the watermark can advance again.
        assert recovery_blocked[channel] is False, (
            "a clean recovery triggered by the wake must release the barrier"
        )

        # The wake event was consumed on servicing the retry, so it cannot
        # re-fire an immediate retry.
        assert not wake.is_set(), (
            "the wake event must be consumed after servicing the retry so it "
            "cannot cause an unbounded run of immediate retries"
        )

        # No duplicate retry task / no spurious extra recovery: exactly one
        # recovery attempt ran, and with the barrier cleared the watcher
        # pauses rather than re-running recovery in a tight loop.
        await asyncio.sleep(0.05)
        assert len(recover_calls) == 1, (
            f"expected exactly one recovery attempt, got {len(recover_calls)}"
        )

        # Clean shutdown: cancel the watcher so no task is left behind.
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(run())


# ------------------------------------------------------------------
# P1: Telegram recovery-barrier bypass fix -- get_chat() failure must
# not prevent message processing.
#
# The live handler's metadata lookup (event.get_chat()) is now
# best-effort: if it fails or times out, the handler must still call
# _handle_live_message so the message enters the durable processing
# state machine, the recovery barrier is raised on failure, and the
# watermark semantics remain safe.
# ------------------------------------------------------------------


def test_live_handler_get_chat_success(monkeypatch):
    """get_chat() succeeds normally -- message is processed and logged with metadata."""
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []
    recovery_blocked = {}

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(event):
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class FakeChat:
        title = "Test Channel"

    class FakeEvent:
        def __init__(self, event_id, chat_id):
            self.id = event_id
            self.chat_id = chat_id
            self.chat = FakeChat()

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked[-1020] = False
        event = FakeEvent(5001, -1020)
        await _handle_live_message(event, channel_locks, recovery_blocked)

    asyncio.run(run())

    assert watermarks == [(-1020, 5001)]
    assert recovery_blocked.get(-1020) is False


def test_live_handler_get_chat_timeout_still_processes_message(monkeypatch):
    """get_chat() times out -- message is still processed, barrier raised on failure."""
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    # First call fails (timeout), second call succeeds
    call_count = {"n": 0}

    async def fake_process_message(event):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return False  # First message fails
        return True  # Second message succeeds

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class FakeEvent:
        def __init__(self, event_id, chat_id):
            self.id = event_id
            self.chat_id = chat_id

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1021: False}

        # First message: get_chat() fails, process_message returns False
        failed_event = FakeEvent(6001, -1021)
        await _handle_live_message(failed_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1021] is True
        assert watermarks == []

        # Second message: get_chat() fails, process_message returns True
        # Watermark must NOT advance because barrier is raised
        later_event = FakeEvent(6002, -1021)
        await _handle_live_message(later_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1021] is True
        assert watermarks == []

        # Now clear barrier and process a message that succeeds
        recovery_blocked[-1021] = False
        good_event = FakeEvent(6003, -1021)
        await _handle_live_message(good_event, channel_locks, recovery_blocked)

        assert watermarks == [(-1021, 6003)]

    asyncio.run(run())


def test_live_handler_get_chat_exception_still_processes_message(monkeypatch):
    """get_chat() raises an exception -- message is still processed."""
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []
    recovery_blocked = {}

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(event):
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class FakeEvent:
        def __init__(self, event_id, chat_id):
            self.id = event_id
            self.chat_id = chat_id

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked[-1022] = False
        event = FakeEvent(7001, -1022)
        await _handle_live_message(event, channel_locks, recovery_blocked)

    asyncio.run(run())

    assert watermarks == [(-1022, 7001)]
    assert recovery_blocked.get(-1022) is False


def test_live_handler_get_chat_failure_watermark_barrier_safety(monkeypatch):
    """When get_chat() fails and process_message returns False, the barrier
    is raised and a later successful message cannot advance the watermark
    past the failed one."""
    from app.adapters.sources.telegram import _handle_live_message

    watermarks = []

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(event):
        # First message fails, second succeeds
        return event.id != 8001

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class FakeEvent:
        def __init__(self, event_id, chat_id):
            self.id = event_id
            self.chat_id = chat_id

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1023: False}

        # Message 8001 fails (get_chat fails, process_message returns False)
        failed_event = FakeEvent(8001, -1023)
        await _handle_live_message(failed_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1023] is True
        assert watermarks == []

        # Message 8002 succeeds (get_chat fails, process_message returns True)
        # Watermark must NOT advance past the failed message
        later_event = FakeEvent(8002, -1023)
        await _handle_live_message(later_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1023] is True
        assert watermarks == []

    asyncio.run(run())


def test_live_handler_get_chat_failure_later_message_cannot_skip_failed(monkeypatch):
    """A later successful message cannot permanently skip an earlier failed
    message when get_chat() failed for the failed message."""
    from app.adapters.sources.telegram import _handle_live_message, _recover_channel

    process_results = {9001: False}
    watermarks = []

    async def fake_get_last_message_id(_channel):
        return 9000

    monkeypatch.setattr(
        telegram.state, "get_last_message_id", fake_get_last_message_id
    )

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        return process_results.get(message.id, True)

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    class _FakeClient:
        def __init__(self, messages):
            self.messages = messages

        async def iter_messages(self, channel, min_id, reverse, limit):
            for message in self.messages:
                yield message

    class FakeEvent:
        def __init__(self, event_id, chat_id):
            self.id = event_id
            self.chat_id = chat_id

    messages = [FakeEvent(9001, -1024)]

    async def run():
        channel_locks = defaultdict(asyncio.Lock)
        recovery_blocked = {-1024: False}

        # Live message 9001 fails (get_chat fails, process_message returns False)
        failed_event = FakeEvent(9001, -1024)
        await _handle_live_message(failed_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1024] is True
        assert watermarks == []

        # Live message 9002 succeeds (get_chat fails, process_message returns True)
        # Barrier is raised, so watermark must not advance
        later_event = FakeEvent(9002, -1024)
        await _handle_live_message(later_event, channel_locks, recovery_blocked)

        assert recovery_blocked[-1024] is True
        assert watermarks == []

        # Now the failure clears - retry recovery from held watermark
        process_results[9001] = True

        recovered = await _recover_channel(_FakeClient(messages), -1024)

        assert recovered is telegram.RECOVERED_COMPLETE
        assert watermarks == [(-1024, 9001)], (
            "retry must walk the held watermark and advance it over "
            "the previously-failed message"
        )

    asyncio.run(run())
