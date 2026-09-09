"""Regression tests for per-worker liveness beats in the retry workers.

The healthcheck flags a critical "alive" worker as stale/hung when its last
beat is older than 3 * heartbeat_interval (45s at the default 15s cadence).
The classification/notification retry loops and the user-notification worker
sleep for their configured interval between sweeps, which can exceed that
window -- so beating only once per loop iteration would make a healthy,
idle worker look hung. sleep_with_beats() interleaves small sleeps with
liveness beats so the maximum beat gap is bounded by an explicit cadence
independent of the (longer) idle interval.
"""
import asyncio

import pytest

from app import heartbeat
from app.heartbeat import WorkerLiveness, STATE_ALIVE, STATE_RUNNING, sleep_with_beats


@pytest.fixture()
def isolated_liveness(monkeypatch):
    reg = WorkerLiveness()
    monkeypatch.setattr(heartbeat, "liveness", reg)
    return reg


def test_sleep_with_beats_beats_multiple_times_during_a_long_sleep(isolated_liveness):
    reg = isolated_liveness
    # Sleep 6 beats' worth; with a 0.01 cadence we expect several beats.
    async def run():
        await sleep_with_beats(0.06, "w", STATE_RUNNING, cadence=0.01)

    asyncio.run(run())

    entry = reg.state("w")
    assert entry is not None
    assert entry["state"] == STATE_ALIVE


def test_maximum_beat_gap_stays_within_cadence_during_idle(isolated_liveness):
    reg = isolated_liveness
    gaps = []

    real_sleep = heartbeat.asyncio.sleep

    async def spy_sleep(seconds):
        # Record the sleep step that is about to happen; the liveness beat
        # should fire between every step, so no single step exceeds cadence.
        gaps.append(seconds)
        await real_sleep(seconds)

    async def run():
        import app.heartbeat as hb
        orig = hb.asyncio.sleep
        hb.asyncio.sleep = spy_sleep
        try:
            await sleep_with_beats(0.5, "w", STATE_RUNNING, cadence=0.1)
        finally:
            hb.asyncio.sleep = orig

    asyncio.run(run())

    assert len(gaps) >= 4, f"expected several sub-cadence steps, got {len(gaps)}"
    assert all(g <= 0.1 for g in gaps), (
        "no single sleep step may exceed the beat cadence, otherwise a long "
        "idle interval would blow the healthcheck staleness window"
    )


async def _run_briefly(loop_task, reg, worker_id, ticks=3):
    """Let the worker loop run for a few ticks, then cancel it."""
    observed = 0
    for _ in range(40):
        await asyncio.sleep(0.005)
        entry = reg.state(worker_id)
        if entry is not None and entry["last_beat"] is not None:
            observed += 1
        if observed >= ticks:
            break
        if loop_task.done():
            break
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass


def test_classification_retry_loop_beats_during_idle(monkeypatch, isolated_liveness):
    reg = isolated_liveness
    calls = {"n": 0}

    async def fake_retry():
        calls["n"] += 1
        return 0

    async def fake_log_error(*a, **k):
        return None

    import app.job_processor as jp
    monkeypatch.setattr(jp, "retry_incomplete_classifications", fake_retry)
    # Short interval + tiny cadence so the test sleeps minimally.
    monkeypatch.setattr(jp, "sleep_with_beats", _patched_sleep_with_beats(reg))

    async def run():
        task = asyncio.create_task(jp.classification_retry_loop(interval_seconds=60))
        await _run_briefly(task, reg, "classification_retry")

    asyncio.run(run())
    assert calls["n"] >= 1
    assert reg.state("classification_retry")["state"] == STATE_ALIVE


def test_notification_retry_loop_beats_during_idle(monkeypatch, isolated_liveness):
    reg = isolated_liveness
    calls = {"n": 0}

    async def fake_retry():
        calls["n"] += 1
        return 0

    async def fake_log_error(*a, **k):
        return None

    import app.job_processor as jp
    monkeypatch.setattr(jp, "retry_incomplete_notifications", fake_retry)
    monkeypatch.setattr(jp, "sleep_with_beats", _patched_sleep_with_beats(reg))

    async def run():
        task = asyncio.create_task(jp.notification_retry_loop(interval_seconds=60))
        await _run_briefly(task, reg, "notification_retry")

    asyncio.run(run())
    assert calls["n"] >= 1
    assert reg.state("notification_retry")["state"] == STATE_ALIVE


def test_user_notification_worker_beats_during_idle(monkeypatch, isolated_liveness):
    reg = isolated_liveness

    async def fake_claim(limit):
        return []

    async def fake_log_error(*a, **k):
        return None

    import app.user_bot as ub
    monkeypatch.setattr(ub, "logger", type("L", (), {
        "claim_pending_user_notifications": staticmethod(fake_claim),
        "log_error": staticmethod(fake_log_error),
    })())
    monkeypatch.setattr(ub, "sleep_with_beats", _patched_sleep_with_beats(reg))

    async def run():
        task = asyncio.create_task(ub.user_notification_worker())
        await _run_briefly(task, reg, "user_notifications")

    asyncio.run(run())
    assert reg.state("user_notifications")["state"] == STATE_ALIVE


def _patched_sleep_with_beats(reg):
    """A bounded sleep_with_beats that beats the target worker and returns
    quickly instead of sleeping the full (large) idle interval."""

    async def patched(seconds, worker_id, state=STATE_RUNNING, cadence=None):
        reg.beat(worker_id, state)
        await asyncio.sleep(0.001)

    return patched
