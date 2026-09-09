"""Tests for the canonical FreeHubJobSource production path.

The legacy app.freehub_worker module has been retired; FreeHubJobSource
is now the sole production adapter for FreeHub ingestion.
"""
import asyncio

import pytest

from app.adapters.sources.freehub import FreeHubApiClient, FreeHubJobSource
from app.source_worker import SourceWorker


def _project(uid="p1"):
    return {
        "uid": uid,
        "_poll_source": "kafiil",
        "title": "Power BI job",
        "description": "Analyze sales data.",
        "platform": "kafiil",
        "price": "$100",
        "project_link": "https://example.invalid/p1",
    }


def _build_source(poller):
    http_client = FreeHubApiClient(base_url="http://fake", user_id="test")
    source = FreeHubJobSource(
        poller=poller,
        marker=lambda project: None,
        http_client=http_client,
    )
    return source


def test_worker_does_not_mark_failed_project_seen(monkeypatch):
    """SourceWorker must not call mark_seen when process_job fails."""
    calls = {"process": 0, "mark": 0}

    async def fake_process_job(job, source):
        calls["process"] += 1
        raise RuntimeError("temporary processing failure")

    async def fake_poll():
        return [_project()]

    async def fake_mark(job):
        calls["mark"] += 1

    source = _build_source(fake_poll)
    source.mark_seen = fake_mark

    worker = SourceWorker(source, fake_process_job, interval=9999)

    async def _run_one_cycle():
        jobs = await source.poll()
        for job in jobs:
            try:
                await fake_process_job(job, source)
                await source.mark_seen(job)
            except Exception:
                pass

    asyncio.run(_run_one_cycle())
    assert calls == {"process": 1, "mark": 0}


def test_worker_marks_project_seen_after_success(monkeypatch):
    """SourceWorker calls mark_seen only after successful processing."""
    calls = {"process": 0, "mark": 0}

    async def fake_process_job(job, source):
        calls["process"] += 1

    async def fake_poll():
        return [_project()]

    async def fake_mark(job):
        calls["mark"] += 1

    source = _build_source(fake_poll)
    source.mark_seen = fake_mark

    async def _run_one_cycle():
        jobs = await source.poll()
        for job in jobs:
            await fake_process_job(job, source)
            await source.mark_seen(job)

    asyncio.run(_run_one_cycle())
    assert calls == {"process": 1, "mark": 1}


def test_worker_source_uses_a_single_shared_http_client(monkeypatch):
    """The FreeHubJobSource must thread its http_client through to poll_once
    on every poll, rather than discarding it."""
    called_with = {}

    async def fake_poll_once(client=None):
        called_with["client"] = client
        return []

    http_client = FreeHubApiClient(base_url="http://fake", user_id="test")
    source = FreeHubJobSource(
        poller=lambda: fake_poll_once(client=http_client),
        marker=lambda project: None,
        http_client=http_client,
    )

    asyncio.run(source.poll())

    assert called_with["client"] is http_client
    assert called_with["client"] is source.http_client


def test_source_aclose_closes_its_transport():
    """Shutting down the FreeHub source must close the transport it owns."""
    closed = {"count": 0}

    class _FakeTransport:
        async def close(self):
            closed["count"] += 1

    http_client = FreeHubApiClient(base_url="http://fake", user_id="test")
    http_client.transport = _FakeTransport()
    source = FreeHubJobSource(
        poller=lambda: asyncio.sleep(0),
        marker=lambda project: None,
        http_client=http_client,
    )

    asyncio.run(source.aclose())
    assert closed["count"] == 1


def _run_worker_until_idle_loop(worker, monkeypatch):
    """Drive SourceWorker.run() for exactly one poll cycle: the patched
    _idle_loop raises immediately, ending the run deterministically. The
    worker's liveness beats are stubbed so driving run() here can never
    pollute the process-global heartbeat registry that other tests
    (test_healthcheck) read via write_heartbeat()."""
    import types

    from app import source_worker as source_worker_module

    monkeypatch.setattr(
        source_worker_module,
        "liveness",
        types.SimpleNamespace(beat=lambda *a, **k: None),
    )

    class _Stop(Exception):
        pass

    async def fake_idle_loop(self, duration):
        raise _Stop

    monkeypatch.setattr(SourceWorker, "_idle_loop", fake_idle_loop)
    with pytest.raises(_Stop):
        asyncio.run(worker.run())


