"""Regression tests for the guard-error retry fix (issue 1).

guards the allow()'s fail-closed False for a guarded job: the durable
row must stay "Pending" on a transient guard "error" (so the next
get_incomplete_notification_jobs() sweep re-evaluates the guard fresh),
and only a durable "do_not_notify" decision may terminate the job as
"Suppressed". Before the fix, both cases were written as "Suppressed",
permanently dropping a job on a provider outage.
"""
import asyncio

import pytest

import app.services.job_processor as jp
from app.adapters.repositories.postgres import COLUMN_MAP

JOB_UUID = "guard-error-1"
JOB_UUID2 = "guard-suppress-1"
JOB_UUID3 = "guard-llm-1"
JOB_UUID4 = "guard-reclass-1"
JOB_UUID5 = "guard-reuse-1"
JOB_UUID6 = "guard-disabled-1"


def _row(job_uuid, method="keyword_direct", **overrides):
    row = {
        "Job UUID": job_uuid,
        "Title": "Power BI Dashboard Needed",
        "Description": "Need a Power BI dashboard built from sales data.",
        "Source": "Test Channel",
        "URL": "https://example.invalid/job",
        "Short URL": "",
        "Decision": "notify_directly",
        "Decision Reason": "direct",
        "Categories": "Power BI",
        "Category ID": "data_analysis",
        "Category Selection Method": method,
        "Needs Gemini": False,
        "Final Decision": "Accepted",
        "Notification Status": "Pending",
    }
    row.update(overrides)
    return row


class _FakeRepo:
    """In-memory JobRepository: durable rows + guard decisions."""

    def __init__(self):
        self.rows = {}
        self.decisions = {}
        self.guard_categories = {}
        self.published = []

    def seed(self, row):
        self.rows[row["Job UUID"]] = dict(row)

    def seed_decision(self, job_uuid, decision, guard_category=None):
        self.decisions[job_uuid] = decision
        if guard_category is not None:
            self.guard_categories[job_uuid] = guard_category

    async def get_job(self, job_uuid):
        row = self.rows.get(job_uuid)
        return dict(row) if row is not None else None

    async def update_job(self, job_uuid, save=True, **fields):
        # Mirror PostgresRepository._sync_update_job: translate the
        # snake_case update field names onto the header-case schema keys
        # and flatten lists the way the adapter's column values expect.
        row = self.rows.setdefault(job_uuid, {"Job UUID": job_uuid})
        for key, value in fields.items():
            if isinstance(value, list):
                value = ", ".join(value)
            row[COLUMN_MAP.get(key, key)] = value
        return True

    async def get_latest_guard_decision(self, job_uuid):
        return self.decisions.get(job_uuid)

    async def get_latest_guard_decision_with_category(self, job_uuid):
        return self.decisions.get(job_uuid), self.guard_categories.get(job_uuid)

    async def get_incomplete_notification_jobs(self):
        return [
            dict(r) for r in self.rows.values()
            if (r.get("Notification Status") or "")
            not in ("", "Complete", "Suppressed")
        ]


class _Recorder:
    async def publish(self, event):
        pass


class _ScriptedGuard:
    """Produces the same durable decision the real
    NotificationGuardIntegration.allow() resolves to: on "error" it
    records an "error" decision (not reusable) and denies; otherwise it
    records notify/do_not_notify and allows/denies accordingly. An empty
    outcomes list makes any accidental evaluation raise IndexError."""

    def __init__(self, repo, outcomes):
        self.repo = repo
        self.outcomes = list(outcomes)
        self.calls = []

    async def allow(self, payload):
        outcome = self.outcomes.pop(0)
        self.calls.append(outcome)
        job_uuid = payload.get("job_uuid", "")
        if outcome == "error":
            self.repo.decisions[job_uuid] = "error"
            return False
        if outcome:
            self.repo.decisions[job_uuid] = "notify"
            return True
        self.repo.decisions[job_uuid] = "do_not_notify"
        return False


def _wire(monkeypatch, repo, guard):
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", _Recorder())
    monkeypatch.setattr(jp, "guard_allow", guard.allow)


# ------------------------------------------------------------------
# Test A -- a transient guard error leaves the job Pending (retryable),
# and the retry sweep re-evaluates the guard fresh and publishes.
# ------------------------------------------------------------------


