"""Business Node notification recovery / retry-sweep tests.

Restores the durable notification state-machine coverage from the main
branch (test_notification_state_machine.py / test_job_processor.py
recovery sections) against the refactored Postgres + Redis-Stream
pipeline:

    Pending -> publish -> Complete
    Pending (stuck crash / transient failure) -> retry sweep -> publish -> Complete
    (Complete|Suppressed) -> never re-touched by a sweep
    live process_job() + retry sweep racing the same job -> exactly one publish

The business node's responsibility ends at a durable Complete/Suppressed
write; fan-out and delivery moved to the stream consumer, so "pending"
is the single recoverable state and the sweep republishing a Pending row
is the crash-safety mechanism (publish happens before the Complete
write; a crash in that window leaves the row Pending and resweepable).
"""
import asyncio

import pytest

import app.services.job_processor as jp
import app.services.state as state_module
from app.adapters.state.json import JsonStateStore
from app.services.state import StateManager

from tests._business_node_fakes import (
    MemRepository,
    RecordingPublisher,
    PassThroughShortener,
    allow_all,
    noop_resolver,
)

DIRECT_TITLE = "Power BI Dashboard Needed"
DIRECT_DESCRIPTION = "Need a Power BI dashboard built from sales data."
REJECT_TEXT = "Need someone to create SQL queries for a reporting system."


@pytest.fixture()
def isolated_dedup(tmp_path, monkeypatch):
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)
    manager = StateManager()
    manager.load()
    return JsonStateStore(manager)


@pytest.fixture()
def pipeline(isolated_dedup, monkeypatch):
    repo = MemRepository()
    publisher = RecordingPublisher()
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "dedup", isolated_dedup)
    monkeypatch.setattr(jp, "stream_publisher", publisher)
    monkeypatch.setattr(jp, "resolver", noop_resolver)
    monkeypatch.setattr(jp, "guard_allow", allow_all)
    monkeypatch.setattr(jp, "url_shortener", PassThroughShortener())
    return repo, publisher


def _direct_job(url="https://example.invalid/recovery"):
    return {
        "title": DIRECT_TITLE,
        "description": DIRECT_DESCRIPTION,
        "raw_text": f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        "source": "Test Channel",
        "url": url,
        "budget": "",
    }


def _notify_ready_row(uuid, status="Pending"):
    return {
        "Job UUID": uuid,
        "Job ID": "seed",
        "Identity Source": "Test Channel",
        "Source": "Test Channel",
        "Title": DIRECT_TITLE,
        "Description": DIRECT_DESCRIPTION,
        "Raw Message": f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        "Company": "",
        "URL": "https://example.invalid/seeded",
        "Short URL": "",
        "Decision": "notify_directly",
        "Decision Reason": "direct",
        "Categories": "Power BI",
        "Category ID": "data_analysis",
        "Category Selection Method": "keyword_direct",
        "Needs Gemini": False,
        "Final Decision": "Accepted",
        "Notification Status": status,
    }


def test_full_lifecycle_pending_publish_complete(pipeline):
    repo, publisher = pipeline
    job_uuid = jp._make_job_uuid("-100801", "lifecycle-1")

    asyncio.run(jp.process_job(_direct_job(), "lifecycle-1", "-100801"))

    assert len(publisher.events) == 1
    assert publisher.events[0]["Job UUID"] == job_uuid
    row = repo.rows[job_uuid]
    assert row["Final Decision"] == "Accepted"
    assert row["Notification Status"] == "Complete"

    # A further sweep must be a complete no-op: Complete rows are not
    # even selected by get_incomplete_notification_jobs().
    assert asyncio.run(jp.retry_incomplete_notifications()) == 0
    assert len(publisher.events) == 1


def test_retry_sweep_resumes_a_pending_row_and_publishes(pipeline):
    repo, publisher = pipeline
    job_uuid = jp._make_job_uuid("-100777", "retry-1")
    repo.seed(_notify_ready_row(job_uuid, status="Pending"))

    retried = asyncio.run(jp.retry_incomplete_notifications())

    assert retried == 1
    assert len(publisher.events) == 1
    assert repo.rows[job_uuid]["Notification Status"] == "Complete"

    # A further sweep must be a no-op.
    assert asyncio.run(jp.retry_incomplete_notifications()) == 0
    assert len(publisher.events) == 1