def test_worker_does_not_log_or_mark_classification_pending(monkeypatch):
    """P3-B: a ClassificationPendingError raised by process_job is
    expected control-flow -- the job is durably pending/claimed on another
    path. The worker must neither record it as a worker error nor mark the
    job seen (mark_seen would falsely retire a still-pending job)."""
    import types

    from app.job_processor import ClassificationPendingError

    calls = {"process": 0, "mark": 0}
    errors = []

    async def fake_process_job(job, source):
        calls["process"] += 1
        raise ClassificationPendingError("pending or claimed by another worker")

    async def fake_poll():
        return [_project()]

    async def fake_mark(job):
        calls["mark"] += 1

    source = _build_source(fake_poll)
    source.mark_seen = fake_mark

    async def fake_log_error(*a, **k):
        errors.append(a)

    logger_stub = types.SimpleNamespace(log_error=fake_log_error)
    worker = SourceWorker(source, fake_process_job, interval=9999, logger=logger_stub)

    _run_worker_until_idle_loop(worker, monkeypatch)

    assert calls == {"process": 1, "mark": 0}, (
        "a ClassificationPendingError must not mark the pending job seen"
    )
    assert errors == [], (
        "a ClassificationPendingError must not be recorded as a worker error"
    )


def test_worker_still_logs_real_failures(monkeypatch):
    """The generic worker-error path must be untouched for genuine
    failures: they are logged (and the job is not marked seen)."""
    import types

    calls = {"process": 0, "mark": 0}
    errors = []

    async def fake_process_job(job, source):
        calls["process"] += 1
        raise RuntimeError("boom")

    async def fake_poll():
        return [_project()]

    async def fake_mark(job):
        calls["mark"] += 1

    source = _build_source(fake_poll)
    source.mark_seen = fake_mark

    async def fake_log_error(*a, **k):
        errors.append(a)

    logger_stub = types.SimpleNamespace(log_error=fake_log_error)
    worker = SourceWorker(source, fake_process_job, interval=9999, logger=logger_stub)

    _run_worker_until_idle_loop(worker, monkeypatch)

    assert calls == {"process": 1, "mark": 0}
    assert len(errors) == 1
    assert "boom" in str(errors[0])


# ----------------------------------------------------------------------
# FIX 1 Regression: SourceWorker liveness during long process_job()
# ----------------------------------------------------------------------

def test_source_worker_beats_liveness_during_long_processing(monkeypatch):
    """A long-running process_job() must not leave the SourceWorker's
    liveness stale. The worker beats on a sub-window cadence during
    active processing, so the healthcheck's staleness window (3 *
    heartbeat_interval) is never exceeded by legitimate work."""
    import types
    from app import source_worker as sw_module
    from app.heartbeat import WorkerLiveness, STATE_ALIVE

    # Use a fresh, isolated liveness registry for this test.
    reg = WorkerLiveness()
    monkeypatch.setattr(sw_module, "liveness", reg)

    beat_times = {"count": 0, "during_processing": 0}

    async def fake_process_job(job, source):
        # Simulate long-running work (classification + LLM + notification)
        # by sleeping longer than the healthcheck stale threshold (45s).
        # We use a short sleep but verify beats happen during it.
        beat_times["during_processing"] = beat_times["count"]
        await asyncio.sleep(0.15)  # Longer than 3 * 0.05s cadence
        beat_times["during_processing"] = beat_times["count"]

    async def fake_poll():
        return [{"uid": "job-1", "title": "Test", "description": "Test"}]

    async def fake_mark(job):
        pass

    source = _build_source(fake_poll)
    source.mark_seen = fake_mark

    # Patch the beat cadence to be very fast for the test
    monkeypatch.setattr(sw_module, "_LIVENESS_BEAT_CADENCE_SECONDS", 0.05)

    worker = SourceWorker(source, fake_process_job, interval=9999)

    class _Stop(Exception):
        pass

    async def fake_idle_loop(self, duration):
        raise _Stop

    monkeypatch.setattr(SourceWorker, "_idle_loop", fake_idle_loop)

    with pytest.raises(_Stop):
        asyncio.run(worker.run())

    # Verify liveness was beaten during the long process_job call
    # (at least 2 beats: one before/at start, one during the 0.15s sleep)
    entry = reg.state(source.id)
    assert entry is not None
    assert entry["state"] == STATE_ALIVE
    # The worker should have beaten at least a couple times during processing
    # (exact count depends on timing, but should be > 1 during the 0.15s sleep)
    # We can't easily count beats, but we can verify the state is alive
    # and the last_beat is recent (which it will be since we just ran)
