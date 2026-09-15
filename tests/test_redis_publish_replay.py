"""Redis stream publish / crash-replay tests (Business Node).

Covers the Redis-side contract of the audit:
  - G: RedisJobStreamPublisher.XADD carries the exact Redis trimming
        directive (stream "jobs:notify", MAXLEN=100000, approximate) so
        the shared stream can never grow without bound;
  - F: publish happens before the "Complete" write, publish exceptions
        are never swallowed (the row stays Pending and republishable),
        and a crash BETWEEN the XADD and the Complete write is healed by
        the next retry sweep, which republishes a byte-identical event
        for the same Job UUID (the consumer's idempotency key).
"""
import asyncio

import pytest

import app.services.job_processor as jp
from app.adapters.streams.redis_publisher import RedisJobStreamPublisher

from tests._business_node_fakes import FakeRedisClient, RecordingPublisher


def _dto_row(uuid):
    return {
        "Job UUID": uuid,
        "Title": "Power BI Dashboard",
        "Description": "Need a Power BI dashboard built from sales data.",
        "Source": "Test Channel",
        "URL": "https://example.invalid/job",
        "Short URL": "https://s.example/x",
        "Decision Reason": "direct match",
        "Categories": "Power BI",
        "Category ID": "data_analysis",
        "Category Selection Method": "keyword_direct",
        "Notification Status": "Pending",
        "Final Decision": "Accepted",
    }


# ------------------------------------------------------------------
# G -- Redis stream trimming + XADD contract
# ------------------------------------------------------------------


def test_xadd_goes_to_jobs_notify_with_maxlen_100k_and_bounded_stream():
    client = FakeRedisClient()
    publisher = RedisJobStreamPublisher(client)

    asyncio.run(publisher.publish(_dto_row("x-1")))

    assert len(client.xadds) == 1
    stream, fields, maxlen, approximate = client.xadds[0]
    assert stream == "jobs:notify"
    assert maxlen == 100_000, (
        "the stream must be trimmed to 100k entries so it cannot grow "
        "without bound on the shared Redis"
    )
    assert approximate is True
    assert fields["Job UUID"] == "x-1"
    assert fields["Title"] == "Power BI Dashboard"


def test_xadd_stringifies_all_field_values():
    """Stream fields must be strings: bools -> "1"/"0", None -> "",
    numbers -> str -- so the consumer can parse one uniform shape."""
    client = FakeRedisClient()
    publisher = RedisJobStreamPublisher(client)

    asyncio.run(
        publisher.publish(
            {
                "Job UUID": "b-1",
                "flag": True,
                "off": False,
                "nil": None,
                "n": 3,
                "f": 2.5,
            }
        )
    )

    _, fields, maxlen, _ = client.xadds[0]
    assert fields == {
        "Job UUID": "b-1",
        "flag": "1",
        "off": "0",
        "nil": "",
        "n": "3",
        "f": "2.5",
    }


def test_close_releases_the_redis_client():
    client = FakeRedisClient()
    publisher = RedisJobStreamPublisher(client)

    asyncio.run(publisher.publish(_dto_row("c-1")))
    asyncio.run(publisher.close())

    assert client.closed is True


# ------------------------------------------------------------------
# F -- publish-before-complete ordering and the crash window
# ------------------------------------------------------------------


class _DurableRepo:
    """JobRepository stand-in holding one durable row, recording update
    calls, optionally crashing on the 'Complete' write to simulate a
    process dying right after the XADD."""

    def __init__(self, durable, crash_on_complete=0):
        self.durable = dict(durable)
        self.updates = []
        self.crash_on_complete = crash_on_complete

    async def get_job(self, job_uuid):
        return dict(self.durable)

    async def update_job(self, job_uuid, save=True, **fields):
        if (
            fields.get("notification_status") == "Complete"
            and self.crash_on_complete > 0
        ):
            self.crash_on_complete -= 1
            raise RuntimeError("process died after XADD before the Complete write")
        self.updates.append(fields)
        for key, value in fields.items():
            self.durable[key if key in self.durable else "Notification Status"] = value
        return True

    async def get_incomplete_notification_jobs(self):
        status = self.durable.get("Notification Status") or ""
        if status and status not in ("Complete", "Suppressed"):
            return [dict(self.durable)]
        return []

    async def log_error(self, *a, **kw):
        return None


def test_publish_exception_is_never_swallowed_and_row_stays_pending(monkeypatch):
    client = FakeRedisClient(raise_on_xadd=RuntimeError("redis down"))
    publisher = RedisJobStreamPublisher(client)
    durable = _dto_row("fail-1")
    repo = _DurableRepo(durable)
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", publisher)

    with pytest.raises(RuntimeError, match="redis down"):
        asyncio.run(jp._publish_and_mark_complete("fail-1", dict(durable)))

    assert repo.updates == [], (
        "a failed publish must never be followed by a 'Complete' write"
    )
    assert durable["Notification Status"] == "Pending"

    # The row is still selected by the resweep (at-least-once delivery):
    # the sweep re-attempts and the failing publish surfaces through the
    # sweep's own per-row error handling.
    assert asyncio.run(jp.retry_incomplete_notifications()) == 1
    assert repo.durable["Notification Status"] == "Pending"


def test_crash_after_xadd_is_healed_by_retry_sweep_with_identical_event(
    monkeypatch,
):
    durable = _dto_row("crash-1")
    repo = _DurableRepo(durable, crash_on_complete=1)
    recorder = RecordingPublisher()
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "stream_publisher", recorder)

    # The XADD succeeded but the process died before recording Complete:
    # the raise propagates out of the choke point exactly like a crash.
    with pytest.raises(RuntimeError, match="process died after XADD"):
        asyncio.run(jp._publish_and_mark_complete("crash-1", dict(durable)))

    assert len(recorder.events) == 1
    assert repo.durable["Notification Status"] == "Pending"

    # The next retry sweep republishes the SAME durable row.
    retried = asyncio.run(jp.retry_incomplete_notifications())

    assert retried == 1
    assert len(recorder.events) == 2
    assert recorder.events[0] == recorder.events[1], (
        "a replayed event must be byte-identical so the consumer can "
        "deduplicate by Job UUID"
    )
    assert repo.durable["Notification Status"] == "Complete"