"""Regression test for audit finding: notification state consistency.

job_processor.py used to keep its own "Telegram: Sent/Failed" status on
the same "Notification Status" column that NotificationService.send()
uses for its authoritative per-sink state ("Sink:<id>=Sent/Failed").
job_processor rewrote that column from a stale snapshot taken *before*
calling send_notification(), which clobbered whatever per-sink state
had just been persisted. The next retry pass would then see neither
format's success marker and resend to a sink that had already
succeeded.

This test exercises the real NotificationService (not a monkeypatched
stand-in for send_notification) with two sinks so the interaction
between job_processor and the notification service is genuinely
covered end-to-end, matching the audit's own test case:

    Sink A succeeds
    Sink B fails
    process continues/retries
    Sink A must NOT be resent
    Sink B must retry
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
from app.notifier import NotificationService


DIRECT_TITLE = "Power BI Dashboard Needed"
DIRECT_DESCRIPTION = "Need a Power BI dashboard built from sales data."


class _FakeSink:
    def __init__(self, sink_id, outcomes):
        self.id = sink_id
        self._outcomes = list(outcomes)
        self.calls = 0

    async def send(self, **kwargs):
        self.calls += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return True


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


def test_successful_sink_is_not_resent_when_another_sink_fails_and_retries(
    isolated_database, monkeypatch
):
    log = isolated_database

    sink_a = _FakeSink("sink_a", outcomes=[True])
    sink_b = _FakeSink("sink_b", outcomes=[False, True])

    service = NotificationService(sinks=[sink_a, sink_b], repository=log)
    monkeypatch.setattr(job_processor, "send_notification", service.send)

    job = _build_direct_job(url="https://example.invalid/multi-sink-1")
    job_uuid = _make_job_uuid("-100950", "multi-sink-1")

    asyncio.run(
        process_job(job=job, job_id="multi-sink-1", identity_source="-100950")
    )

    assert sink_a.calls == 1, "sink A must be attempted once on the first pass"
    assert sink_b.calls == 1, "sink B must be attempted once on the first pass"

    row = log.get_job(job_uuid)
    assert row["Notification Status"] != "Complete", (
        "the job cannot be Complete while sink B is still failed"
    )
    assert "Sink:sink_a=Sent" in row["Notification Status"], (
        "sink A's success must be durably recorded, not clobbered by "
        "job_processor's own status write"
    )
    assert "Sink:sink_b=Failed" in row["Notification Status"]

    retried = asyncio.run(retry_incomplete_notifications())
    assert retried == 1

    assert sink_a.calls == 1, (
        "a retry sweep caused by sink B's failure must NOT resend to "
        "sink A, which already succeeded"
    )
    assert sink_b.calls == 2, "sink B must be retried"

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete", (
        "once every sink has resolved, the job-level rollup must be "
        "the single terminal status"
    )

    # A further sweep must be a complete no-op.
    retried_again = asyncio.run(retry_incomplete_notifications())
    assert retried_again == 0
    assert sink_a.calls == 1
    assert sink_b.calls == 2

