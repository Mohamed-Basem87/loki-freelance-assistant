"""Regression tests for the Redis stream DTO contract.

Covers issues 3/4/5/6 from the refactor audit: the stream event must be
a stable, minimal, self-contained DTO (not the whole Postgres row),
with Job UUID first and mandatory as the consumer's idempotency key;
the DTO must always be built from the freshly-read durable row inside
the single publish choke point (_publish_and_mark_complete), so a
stale caller snapshot can never ship stale Categories/Category
Selection Method (the guard's full_stack reclassification) and a
replayed event is byte-identical to the initial publish.
"""
import asyncio

import pytest

import app.services.job_processor as jp


def _full_row(job_uuid="job-abc"):
    return {
        "Timestamp": "2026-01-01T00:00:00+00:00",
        "Job UUID": job_uuid,
        "Job ID": "j1",
        "Identity Source": "Test Channel",
        "Source": "Test Channel",
        "Title": "Power BI Dashboard",
        "Description": "Need a Power BI dashboard built from sales data.",
        "Raw Message": "Power BI Dashboard\n\nNeed a dashboard.",
        "Company": "ACME",
        "URL": "https://example.invalid/job",
        "Short URL": "https://s.example/x",
        "Decision Reason": "direct match",
        "Categories": "Power BI",
        "Category ID": "data_analysis",
        "Category Selection Method": "keyword_direct",
        "Notification Status": "Pending",
        "Final Decision": "Accepted",
        "Core Positive Hit Count": 3,
        "Supporting Positive Weight": 2.5,
    }


class _Repo:
    def __init__(self, durable):
        self.durable = durable
        self.updates = []

    async def get_job(self, job_uuid):
        return dict(self.durable) if self.durable is not None else None

    async def update_job(self, job_uuid, save=True, **fields):
        self.updates.append(fields)
        return True


class _Recorder:
    def __init__(self, raise_on_publish=False):
        self.events = []
        self.raise_on_publish = raise_on_publish

    async def publish(self, event):
        if self.raise_on_publish:
            raise RuntimeError("publish failed")
        self.events.append(dict(event))


# ------------------------------------------------------------------
# DTO contract (issues 3 & 4): exactly the renderer's fields, ordered,
# no internal/audit columns leaked into Redis.
# ------------------------------------------------------------------


def test_dto_exact_field_set_and_order():
    event = jp.build_notification_event(_full_row())

    assert list(event) == list(jp.NOTIFICATION_DTO_FIELDS)
    assert list(event)[0] == "Job UUID", "Job UUID must be the first field"
    assert set(event) == {
        "Job UUID",
        "Title",
        "Description",
        "Source",
        "URL",
        "Short URL",
        "Decision Reason",
        "Categories",
        "Category ID",
        "Category Selection Method",
    }
    # Internal/audit columns must never leak into the stream.
    for leaked in ("Raw Message", "Notification Status", "Final Decision",
                   "Company", "Timestamp", "Job ID", "Core Positive Hit Count"):
        assert leaked not in event, f"internal column {leaked!r} leaked into the DTO"
    assert {event[k] for k in event} == {
        "job-abc", "Power BI Dashboard",
        "Need a Power BI dashboard built from sales data.",
        "Test Channel", "https://example.invalid/job", "https://s.example/x",
        "direct match", "Power BI", "data_analysis", "keyword_direct",
    }


def test_dto_requires_job_uuid():
    with pytest.raises(ValueError):
        jp.build_notification_event({"Title": "no id"})


def test_dto_missing_fields_become_empty_strings_preserving_order():
    event = jp.build_notification_event({"Job UUID": "z"})
    assert list(event) == list(jp.NOTIFICATION_DTO_FIELDS)
    for field in jp.NOTIFICATION_DTO_FIELDS:
        assert event[field] == ("z" if field == "Job UUID" else "")


# ------------------------------------------------------------------
# Publish choke point (issues 5 & 6): the event is built from the
# FRESH durable row, publish happens before the Complete write, and a
# publish failure leaves the row Pending (republishable).
# ------------------------------------------------------------------


def test_publish_event_built_from_fresh_durable_row_not_caller_snapshot(
    monkeypatch,
):
    # The guard reclassified this job to full_stack AFTER the caller's
    # snapshot was taken: the durable row has the new category/categories/
    # method, the snapshot only picked up "Category ID" (the old local
    # update in _resume_pending_notifications_unlocked).
    durable = _full_row("job-fresh")
    durable["Category ID"] = "full_stack"
    durable["Categories"] = "Full Stack"
    durable["Category Selection Method"] = "llm"

    stale = _full_row("job-fresh")

    repo = _Repo(durable)
    recorder = _Recorder()
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", recorder)

    asyncio.run(jp._publish_and_mark_complete("job-fresh", stale))

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event["Category ID"] == "full_stack"
    assert event["Categories"] == "Full Stack"
    assert event["Category Selection Method"] == "llm"
    assert event["Job UUID"] == "job-fresh"
    assert repo.updates == [{"notification_status": "Complete"}]


def test_publish_happens_before_complete_write(monkeypatch):
    order = []

    class _OrderedRecorder:
        async def publish(self, event):
            order.append("publish")

    class _OrderedRepo:
        async def get_job(self, job_uuid):
            return dict(_full_row("order-1"))

        async def update_job(self, job_uuid, save=True, **fields):
            order.append("complete")

    monkeypatch.setattr(jp, "logger", _OrderedRepo())
    monkeypatch.setattr(jp, "stream_publisher", _OrderedRecorder())

    asyncio.run(jp._publish_and_mark_complete("order-1", {}))

    assert order == ["publish", "complete"]


def test_publish_failure_prevents_complete_write(monkeypatch):
    repo = _Repo(_full_row("fail-1"))
    recorder = _Recorder(raise_on_publish=True)
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", recorder)

    with pytest.raises(RuntimeError):
        asyncio.run(jp._publish_and_mark_complete("fail-1", _full_row("fail-1")))

    # A publish failure must NOT record "Complete": the job stays
    # durable-Pending so the resweep republishes it.
    assert repo.updates == []


def test_initial_and_replay_events_are_identical(monkeypatch):
    durable = _full_row("replay-1")
    repo = _Repo(durable)
    recorder = _Recorder()
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", recorder)

    # Initial publish (live process_job approval) ...
    asyncio.run(jp._publish_and_mark_complete("replay-1", durable))
    # ... replay (a later retry sweep resweeping a still-Pending row).
    asyncio.run(jp._publish_and_mark_complete("replay-1", durable))

    assert len(recorder.events) == 2
    assert recorder.events[0] == recorder.events[1], (
        "replayed event must be byte-identical to the initial publish "
        "so the consumer can deduplicate by Job UUID"
    )