
import asyncio
from pathlib import Path

import pytest

from app import logger as logger_module
from app.notifier import NotificationService
from app.adapters.sources.freehub import FreeHubJobSource


def test_channel_destination_obeys_category_and_source_filters(tmp_path):
    original = logger_module.logger.path
    logger_module.logger.close()
    logger_module.logger.path = Path(tmp_path) / "routing.db"
    logger_module.logger.initialize()
    try:
        async def scenario():
            user_id = await logger_module.logger.run(
                logger_module.logger.ensure_user, 900001, "channel-test", "Channel Test"
            )
            await logger_module.logger.run(
                logger_module.logger.set_user_category, user_id, "data_analysis"
            )
            await logger_module.logger.run(
                logger_module.logger.set_user_source, user_id, "nafezly"
            )
            await logger_module.logger.run(
                logger_module.logger.ensure_channel_destination, -100900001
            )

            # The channel has no category/source preference yet, so it is
            # not eligible merely because it is a non-user destination.
            rows = await logger_module.logger.run(
                logger_module.logger.get_category_subscribers,
                "data_analysis",
                "mostaql",
            )
            assert rows == []

        asyncio.run(scenario())
    finally:
        logger_module.logger.close()
        logger_module.logger.path = original


def test_notification_service_attempts_every_sink():
    class Sink:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        async def send(self, **payload):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    async def scenario():
        first = Sink(True)
        second = Sink(RuntimeError("down"))
        service = NotificationService(sinks=(first, second))
        result = await service.send(job_uuid="j1")
        assert result is False
        assert first.calls == 1
        assert second.calls == 1

    asyncio.run(scenario())


def test_user_notification_claim_stamps_a_lease_without_spending_an_attempt(tmp_path):
    """Claiming a notification for delivery must not consume its
    attempt budget: attempts are spent only by a real (non-RetryAfter)
    delivery failure. Regression test for the audit finding that
    claim-time incrementing burned an attempt per restart cycle on
    crashed deliveries, exhausting the budget with no real send."""
    original = logger_module.logger.path
    logger_module.logger.close()
    logger_module.logger.path = Path(tmp_path) / "attempts.db"
    logger_module.logger.initialize()
    try:
        async def scenario():
            user_id = await logger_module.logger.run(
                logger_module.logger.ensure_user, 900002, "attempt-test", "Attempt Test"
            )
            await logger_module.logger.run(
                logger_module.logger.set_user_category, user_id, "data_analysis"
            )
            await logger_module.logger.run(
                logger_module.logger.queue_user_notifications,
                "attempt-job", "data_analysis", "", True
            )
            rows = await logger_module.logger.run(
                logger_module.logger.claim_pending_user_notifications, 20
            )
            assert len(rows) == 1
            assert rows[0]["Status"] == "Sending"
            assert rows[0]["Attempts"] == "0"
            assert rows[0]["Claimed At"], "a delivery lease must be stamped on claim"

            # A genuine delivery failure spends exactly one attempt; the
            # row stays claimable until the max-attempt gate kicks in.
            await logger_module.logger.run(
                logger_module.logger.update_user_notification,
                rows[0]["Notification ID"], "Failed", 1, "failed", None, True
            )
            again = await logger_module.logger.run(
                logger_module.logger.claim_pending_user_notifications, 20
            )
            assert [r["Attempts"] for r in again] == ["1"]

            # Once failures reach the configured budget, the row stops
            # being claimed (it genuinely failed -- not RetryAfter).
            original_max_attempts = logger_module.logger.max_user_notification_attempts
            logger_module.logger.max_user_notification_attempts = 1
            try:
                await logger_module.logger.run(
                    logger_module.logger.update_user_notification,
                    again[0]["Notification ID"], "Failed", 1, "failed", None, True
                )
                assert await logger_module.logger.run(
                    logger_module.logger.claim_pending_user_notifications, 20
                ) == []
            finally:
                # Restore the shared logger singleton's attribute count
                # immediately -- this attribute is not test-scoped, so
                # leaving it at 1 would silently break every other
                # test in the suite that claims user notifications
                # against the real MAX_ATTEMPTS budget.
                logger_module.logger.max_user_notification_attempts = original_max_attempts

        asyncio.run(scenario())
    finally:
        logger_module.logger.close()
        logger_module.logger.path = original