def test_retry_sweep_cannot_duplicate_a_live_notification(
    pipeline, monkeypatch
):
    """A retry sweep and a live process_job() may overlap: the per-job
    notification lock serializes the two paths and the second waiter
    re-reads the durable row after acquiring the lock, so the same job
    is never published twice."""
    repo, _ = pipeline
    job_uuid = jp._make_job_uuid("-100444", "race-1")

    release = asyncio.Event()
    publisher = RecordingPublisher(block_on=release)
    monkeypatch.setattr(jp, "stream_publisher", publisher)

    async def scenario():
        live = asyncio.create_task(
            jp.process_job(_direct_job(), "race-1", "-100444")
        )

        # The live process_job() holds the per-job notification lock
        # with its first publish in flight.
        await publisher.started.wait()

        retry = asyncio.create_task(jp.retry_incomplete_notifications())
        await asyncio.sleep(0)

        release.set()
        await live
        await retry

    asyncio.run(scenario())

    assert len(publisher.events) == 1, (
        "the sweep must not duplicate a notification the live caller is "
        "still delivering"
    )
    assert repo.rows[job_uuid]["Notification Status"] == "Complete"


def test_retry_sweep_ignores_complete_and_suppressed_jobs(pipeline):
    repo, publisher = pipeline

    complete_uuid = jp._make_job_uuid("-100001", "done-1")
    suppressed_uuid = jp._make_job_uuid("-100002", "done-2")
    repo.seed(_notify_ready_row(complete_uuid, status="Complete"))
    repo.seed(_notify_ready_row(suppressed_uuid, status="Suppressed"))

    retried = asyncio.run(jp.retry_incomplete_notifications())

    assert retried == 0
    assert publisher.events == []


def test_accepted_job_without_notification_status_resumes_notification(pipeline):
    """Crash window: Final Decision was durably recorded as Accepted but
    the process died before Notification Status was set to Pending.
    Reprocessing must resume the notification workflow."""
    repo, publisher = pipeline
    job = _direct_job()
    job_uuid = jp._make_job_uuid("-100779", "accepted-recovery-001")

    row = _notify_ready_row(job_uuid, status="")
    repo.seed(row)

    asyncio.run(jp.process_job(job, "accepted-recovery-001", "-100779"))

    assert repo.rows[job_uuid]["Notification Status"] == "Complete"
    assert len(publisher.events) == 1
    assert publisher.events[0]["Job UUID"] == job_uuid


def test_incomplete_job_row_is_resumed_after_processing_crash(pipeline):
    """Crash window P1-A: the durable row was created but Final Decision
    was never recorded. Reprocessing the same identity must finish
    classification instead of silently returning and leaving the row
    permanently undecided."""
    repo, _ = pipeline
    job_id = "crashed-001"
    identity_source = "-100777"
    job_uuid = jp._make_job_uuid(identity_source, job_id)

    repo.seed(
        {
            "Job UUID": job_uuid,
            "Job ID": job_id,
            "Identity Source": identity_source,
            "Source": "Recovery Test Channel",
            "Title": REJECT_TEXT,
            "Description": "",
            "Raw Message": REJECT_TEXT,
            "Company": "",
            "URL": "",
            "Decision": "reject",
            "Decision Reason": "insufficient_signal",
            "Categories": "",
            "Negative Categories": "",
            "Final Decision": "",
            "Notification Status": "",
            "Category ID": None,
            "Category Selection Method": "",
        }
    )

    asyncio.run(
        jp.process_job(
            {
                "title": REJECT_TEXT,
                "description": "",
                "raw_text": REJECT_TEXT,
                "source": "Recovery Test Channel",
                "url": "",
                "budget": "",
            },
            job_id,
            identity_source,
        )
    )

    recovered = repo.rows[job_uuid]
    assert recovered["Final Decision"] == "Rejected"
    # The classifier returns every enabled category here; data_analysis
    # (the rejection-reason representative) rejects with its own reason,
    # which the refactored pipeline preserves verbatim.
    assert recovered["Decision Reason"] == "insufficient_signal"
    assert not (recovered["Notification Status"] or ""), (
        "a rejected job has no notification workflow to resume"
    )
    assert len(repo.rows) == 1