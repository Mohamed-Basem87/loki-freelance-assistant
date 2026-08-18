"""
Regression tests for the Notification Guard retry-correctness fix.

These exercise the REAL interaction between process_job(),
NotificationGuardIntegration (wired up exactly like app.notification_
guard.integration.install() does, just with a scripted guard instead
of the real Groq-backed one), and retry_incomplete_notifications() --
not the guard class in isolation. That boundary (job_processor <->
notification_guard.integration <-> the retry sweep) is exactly where
the audit found the bug: the old in-memory, use-twice-then-evict cache
in NotificationGuardIntegration meant a retry sweep pass -- running
after the cache entry had already been consumed by the original
private+channel pair -- re-asked the guard's provider from scratch,
and could get a different answer the second time.
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
from app.notification_guard.integration import NotificationGuardIntegration
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


# A single "Power BI Dashboard Needed" text is used throughout this
# file (mirrors _seed_notify_ready_job in test_job_processor.py) --
# it's a notify_directly match, so ai_used stays False and every job
# below actually reaches the guard, unlike an LLM-reviewed job.
DIRECT_TITLE = "Power BI Dashboard Needed"
DIRECT_DESCRIPTION = "Need a Power BI dashboard built from sales data."


def _build_direct_job(source="Test Channel", url="https://example.invalid/job"):
    return {
        "title": DIRECT_TITLE,
        "description": DIRECT_DESCRIPTION,
        "raw_text": f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        "source": source,
        "url": url,
        "budget": "",
    }


# needs_gemini text (from tests/test_keyword_filter.py's
# AUTOMATION_CASES), used for the LLM-reviewed bypass test -- must
# route through Gemini, never notify_directly, so ai_used ends up True.
LLM_REVIEWED_TITLE = "Excel Data Entry from Online"
LLM_REVIEWED_DESCRIPTION = (
    "I need someone to do data entry from online sources into an "
    "Excel file. Accuracy matters."
)


def _build_llm_reviewed_job(source="Test Channel"):
    return {
        "title": LLM_REVIEWED_TITLE,
        "description": LLM_REVIEWED_DESCRIPTION,
        "raw_text": f"{LLM_REVIEWED_TITLE}\n\n{LLM_REVIEWED_DESCRIPTION}",
        "source": source,
        "url": "https://example.invalid/llm-job",
        "budget": "",
    }


class ScriptedGuard:
    """
    A minimal stand-in for app.notification_guard.guard.NotificationGuard
    that persists a decision through the exact same durable path the
    real guard uses (log_guard_decision -> the notification_guard
    table), so NotificationGuardIntegration's persisted-decision lookup
    (get_latest_guard_decision) sees exactly what it would see against
    the real guard. `outcomes` is a list consumed one call at a time,
    each either True, False, or "error" (simulating a fail-closed
    provider exception -- persists "error" and denies, like the real
    guard's except branch).
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def allow(self, job, *, original_decision=""):
        self.calls += 1
        outcome = self.outcomes.pop(0)

        if outcome == "error":
            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision="error",
                provider="Scripted",
                model="scripted",
                response_time_ms=0,
                error="simulated provider outage",
            )
            return False

        await log_guard_decision(
            job_uuid=job.get("job_uuid", ""),
            source=job.get("source", ""),
            title=job.get("title", ""),
            original_decision=original_decision,
            guard_decision="notify" if outcome else "do_not_notify",
            provider="Scripted",
            model="scripted",
            response_time_ms=0,
        )
        return outcome


def _wire_guard(monkeypatch, fake_private, fake_channel, outcomes):
    """
    Set app.job_processor.send_notification/send_channel_notification
    to `fake_private`/`fake_channel`, then wrap *those* with a
    NotificationGuardIntegration backed by a ScriptedGuard -- exactly
    the composition app.notification_guard.integration.install()
    builds in production (guard wraps the real notifier functions).
    Order matters: the guard must wrap the fakes, not the other way
    around, or process_job()'s calls would bypass the guard entirely.
    """
    monkeypatch.setattr(job_processor, "send_notification", fake_private)
    monkeypatch.setattr(job_processor, "send_channel_notification", fake_channel)

    guard = ScriptedGuard(outcomes)
    integration = NotificationGuardIntegration(guard)

    monkeypatch.setattr(
        job_processor,
        "send_notification",
        integration.wrap_private(job_processor.send_notification),
    )
    monkeypatch.setattr(
        job_processor,
        "send_channel_notification",
        integration.wrap_channel(job_processor.send_channel_notification),
    )

    return guard


