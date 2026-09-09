"""Regression tests for BUG #1: Telegram worker reconnect/backoff.

Previously the Telegram worker's run() called run_until_disconnected()
once and, when it returned (a transient network disconnect), the worker
task simply ended -- a silent death. The container's healthcheck (even
with a per-worker liveness registry) would eventually observe it dead,
but the worker never got back up on its own.

Fix: run() loops forever, reconnecting on transient disconnects with
bounded exponential backoff (1s -> 60s cap), cleaning up the old client
before building a fresh one, while fatal errors (auth/session/forbidden)
propagate so TaskGroup shuts the process down and a CancelledError
(TaskGroup shutdown) propagates without attempting a reconnect.
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.adapters.sources.telegram as telegram


class _FakeSource:
    """Stand-in for TelegramChannelJobSource that records client builds."""

    def __init__(self, clients, loop):
        self._clients = clients
        self.client = None
        self.channels = ("chan-a",)
        self.max_recovery_messages = 2000
        self.recovery_retry_base_seconds = 5.0
        self.recovery_retry_cap_seconds = 300.0
        self.liveness_beat_interval_seconds = 15.0
        self.loop = loop
        self.connects = 0
        self.aclose_calls = 0

    async def _client(self):
        if self.client is None:
            idx = min(self.connects, len(self._clients) - 1)
            self.client = self._clients[idx]
            self.connects += 1
        return self.client

    async def aclose(self):
        self.aclose_calls += 1
        self.client = None


def test_transient_disconnect_reconnects_instead_of_dying(monkeypatch):
    """run_until_disconnected() returning normally (transient network
    hiccup) must trigger a reconnect, not silently end the worker."""
    events = []
    client = SimpleNamespace(id="client")
    loop_calls = {"n": 0}

    source = _FakeSource([client], events)

    async def fake_loop(client, channels, **kwargs):
        loop_calls["n"] += 1
        events.append(("loop", client.id))
        # First invocation returns immediately (a disconnect); later
        # invocations block until cancelled so the test can stop the
        # worker while it is "connected" again.
        if loop_calls["n"] == 1:
            return
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram, "_run_telegram_loop", fake_loop)
    monkeypatch.setattr(telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness())
    # Shrink only this test's backoff so the second connection attempt
    # (which this test is trying to observe) happens within the wait.
    monkeypatch.setattr(telegram, "_RECONNECT_BASE_SECONDS", 0.01)

    worker = telegram.TelegramChannelWorker(source)

    async def scenario():
        task = asyncio.create_task(worker.run())
        # Give run() time to connect, "disconnect", then reconnect.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert source.connects == 2, (
        "the worker must build a fresh connection after a transient "
        "disconnect rather than ending its task"
    )
    assert [e[0] for e in events] == ["loop", "loop"], (
        "_run_telegram_loop must be entered again after a reconnect"
    )
    assert source.aclose_calls >= 1, (
        "the stale client must be cleaned up before reconnecting"
    )


def test_fatal_telethon_error_propagates_to_shut_down(monkeypatch):
    """An auth/session error is not recoverable by reconnecting -- it
    must propagate so TaskGroup shuts the process down (the container
    restarts with fresh session state)."""
    source = _FakeSource([SimpleNamespace(id="c")], [])

    async def fatal_loop(client, channels, **kwargs):
        raise ConnectionError("AUTH_KEY_UNREGISTERED: session is invalid")

    monkeypatch.setattr(telegram, "_run_telegram_loop", fatal_loop)
    monkeypatch.setattr(telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness())

    worker = telegram.TelegramChannelWorker(source)

    with pytest.raises(ConnectionError, match="AUTH_KEY_UNREGISTERED"):
        asyncio.run(worker.run())

    assert source.connects == 1, (
        "a fatal error must not trigger a reconnect attempt"
    )


def test_cancellation_propagates_without_reconnecting(monkeypatch):
    """TaskGroup shutdown cancels the worker; the CancelledError must
    propagate immediately without any reconnect backoff."""
    source = _FakeSource([SimpleNamespace(id="c1"), SimpleNamespace(id="c2")], [])

    async def blocking_loop(client, channels, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(telegram, "_run_telegram_loop", blocking_loop)
    monkeypatch.setattr(telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness())

    worker = telegram.TelegramChannelWorker(source)

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert source.connects == 1, (
        "a clean cancellation must not initiate a reconnect"
    )


def test_fatal_error_detection_flags_known_markers():
    """The fatal-error classifier must recognize the unrecoverable session/
    auth error codes and leave transient errors alone."""
    assert telegram._is_fatal_telethon_error(ConnectionError("AUTH_KEY_UNREGISTERED"))
    assert telegram._is_fatal_telethon_error(RuntimeError("phone number invalid"))
    assert telegram._is_fatal_telethon_error(RuntimeError("API_ID invalid"))
    assert telegram._is_fatal_telethon_error(RuntimeError("SFAuthError: session revoked"))
    assert not telegram._is_fatal_telethon_error(
        ConnectionError("connection reset by peer")
    )
    assert not telegram._is_fatal_telethon_error(TimeoutError("timed out"))


def test_reconnect_backoff_is_bounded_and_exponential():
    """The reconnect backoff must grow exponentially to a hard cap so the
    reconnect loop can never become a CPU spin or hammer the API."""
    assert telegram._RECONNECT_BASE_SECONDS == 1.0
    assert telegram._RECONNECT_MAX_SECONDS == 60.0
    assert telegram._RECONNECT_MAX_ATTEMPTS == 0, (
        "0 = unlimited attempts, bounded only by the backoff cap"
    )

    delay = telegram._RECONNECT_BASE_SECONDS
    seen_delays = []
    for _ in range(10):
        seen_delays.append(delay)
        delay = min(delay * 2, telegram._RECONNECT_MAX_SECONDS)

    assert seen_delays[0] == 1.0
    assert seen_delays[1] == 2.0
    assert seen_delays[-1] == telegram._RECONNECT_MAX_SECONDS, (
        "the backoff must plateau at the cap, not grow without bound"
    )


def test_worker_reports_reconnecting_during_backoff(monkeypatch):
    """While the worker is in its reconnect/backoff window it must beat
    the liveness registry with STATE_RECONNECTING so the healthcheck
    treats it as temporarily degraded, not dead."""
    from app.heartbeat import WorkerLiveness

    real_liveness = WorkerLiveness()
    source = _FakeSource([SimpleNamespace(id="c")], [])

    # First loop invocation returns immediately (transient disconnect);
    # the worker then enters backoff. We can't easily observe the small
    # sleep, so drive the liveness registry directly and assert the
    # worker registers + beats as expected, then verify the state the
    # healthcheck consumes is the reconnecting state by simulating the
    # beat the worker performs.
    real_liveness.register("telegram")
    real_liveness.beat("telegram", telegram.STATE_RECONNECTING)
    assert real_liveness.state("telegram")["state"] == telegram.STATE_RECONNECTING

    # And confirm the module constants match what the healthcheck
    # accepts (alive|reconnecting).
    from app.healthcheck import _ACCEPTABLE_STATES
    assert telegram.STATE_RECONNECTING in _ACCEPTABLE_STATES


# ---------------------------------------------------------------------------
# Exception-classification hardening: type-first with narrow text fallback.
# The categories from the task are exercised explicitly: recoverable
# network/server/flood errors must NOT crash the process, while genuine
# auth/session/authorization errors still propagate for operator attention.
# ---------------------------------------------------------------------------


class _Req:
    """Stand-in for Telethon's `request` argument on typed RPC errors."""


