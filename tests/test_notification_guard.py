"""
NotificationGuardIntegration is now backed by the durable
`notification_guard` table (see app.logger / the retry-correctness
fix in app.notification_guard.integration) rather than an in-memory,
use-twice-then-evict cache, so every test in this file needs a real
(isolated, temporary) database -- exactly like test_job_processor.py
and test_pipeline.py -- for `_allow()`'s
`get_latest_guard_decision` lookup to have something to read.

FakeGuard.allow() persists a decision the same way the real
NotificationGuard.allow() does (via log_guard_decision), so these
tests exercise the actual reuse-from-persistence path instead of
special-casing the fake.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.logger import logger
from app.notification_guard.integration import (
    NotificationGuardIntegration,
)
from app.notification_guard.logger import log_guard_decision


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


class FakeGuard:

    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = 0
        self.jobs = []

    async def allow(self, job, *, original_decision=""):
        self.calls += 1
        self.jobs.append({
            "job": job,
            "original_decision": original_decision,
        })

        await log_guard_decision(
            job_uuid=job.get("job_uuid", ""),
            source=job.get("source", ""),
            title=job.get("title", ""),
            original_decision=original_decision,
            guard_decision="notify" if self.allowed else "do_not_notify",
            provider="Fake",
            model="fake-model",
            response_time_ms=0,
        )

        return self.allowed


async def fake_private(**kwargs):
    return True


async def fake_channel(**kwargs):
    return True


async def run_guard_test(
    allowed,
    ai_used=False,
):

    guard = FakeGuard(allowed)

    integration = NotificationGuardIntegration(guard)

    private = integration.wrap_private(fake_private)
    channel = integration.wrap_channel(fake_channel)

    kwargs = {
        "job_uuid": "job-1",
        "title": "Power BI dashboard",
        "description": "Build a sales dashboard from Excel data.",
        "source": "test",
        "decision": "Accepted",
        "ai_used": ai_used,
    }

    private_result = await private(**kwargs)
    channel_result = await channel(**kwargs)

    return (
        guard,
        private_result,
        channel_result,
    )


def test_direct_notification_allowed(isolated_database):

    guard, private, channel = asyncio.run(
        run_guard_test(True)
    )

    assert guard.calls == 1
    assert private is True
    assert channel is True

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"
    assert guard.jobs[0]["original_decision"] == "Accepted"


def test_direct_notification_rejected(isolated_database):

    guard, private, channel = asyncio.run(
        run_guard_test(False)
    )

    assert guard.calls == 1
    assert private is False
    assert channel is False

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"


def test_llm_review_bypasses_guard(isolated_database):

    guard, private, channel = asyncio.run(
        run_guard_test(
            False,
            ai_used=True,
        )
    )

    assert guard.calls == 0
    assert private is True
    assert channel is True