# ------------------------------------------------------------------
# Test A -- Allow survives retry.
# ------------------------------------------------------------------


def test_guard_allow_decision_survives_retry_sweep(isolated_database, monkeypatch):
    log = isolated_database

    calls = {"private": 0, "channel": 0}
    channel_attempts = {"n": 0}

    async def fake_private(**kwargs):
        calls["private"] += 1
        return True

    async def fake_channel(**kwargs):
        calls["channel"] += 1
        channel_attempts["n"] += 1
        # Fails the first time (during process_job()), succeeds on the
        # retry-sweep attempt.
        return channel_attempts["n"] >= 2

    guard = _wire_guard(monkeypatch, fake_private, fake_channel, outcomes=[True])

    job = _build_direct_job()
    job_uuid = _make_job_uuid("-100901", "allow-retry-1")

    asyncio.run(
        process_job(job=job, job_id="allow-retry-1", identity_source="-100901")
    )

    assert guard.calls == 1, "the guard must be evaluated exactly once for both legs"
    assert calls == {"private": 1, "channel": 1}

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Telegram: Sent; Telegram Channel: Failed"

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    assert calls == {"private": 1, "channel": 2}, (
        "the already-successful private leg must never be resent; the "
        "channel leg must be retried"
    )
    assert guard.calls == 1, (
        "the retried channel attempt must reuse the original guard "
        "decision instead of asking the provider again"
    )

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete"


# ------------------------------------------------------------------
# Test B -- Suppression survives retry.
# ------------------------------------------------------------------


def test_guard_suppression_decision_survives_retry_sweep(
    isolated_database, monkeypatch
):
    """
    The private leg is suppressed by the guard (and durably recorded
    as such) before an unrelated crash/restart leaves the channel leg
    unresolved -- exactly the "resume a partially-complete workflow"
    scenario _resume_pending_notifications_unlocked already handles.
    The retry sweep must resolve the channel leg using the SAME
    persisted do_not_notify decision, never asking the guard's
    provider again, and the job must end up "Suppressed", not
    "Failed" or re-sent.
    """
    log = isolated_database

    calls = {"private": 0, "channel": 0}

    async def fake_private(**kwargs):
        calls["private"] += 1
        return True

    async def fake_channel(**kwargs):
        calls["channel"] += 1
        return True

    guard = _wire_guard(monkeypatch, fake_private, fake_channel, outcomes=[False])

    job_uuid = _make_job_uuid("-100902", "suppress-retry-1")

    from app.filters import keyword_filter

    result = keyword_filter(
        f"{DIRECT_TITLE}\n{DIRECT_DESCRIPTION}", title=DIRECT_TITLE
    )
    assert result["notify_directly"] is True

    log.create_job(
        job_uuid=job_uuid,
        job_id="suppress-retry-1",
        source="Test Channel",
        title=DIRECT_TITLE,
        description=DIRECT_DESCRIPTION,
        raw_message=f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        filter_text=f"{DIRECT_TITLE}\n{DIRECT_DESCRIPTION}",
        company="",
        url="https://example.invalid/suppress-retry",
        filter_result=result,
        filter_time_ms=0,
        save=True,
    )

    # Simulate the private leg already having gone through the guard
    # once (denied -> Suppressed) in an earlier pass, with the guard's
    # own persisted decision left behind -- and the channel leg never
    # having been attempted (e.g. the process restarted in between).
    guard_job = {
        "job_uuid": job_uuid,
        "source": "Test Channel",
        "title": DIRECT_TITLE,
        "description": DIRECT_DESCRIPTION,
    }
    denied = asyncio.run(guard.allow(guard_job, original_decision="Accepted"))
    assert denied is False
    assert guard.calls == 1

    log.update_job(
        job_uuid,
        final_decision="Accepted",
        decision_reason=result["reason"],
        notification_status="Telegram: Suppressed",
        save=True,
    )

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    assert calls == {"private": 0, "channel": 0}, (
        "a suppressed leg must never reach the underlying sender"
    )
    assert guard.calls == 1, (
        "the retry must reuse the persisted suppression decision "
        "instead of asking the guard's provider again"
    )

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Suppressed"

    # A later sweep must leave a resolved ("Suppressed") job alone.
    retried_again = asyncio.run(retry_incomplete_notifications())
    assert retried_again == 0
    assert guard.calls == 1