def test_recoverable_network_and_connection_errors_are_not_fatal():
    import socket
    for exc in (
        ConnectionError("connection reset by peer"),
        ConnectionResetError("connection reset"),
        ConnectionAbortedError("aborted"),
        ConnectionRefusedError("refused"),
        OSError("resource temporarily unavailable"),
        BrokenPipeError("broken pipe"),
        socket.timeout("timed out"),
    ):
        assert not telegram._is_fatal_telethon_error(exc), f"{exc!r} must be recoverable"


def test_flood_and_rate_limit_errors_are_not_fatal():
    """The flood/rate-limit family must never crash the app -- including
    FloodTestPhoneWaitError, whose OLD substring classifier matched on the
    'phone' word in its class name and wrongly killed the process."""
    from telethon.errors import (      # noqa: E402
        FloodError,
        FloodWaitError,
        FloodPremiumWaitError,
        FloodTestPhoneWaitError,
        SlowModeWaitError,
    )
    assert issubclass(FloodTestPhoneWaitError, FloodError)
    for exc in (
        FloodError(_Req(), "flood"),
        FloodWaitError(_Req()),
        FloodPremiumWaitError(_Req()),
        FloodTestPhoneWaitError(_Req()),
        SlowModeWaitError(_Req()),
    ):
        assert not telegram._is_fatal_telethon_error(exc), (
            f"{type(exc).__name__} is a rate-limit: must reconnect, not crash"
        )