def test_guard_error_leaves_pending_then_retry_completes(monkeypatch):
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID))
    guard = _ScriptedGuard(repo, outcomes=["error", True])
    _wire(monkeypatch, repo, guard)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID, repo.rows[JOB_UUID]
    ))

    assert guard.calls == ["error"]
    assert repo.rows[JOB_UUID]["Notification Status"] == "Pending", (
        "a fail-closed guard error must NOT be treated as a durable "
        "suppression -- the row stays retryable"
    )

    retried = asyncio.run(jp.retry_incomplete_notifications())

    assert retried == 1
    assert guard.calls == ["error", True], (
        "the retry must re-evaluate the guard since 'error' is not durable"
    )
    assert repo.rows[JOB_UUID]["Notification Status"] == "Complete"


# ------------------------------------------------------------------
# Test B -- a durable do_not_notify terminates as Suppressed and is
# never re-evaluated or re-published.
# ------------------------------------------------------------------


def test_durable_do_not_notify_suppresses_and_stays_suppressed(monkeypatch):
    # Non-tie-break rows keep the original terminal contract: a durable
    # guard rejection always ends in "Suppressed" and is never
    # re-evaluated. (Only multi-candidate tie-break picks go down the
    # fallback path instead -- see
    # test_guard_do_not_notify_keyword_direct_falls_back.)
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID2, method="arbitration_only"))
    guard = _ScriptedGuard(repo, outcomes=[False])
    _wire(monkeypatch, repo, guard)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID2, repo.rows[JOB_UUID2]
    ))

    assert guard.calls == [False]
    assert repo.rows[JOB_UUID2]["Notification Status"] == "Suppressed"

    # A later sweep must leave a resolved ("Suppressed") job alone.
    retried = asyncio.run(jp.retry_incomplete_notifications())
    assert retried == 0
    assert guard.calls == [False], "suppressed job must never be re-evaluated"


# ------------------------------------------------------------------
# Test B2 -- a durable do_not_notify on a MULTI-CANDIDATE tie-break
# pick ("keyword_direct_tiebreak") is NOT terminal: it is re-routed to
# one LLM arbitration pass by resetting the durable row to a pending
# classification with the guard-fallback marker. A clean single-
# candidate keyword-direct pick that the guard rejects stays
# terminally suppressed (Test B3).
# ------------------------------------------------------------------


def test_guard_do_not_notify_keyword_direct_falls_back(monkeypatch):
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID2, method="keyword_direct_tiebreak"))
    guard = _ScriptedGuard(repo, outcomes=[False])
    _wire(monkeypatch, repo, guard)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID2, repo.rows[JOB_UUID2]
    ))

    row = repo.rows[JOB_UUID2]
    assert guard.calls == [False]
    assert row["Final Decision"] == "Pending", (
        "a guard-rejected multi-candidate tie-break pick must go back to "
        "pending classification instead of being terminally suppressed"
    )
    assert row["Needs Gemini"] is True
    assert str(row["Decision Reason"] or "").startswith("Guard Fallback")
    assert (row.get("Notification Status") or "") == "", (
        "notification status must be cleared so the notification sweep "
        "and the classification retry sweep do not race on the same job"
    )

    # The cleared notification status means the notification retry sweep
    # must NOT re-evaluate the guard for this job.
    retried = asyncio.run(jp.retry_incomplete_notifications())
    assert retried == 0


def test_guard_do_not_notify_single_keyword_direct_suppresses(monkeypatch):
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID6, method="keyword_direct"))
    guard = _ScriptedGuard(repo, outcomes=[False])
    _wire(monkeypatch, repo, guard)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID6, repo.rows[JOB_UUID6]
    ))

    assert guard.calls == [False]
    assert repo.rows[JOB_UUID6]["Notification Status"] == "Suppressed", (
        "a clean single-candidate keyword-direct pick that the guard "
        "rejects must be terminally suppressed -- only multi-candidate "
        "tie-break picks get the fallback rescue"
    )


# ------------------------------------------------------------------
# Test C -- LLM-reviewed jobs bypass the guard entirely.
# ------------------------------------------------------------------


def test_llm_reviewed_job_bypasses_guard(monkeypatch):
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID3, method="llm"))
    # Empty outcomes: any accidental evaluation raises IndexError.
    guard = _ScriptedGuard(repo, outcomes=[])
    _wire(monkeypatch, repo, guard)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID3, repo.rows[JOB_UUID3]
    ))

    assert guard.calls == [], "the guard must never be evaluated for an LLM-reviewed job"
    assert repo.rows[JOB_UUID3]["Notification Status"] == "Complete"