# ------------------------------------------------------------------
# Test C -- Guard error remains distinct from a durable decision.
# ------------------------------------------------------------------


def test_guard_error_is_not_treated_as_a_durable_decision(
    isolated_database, monkeypatch
):
    log = isolated_database

    calls = {"private": 0, "channel": 0}

    async def fake_private(**kwargs):
        calls["private"] += 1
        return True

    async def fake_channel(**kwargs):
        calls["channel"] += 1
        return True

    # First evaluation (private leg) errors; second (channel leg, same
    # invocation -- the persisted "error" is not reusable, so it's
    # evaluated fresh) succeeds with ALLOW.
    guard = _wire_guard(
        monkeypatch, fake_private, fake_channel, outcomes=["error", True]
    )

    job = _build_direct_job(url="https://example.invalid/guard-error")
    job_uuid = _make_job_uuid("-100903", "guard-error-1")

    asyncio.run(
        process_job(job=job, job_id="guard-error-1", identity_source="-100903")
    )

    assert guard.calls == 2, (
        "an errored evaluation must not be reused for the other leg -- "
        "it must be evaluated fresh"
    )
    # Private was denied by the fail-closed error response; channel
    # was allowed by the second (successful) evaluation.
    assert calls == {"private": 0, "channel": 1}

    row = log.get_job(job_uuid)
    # Private failed outright (denied, and "error" is not a genuine
    # do_not_notify, so it's retryable, not "Suppressed").
    assert "Telegram: Failed" in row["Notification Status"]
    assert "Telegram Channel: Sent" in row["Notification Status"]

    # Retry sweep: private is retried. The most recent persisted
    # decision is now "notify" (from the channel's successful
    # evaluation), so the retry reuses THAT -- correctly -- without
    # calling the guard's provider a third time.
    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    assert calls == {"private": 1, "channel": 1}
    assert guard.calls == 2, (
        "the retry must reuse the persisted 'notify' decision, not "
        "call the guard's provider again"
    )

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete"


# ------------------------------------------------------------------
# Test D -- LLM-reviewed jobs bypass the guard, including on retry.
# ------------------------------------------------------------------


def test_llm_reviewed_job_bypasses_guard_on_retry(isolated_database, monkeypatch):
    log = isolated_database

    calls = {"private": 0, "channel": 0}
    channel_attempts = {"n": 0}

    async def fake_private(**kwargs):
        calls["private"] += 1
        return True

    async def fake_channel(**kwargs):
        calls["channel"] += 1
        channel_attempts["n"] += 1
        return channel_attempts["n"] >= 2

    # No outcomes are ever consumed if the bypass works correctly --
    # an empty list makes any accidental guard.allow() call raise
    # IndexError, which is exactly the failure signal we want.
    guard = _wire_guard(monkeypatch, fake_private, fake_channel, outcomes=[])

    def fake_evaluate_job(filter_text, result):
        # evaluate_job is called via asyncio.to_thread(...), i.e.
        # synchronously in a worker thread -- it must be a plain
        # function, not a coroutine function.
        return {
            "decision": "accept",
            "reason": "LLM accepted",
            "confidence": 0.9,
        }

    monkeypatch.setattr(job_processor, "evaluate_job", fake_evaluate_job)

    job = _build_llm_reviewed_job()
    job_uuid = _make_job_uuid("-100904", "llm-bypass-1")

    asyncio.run(
        process_job(job=job, job_id="llm-bypass-1", identity_source="-100904")
    )

    assert guard.calls == 0
    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Telegram: Sent; Telegram Channel: Failed"

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 1
    assert guard.calls == 0, "retrying an LLM-reviewed job must never invoke the guard"
    assert calls == {"private": 1, "channel": 2}

    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete"