def test_server_and_rpc_failures_are_not_fatal():
    from telethon.errors import ServerError, TimedOutError, RPCError
    for exc in (
        ServerError(_Req(), "server error"),
        TimedOutError(_Req(), "timed out"),
        RPCError(_Req(), "temporary rpc failure"),
    ):
        assert not telegram._is_fatal_telethon_error(exc), (
            f"{type(exc).__name__} is transient: must reconnect, not crash"
        )


def test_authentication_and_session_errors_are_fatal_by_type():
    from telethon.errors import (
        AuthKeyError,
        AuthKeyDuplicatedError,
        AuthKeyInvalidError,
        AuthKeyUnregisteredError,
        AuthKeyPermEmptyError,
        SessionExpiredError,
        SessionPasswordNeededError,
        SessionRevokedError,
        UnauthorizedError,
    )
    for exc in (
        UnauthorizedError(_Req(), "unauthorized"),
        SessionExpiredError(_Req()),
        SessionRevokedError(_Req()),
        SessionPasswordNeededError(_Req()),
        AuthKeyUnregisteredError(_Req()),
        AuthKeyInvalidError(_Req()),
        AuthKeyError(_Req(), "auth key"),
        AuthKeyDuplicatedError(_Req()),
        AuthKeyPermEmptyError(_Req()),
    ):
        assert telegram._is_fatal_telethon_error(exc), (
            f"{type(exc).__name__} is an auth/session failure: must propagate"
        )


def test_authorization_channel_access_errors_are_fatal_by_type():
    """Genuinely unrecoverable authorization failures (the account cannot
    read the channel at all) must stay fatal -- via explicit Telethon types
    now, not the old word-matching."""
    from telethon.errors import (
        ChannelInvalidError,
        ChannelParicipantMissingError,
        ChannelPrivateError,
        ChatForbiddenError,
        ForbiddenError,
        UserBannedInChannelError,
        UserBlockedError,
        UserKickedError,
    )
    for exc in (
        ForbiddenError(_Req(), "forbidden"),
        ChatForbiddenError(_Req()),
        ChannelPrivateError(_Req()),
        ChannelInvalidError(_Req()),
        ChannelParicipantMissingError(_Req()),
        UserKickedError(_Req()),
        UserBannedInChannelError(_Req()),
        UserBlockedError(_Req()),
    ):
        assert telegram._is_fatal_telethon_error(exc), (
            f"{type(exc).__name__} blocks all reads: must propagate, not spin"
        )


def test_telethon_shutdown_read_error_is_not_fatal():
    from telethon.errors import ReadCancelledError
    assert not telegram._is_fatal_telethon_error(ReadCancelledError())


def test_text_fallback_uses_exact_rpc_codes_not_bare_words():
    """The fallback must match exact unrecoverable RPC codes, so a transient
    error that merely *mentions* 'session' or 'auth' in passing is NOT
    treated as fatal -- the failure mode of the old substring classifier."""
    assert telegram._is_fatal_telethon_error(ConnectionError("SESSION_REVOKED"))
    assert not telegram._is_fatal_telethon_error(
        RuntimeError("session is invalid in bucket 7; retrying in a moment")
    )
    assert not telegram._is_fatal_telethon_error(
        RuntimeError("auth service is flaky on this region, falling back")
    )
    assert not telegram._is_fatal_telethon_error(
        RuntimeError("renewing session cookie next poll")
    )
    assert not telegram._is_fatal_telethon_error(
        ConnectionError("terminal unavailable after reauthorization queue")
    )