# ------------------------------------------------------------------
# Test D -- a durable full_stack reclassification is what gets
# published, never the stale caller snapshot (issues 5/6 through the
# real resume path).
# ------------------------------------------------------------------


def test_guard_reclassification_published_from_fresh_durable_row(monkeypatch):
    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID4))
    guard = _ScriptedGuard(repo, outcomes=[True])
    _wire(monkeypatch, repo, guard)

    events = []

    class _Recorder:
        async def publish(self, event):
            events.append(dict(event))

    monkeypatch.setattr(jp, "stream_publisher", _Recorder())

    async def reclassify_resolver(job_uuid, row, category_id):
        # Mirrors NotificationGuardIntegration.resolve_category: persists
        # Category ID + Categories + Category Selection Method together.
        await repo.update_job(
            job_uuid,
            category_id="full_stack",
            category_selection_method="llm",
            categories=["Full Stack"],
        )
        return "full_stack"

    monkeypatch.setattr(jp, "resolver", reclassify_resolver)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID4, repo.rows[JOB_UUID4]
    ))

    assert guard.calls == [True]
    assert repo.rows[JOB_UUID4]["Notification Status"] == "Complete"
    assert len(events) == 1
    event = events[0]
    assert event["Category ID"] == "full_stack"
    assert event["Categories"] == "Full Stack"
    assert event["Category Selection Method"] == "llm"
    assert event["Job UUID"] == JOB_UUID4


# ------------------------------------------------------------------
# Test E -- a durable "notify" decision is reused forever through the
# REAL NotificationGuardIntegration, so the guard's provider is never
# re-evaluated once the job is resolved (issue 2 durability contract).
# ------------------------------------------------------------------


def test_durable_notify_decision_is_reused_across_retry_sweeps(monkeypatch):
    from app.notification_guard.integration import NotificationGuardIntegration

    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID5))
    repo.seed_decision(JOB_UUID5, "notify")

    class _NeverCalledGuard:
        # Any accidental fresh evaluation raises: the persisted
        # "notify" decision must be the only thing consulted.
        async def decide(self, *a, **kw):
            raise AssertionError("guard provider must not be re-evaluated")

    integration = NotificationGuardIntegration(_NeverCalledGuard(), repository=repo)

    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", _Recorder())
    monkeypatch.setattr(jp, "guard_allow", integration.allow)
    monkeypatch.setattr(jp, "resolver", integration.resolve_category)

    # The initial resume reuses the pre-existing durable decision
    # without ever asking the provider, and publishes.
    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID5, repo.rows[JOB_UUID5]
    ))
    assert repo.rows[JOB_UUID5]["Notification Status"] == "Complete"

    # A later retry sweep sees no remaining Pending rows and re-evaluates
    # nothing for this job.
    assert asyncio.run(jp.retry_incomplete_notifications()) == 0
    assert repo.rows[JOB_UUID5]["Notification Status"] == "Complete"


# ------------------------------------------------------------------
# Test F -- a disabled guard is a transparent no-op: it must never let a
# stale durable "do_not_notify" (from before the guard was disabled) turn
# an otherwise deliverable job into a suppression.
# ------------------------------------------------------------------


def test_disabled_guard_is_transparent_despite_stale_do_not_notify(monkeypatch):
    from app.notification_guard.integration import NotificationGuardIntegration

    repo = _FakeRepo()
    repo.seed(_row(JOB_UUID6))
    repo.seed_decision(JOB_UUID6, "do_not_notify")

    class _DisabledGuard:
        enabled = False

        async def decide(self, *a, **kw):
            raise AssertionError("a disabled guard must never evaluate")

    integration = NotificationGuardIntegration(_DisabledGuard(), repository=repo)

    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", _Recorder())
    monkeypatch.setattr(jp, "guard_allow", integration.allow)
    monkeypatch.setattr(jp, "resolver", integration.resolve_category)

    asyncio.run(jp._resume_pending_notifications_unlocked(
        JOB_UUID6, repo.rows[JOB_UUID6]
    ))

    assert repo.rows[JOB_UUID6]["Notification Status"] == "Complete", (
        "a disabled guard must never suppress a deliverable job"
    )