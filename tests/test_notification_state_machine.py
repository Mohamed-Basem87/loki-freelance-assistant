"""
Full notification state-machine regression tests.

Walks the complete durable lifecycle end-to-end through the real
process_job() / _resume_pending_notifications_unlocked() /
retry_incomplete_notifications() interaction (not a fake standing in
for any of them):

    Pending -> Private SENT -> Channel FAILED -> Retry -> Channel SENT
    -> Complete

and the analogous suppression path:

    Pending -> Private SUPPRESSED -> Channel unresolved -> Retry
    -> Channel SUPPRESSED -> Suppressed

Both protect the same thing the audit called out: that the already-
resolved leg is durably recorded and is never revisited (resent, or
re-suppressed by asking the guard again) once the retry sweep picks
the job back up.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

import app.job_processor as job_processor
from app.job_processor import (
    _make_job_uuid,
    process_job,
    retry_incomplete_notifications,
)
from app.logger import logger


DIRECT_TITLE = "Power BI Dashboard Needed"
DIRECT_DESCRIPTION = "Need a Power BI dashboard built from sales data."


from app.categories.data_analysis.profile import PROFILE

@pytest.fixture()
def isolated_database():
    tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")
    original_path = logger.path

    logger.path = Path(tmp_dir) / "test_logs.db"
    logger.initialize()

    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


def _build_direct_job(url):
    return {
        "title": DIRECT_TITLE,
        "description": DIRECT_DESCRIPTION,
        "raw_text": f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        "source": "Test Channel",
        "url": url,
        "budget": "",
    }


def test_full_lifecycle_pending_sent_failed_retry_complete(
    isolated_database, monkeypatch
):
    log = isolated_database

    private_calls = []
    channel_calls = []
    pending_status_seen = {}

    async def fake_private(**kwargs):
        # By the time the private leg is attempted, the row must
        # already have been durably marked "Pending" -- persisted
        # BEFORE the external side effect, per process_job()'s own
        # comment: "If Loki crashes before the first send, the next
        # recovery can resume the workflow."
        row = await logger.run(logger.get_job, kwargs["job_uuid"])
        pending_status_seen["status"] = row["Notification Status"]

        private_calls.append(kwargs["job_uuid"])
        return True

    channel_attempts = {"n": 0}

    async def fake_channel(**kwargs):
        channel_calls.append(kwargs["job_uuid"])
        channel_attempts["n"] += 1
        return channel_attempts["n"] >= 2  # fails once, then succeeds

    monkeypatch.setattr(job_processor, "send_notification", fake_private)
    monkeypatch.setattr(job_processor, "send_channel_notification", fake_channel)

    job = _build_direct_job(url="https://example.invalid/lifecycle-allow")
    job_uuid = _make_job_uuid("-100801", "lifecycle-1")

    asyncio.run(
        process_job(job=job, job_id="lifecycle-1", identity_source="-100801")
    )

    assert pending_status_seen["status"] == "Pending"

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Telegram: Sent; Telegram Channel: Failed"
    assert private_calls == [job_uuid]
    assert channel_calls == [job_uuid]

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    # The already-successful private leg must never be resent.
    assert private_calls == [job_uuid]
    assert channel_calls == [job_uuid, job_uuid]

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete"

    # A further sweep must be a complete no-op: Complete rows are not
    # even selected by get_incomplete_notification_jobs().
    retried_again = asyncio.run(retry_incomplete_notifications())
    assert retried_again == 0
    assert private_calls == [job_uuid]
    assert channel_calls == [job_uuid, job_uuid]


def test_full_lifecycle_pending_suppressed_retry_suppressed(
    isolated_database, monkeypatch
):
    """
    The suppression analog. The private leg is suppressed by the
    guard on the first pass (recorded durably as "Telegram:
    Suppressed", not "Failed" -- see _was_suppressed_by_guard); the
    channel leg is left unresolved by that same pass (simulating a
    restart between the two legs, exactly like
    _resume_pending_notifications_unlocked is designed to resume).
    The retry sweep must resolve the channel leg using the persisted
    guard decision -- ending in "Suppressed", not "Failed" and not
    re-sent.
    """
    log = isolated_database

    private_calls = []
    channel_calls = []

    async def fake_private(**kwargs):
        private_calls.append(kwargs["job_uuid"])
        return False  # denied

    async def fake_channel(**kwargs):
        channel_calls.append(kwargs["job_uuid"])
        return False  # denied

    monkeypatch.setattr(job_processor, "send_notification", fake_private)
    monkeypatch.setattr(job_processor, "send_channel_notification", fake_channel)

    job_uuid = _make_job_uuid("-100802", "lifecycle-suppress-1")

    from app.filters import keyword_filter
    from app.notification_guard.logger import log_guard_decision

    result = keyword_filter(
        f"{DIRECT_TITLE}\n{DIRECT_DESCRIPTION}",
        title=DIRECT_TITLE,
        profile=PROFILE,
    )
    assert result["notify_directly"] is True

    log.create_job(
        job_uuid=job_uuid,
        job_id="lifecycle-suppress-1",
        source="Test Channel",
        title=DIRECT_TITLE,
        description=DIRECT_DESCRIPTION,
        raw_message=f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        filter_text=f"{DIRECT_TITLE}\n{DIRECT_DESCRIPTION}",
        company="",
        url="https://example.invalid/lifecycle-suppress",
        filter_result=result,
        filter_time_ms=0,
        save=True,
    )
    log.update_job(
        job_uuid,
        final_decision="Accepted",
        decision_reason=result["reason"],
        notification_status="Pending",
        save=True,
    )

    # A guard decision persisted for this job (do_not_notify), as if
    # NotificationGuardIntegration evaluated it once during the
    # private leg's attempt.
    asyncio.run(
        log_guard_decision(
            job_uuid=job_uuid,
            source="Test Channel",
            title=DIRECT_TITLE,
            original_decision="Accepted",
            guard_decision="do_not_notify",
            provider="Test",
            model="test",
            response_time_ms=0,
        )
    )

    # Simulate the outcome of an earlier pass that resolved the
    # private leg (denied + suppressed via the persisted decision)
    # and then stopped -- e.g. a restart before the channel leg was
    # ever attempted. This is exactly the durable row shape
    # _resume_pending_notifications_unlocked is designed to resume.
    log.update_job(
        job_uuid,
        notification_status="Telegram: Suppressed",
        save=True,
    )

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Telegram: Suppressed"
    assert private_calls == []
    assert channel_calls == []

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    assert private_calls == [], (
        "private is already resolved (Suppressed) -- it must never be "
        "attempted at all, let alone resent"
    )
    assert channel_calls == [job_uuid]

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Suppressed"

    retried_again = asyncio.run(retry_incomplete_notifications())
    assert retried_again == 0