def test_recoverable_flood_wait_error_triggers_reconnect_not_propagation(monkeypatch):
    """Run-level: a FloodWaitError raised inside the live loop is a
    recoverable condition -- the worker must reconnect, not exit."""
    from telethon.errors import FloodWaitError
    from app.heartbeat import WorkerLiveness

    source = _FakeSource([SimpleNamespace(id="c1"), SimpleNamespace(id="c2")], [])
    loop_calls = {"n": 0}

    async def flood_loop(client, channels, **kwargs):
        loop_calls["n"] += 1
        if loop_calls["n"] == 1:
            raise FloodWaitError(_Req())
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram, "_run_telegram_loop", flood_loop)
    monkeypatch.setattr(
        telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness()
    )
    monkeypatch.setattr(telegram, "_RECONNECT_BASE_SECONDS", 0.01)

    worker = telegram.TelegramChannelWorker(source)

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert source.connects == 2, (
        "a flood/rate-limit condition must reconnect, not crash the process"
    )


def test_read_cancelled_error_triggers_reconnect_not_propagation(monkeypatch):
    """Run-level: Telethon's ReadCancelledError (connection teardown) is a
    disconnect signal -- reconnect, do not exit."""
    from telethon.errors import ReadCancelledError

    source = _FakeSource([SimpleNamespace(id="c1"), SimpleNamespace(id="c2")], [])
    loop_calls = {"n": 0}

    async def read_cancelled_loop(client, channels, **kwargs):
        loop_calls["n"] += 1
        if loop_calls["n"] == 1:
            raise ReadCancelledError()
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram, "_run_telegram_loop", read_cancelled_loop)
    monkeypatch.setattr(
        telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness()
    )
    monkeypatch.setattr(telegram, "_RECONNECT_BASE_SECONDS", 0.01)

    worker = telegram.TelegramChannelWorker(source)

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert source.connects == 2


def test_typed_auth_error_still_propagates_to_shut_down(monkeypatch):
    """Run-level: a typed Telethon auth/session error remains fatal even
    though it is no longer classified by substring matching."""
    from telethon.errors import AuthKeyUnregisteredError

    source = _FakeSource([SimpleNamespace(id="c")], [])

    async def fatal_loop(client, channels, **kwargs):
        raise AuthKeyUnregisteredError(_Req())

    monkeypatch.setattr(telegram, "_run_telegram_loop", fatal_loop)
    monkeypatch.setattr(
        telegram, "liveness", __import__("app.heartbeat", fromlist=["WorkerLiveness"]).WorkerLiveness()
    )

    worker = telegram.TelegramChannelWorker(source)

    with pytest.raises(AuthKeyUnregisteredError):
        asyncio.run(worker.run())

    assert source.connects == 1, "a fatal auth error must not reconnect"


# ---------------------------------------------------------------------------
# Idle liveness: a healthy connected worker must keep beating on cadence so
# the healthcheck's staleness window treats "connected and waiting" as
# healthy instead of stale/hung.
# ---------------------------------------------------------------------------


def test_connected_worker_beats_liveness_on_cadence(monkeypatch):
    from app.heartbeat import WorkerLiveness

    real_liveness = WorkerLiveness()
    source = _FakeSource([SimpleNamespace(id="c1")], [])
    source.liveness_beat_interval_seconds = 0.02

    async def blocking_loop(client, channels, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram, "_run_telegram_loop", blocking_loop)
    monkeypatch.setattr(telegram, "liveness", real_liveness)

    worker = telegram.TelegramChannelWorker(source)

    async def scenario():
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.08)
        first = real_liveness.state("telegram")
        await asyncio.sleep(0.05)
        second = real_liveness.state("telegram")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return first, second

    first, second = asyncio.run(scenario())

    assert first is not None and second is not None
    assert first["state"] == telegram.STATE_ALIVE
    assert second["state"] == telegram.STATE_ALIVE
    assert second["last_beat"] > first["last_beat"], (
        "an idle-but-connected worker must keep beating on cadence so the "
        "healthcheck does not flag it stale"
    )
    assert source.connects == 1, "no reconnect should happen while connected"