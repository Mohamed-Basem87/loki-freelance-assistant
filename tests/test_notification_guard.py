"""
NotificationGuardIntegration is now backed by the durable
`notification_guard` table (see app.logger / the retry-correctness
fix in app.notification_guard.integration) rather than an in-memory,
use-twice-then-evict cache, so every test in this file needs a real
(isolated, temporary) database -- exactly like test_job_processor.py
and test_pipeline.py -- for `_allow()`'s
`get_latest_guard_decision` lookup to have something to read.

FakeGuard.decide() persists a decision the same way the real
NotificationGuard.decide() does (via log_guard_decision), so these
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

    def __init__(self, allowed, reclassify_to=None):
        self.allowed = allowed
        # None means "keep whatever category resolve_category() was
        # asked to resolve"; set to e.g. "full_stack" to simulate the
        # guard reclassifying the job.
        self.reclassify_to = reclassify_to
        self.calls = 0
        self.jobs = []

    async def decide(self, job, *, original_decision="", category_id=""):
        self.calls += 1
        resolved_category = self.reclassify_to or category_id

        self.jobs.append({
            "job": job,
            "original_decision": original_decision,
            "category_id": category_id,
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
            guard_category=resolved_category if self.allowed else "",
        )

        return {
            "allowed": self.allowed,
            "category_id": resolved_category if self.allowed else category_id,
        }


async def fake_private(**kwargs):
    return True


async def fake_channel(**kwargs):
    return True


async def run_guard_test(
    allowed,
    ai_used=False,
):
    """
    Mirrors the real pipeline's call order (see
    app.job_processor._resume_pending_notifications_unlocked):
    resolve_category() runs once, up front, and only afterward do the
    wrapped private/routing calls consult the persisted result. Tests
    exercising wrap_private in isolation therefore have to drive
    resolve_category() first, exactly like production does, rather
    than expecting wrap_private to trigger evaluation itself.
    """

    guard = FakeGuard(allowed)

    integration = NotificationGuardIntegration(guard)

    row = {
        "Source": "test",
        "Title": "Power BI dashboard",
        "Description": "Build a sales dashboard from Excel data.",
        "Final Decision": "Accepted",
        "Needs Gemini": ai_used,
    }

    resolved_category_id = await integration.resolve_category(
        "job-1", row, "data_analysis"
    )

    private = integration.wrap_private(fake_private)

    kwargs = {
        "job_uuid": "job-1",
        "title": "Power BI dashboard",
        "description": "Build a sales dashboard from Excel data.",
        "source": "test",
        "decision": "Accepted",
        "ai_used": ai_used,
        "category_id": resolved_category_id,
    }

    private_result = await private(**kwargs)

    return (
        guard,
        private_result,
    )


def test_direct_notification_allowed(isolated_database):

    guard, private = asyncio.run(
        run_guard_test(True)
    )

    assert guard.calls == 1
    assert private is True

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"
    assert guard.jobs[0]["original_decision"] == "Accepted"


def test_direct_notification_rejected(isolated_database):

    guard, private = asyncio.run(
        run_guard_test(False)
    )

    assert guard.calls == 1
    assert private is False

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"


def test_llm_review_bypasses_guard(isolated_database):

    guard, private = asyncio.run(
        run_guard_test(
            False,
            ai_used=True,
        )
    )

    assert guard.calls == 0
    assert private is True