def test_stale_sending_lease_is_reopened_without_charging_an_attempt(tmp_path):
    """A "Sending" row whose claim lease has expired (the claiming
    process died mid-delivery) must be returned to the pending queue so
    it can be delivered again -- and must NOT be charged an attempt for
    the abandoned delivery. This is the crash scenario the old
    claim-time incrementing got wrong."""
    from datetime import datetime, timedelta

    original = logger_module.logger.path
    logger_module.logger.close()
    logger_module.logger.path = Path(tmp_path) / "lease.db"
    logger_module.logger.initialize()
    try:
        async def scenario():
            user_id = await logger_module.logger.run(
                logger_module.logger.ensure_user, 900003, "lease-test", "Lease Test"
            )
            await logger_module.logger.run(
                logger_module.logger.set_user_category, user_id, "data_analysis"
            )
            await logger_module.logger.run(
                logger_module.logger.queue_user_notifications,
                "lease-job", "data_analysis", "", True
            )
            # Simulate a crashed delivery: claimed long ago, never finished.
            stale = (datetime.now() - timedelta(hours=1)).isoformat()
            cursor = logger_module.logger._conn.execute(
                'UPDATE user_notifications SET "Status" = "Sending", '
                '"Claimed At" = ?, "Updated At" = ? WHERE "Job UUID" = ?',
                (stale, stale, "lease-job"),
            )
            assert cursor.rowcount == 1
            logger_module.logger.save()

            # Shrink the lease so the stale claim is older than it.
            logger_module.logger.user_notification_claim_lease_seconds = 60

            rows = await logger_module.logger.run(
                logger_module.logger.claim_pending_user_notifications, 20
            )
            assert len(rows) == 1
            assert rows[0]["Status"] == "Sending"
            assert rows[0]["Attempts"] == "0", (
                "an abandoned delivery must not cost an attempt"
            )

        asyncio.run(scenario())
    finally:
        logger_module.logger.close()
        logger_module.logger.path = original


def test_freehub_source_uses_injected_http_client_in_poll_path(monkeypatch):
    """FreeHubJobSource must thread its injected http_client through to
    app.freehub.poll_once on every poll, rather than discarding it or
    constructing its own infrastructure. It never builds its own HTTP client
    or reads config -- the poller/marker/http_client are all injected."""
    sentinel = object()
    observed = {}

    async def fake_poll_once(*, client=None):
        observed["client"] = client
        return []

    from app import freehub as freehub_logic
    monkeypatch.setattr(freehub_logic, "poll_once", fake_poll_once)

    source = FreeHubJobSource(
        poller=lambda: freehub_logic.poll_once(client=sentinel),
        marker=lambda job: None,
        http_client=sentinel,
    )

    asyncio.run(source.poll())
    assert observed["client"] is sentinel
    assert source.http_client is sentinel


def test_db_timeout_quarantines_shared_executor_instead_of_replacing_it(monkeypatch):
    import app.logger as lm

    original_executor = lm._EXECUTOR
    original_timeout = lm._DB_TIMEOUT_SECONDS
    try:
        class FakeFuture:
            pass

        class FakeLoop:
            def run_in_executor(self, executor, fn):
                async def never():
                    await asyncio.Event().wait()
                return never()

        monkeypatch.setattr(lm, "_DB_TIMEOUT_SECONDS", 0.001)

        async def scenario():
            # Use the real logger object but replace its executor with an
            # executor that returns a never-completing awaitable.
            class Executor:
                pass
            lm._EXECUTOR = Executor()
            lm.logger._executor_poisoned = False

            monkeypatch.setattr(
                asyncio,
                "get_running_loop",
                lambda: FakeLoop(),
            )
            with pytest.raises(lm.StuckExecutorError):
                await lm.logger.run(lambda: None)
            assert lm.logger._executor_poisoned is True
            assert isinstance(lm._EXECUTOR, Executor)

        asyncio.run(scenario())
    finally:
        lm._EXECUTOR = original_executor
        lm._DB_TIMEOUT_SECONDS = original_timeout
        lm.logger._executor_poisoned = False


def test_runtime_numeric_validation_rejects_non_positive_values():
    from app.runtime_config import _validate_positive

    with pytest.raises(ValueError):
        _validate_positive("concurrency", 0)
    with pytest.raises(ValueError):
        _validate_positive("timeout", -1)
    _validate_positive("attempts", 1, minimum=0)
