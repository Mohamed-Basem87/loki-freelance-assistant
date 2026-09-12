"""Tests for the in-container scraper scheduler (app.scraper_scheduler).

The scheduler runs scraper/scraper.py as a subprocess on the FileJobSource
poll cadence so the whole LinkedIn/Wuzzuf pipeline lives inside the
container (no host cron/script). These tests exercise the subprocess
runner and the worker-registry wiring without installing scrapling.
"""
import asyncio
import json

import pytest

from app.scraper_scheduler import _run_scraper_once, scraper_scheduler_factory


@pytest.fixture()
def _beatless(monkeypatch):
    """Make the loop idle without touching the process-global liveness
    registry (app.heartbeat.liveness is sticky; beating an extra worker
    here would pollute later healthcheck tests)."""
    import app.scraper_scheduler as scheduler_module

    async def _idle(seconds, worker_id, state=None, cadence=None):
        await asyncio.sleep(0)

    monkeypatch.setattr(scheduler_module, "sleep_with_beats", _idle)


@pytest.fixture()
def _scriptless_scraper(monkeypatch):
    """Drive the scheduler loop without spawning real python subprocesses.

    These state-transition tests only care how the loop maps run outcomes
    to liveness state (and back); the real subprocess lifecycle is
    already pinned by the test_run_scraper_once_* tests. Spawning a real
    child here makes every failing "run" cost a full interpreter startup,
    and on a busy CI runner enough of those can consume the whole watch
    window, turning a correct worker into a flaky test failure. A faked
    run returns the next exit code synchronously, so the state machine is
    exercised deterministically in milliseconds. Mutate the returned
    dict's "exit_codes" list: a single element repeats forever; multiple
    elements are consumed in order and the last one repeats.
    """
    import app.scraper_scheduler as scheduler_module

    run_info = {"exit_codes": [], "runs": 0}

    async def fake_run(script_path, workdir, timeout=600):
        run_info["runs"] += 1
        codes = run_info["exit_codes"]
        if not codes:
            return 1
        return codes.pop(0) if len(codes) > 1 else codes[0]

    monkeypatch.setattr(scheduler_module, "_run_scraper_once", fake_run)
    return run_info


def test_run_scraper_once_runs_child_and_writes_to_workdir(tmp_path):
    script = tmp_path / "scraper.py"
    script.write_text(
        'import json, pathlib\n'
        'pathlib.Path("jobs_results.json").write_text(json.dumps([{"title": "x"}]))\n',
        encoding="utf-8",
    )
    out_file = tmp_path / "jobs_results.json"

    code = asyncio.run(_run_scraper_once(script, tmp_path))

    assert code == 0
    assert json.loads(out_file.read_text(encoding="utf-8")) == [{"title": "x"}]


def test_run_scraper_once_reports_child_failure(tmp_path):
    script = tmp_path / "scraper.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

    code = asyncio.run(_run_scraper_once(script, tmp_path))

    assert code == 3


def test_run_scraper_once_kills_hung_child(tmp_path):
    script = tmp_path / "scraper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    with pytest.raises(TimeoutError):
        asyncio.run(_run_scraper_once(script, tmp_path, timeout=1))


def test_scheduler_factory_runs_scraper_immediately_then_idles(
    tmp_path, _beatless, _scriptless_scraper
):
    _scriptless_scraper["exit_codes"] = [0]  # every run succeeds

    loop = scraper_scheduler_factory(
        script_path=tmp_path / "scraper.py", workdir=tmp_path, interval=60
    )

    async def scenario():
        task = asyncio.create_task(loop())
        for _ in range(100):
            if _scriptless_scraper["runs"] >= 1:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _scriptless_scraper["runs"] >= 1, (
            "scheduler should run the scraper on startup"
        )

    asyncio.run(scenario())


def test_scheduler_keeps_looping_across_failed_runs(
    tmp_path, _beatless, _scriptless_scraper
):
    """A failing scrape must not escape the worker loop (TaskGroup would
    otherwise shut the whole app down); it logs and retries next cycle."""
    _scriptless_scraper["exit_codes"] = [9, 0]

    loop = scraper_scheduler_factory(
        script_path=tmp_path / "scraper.py", workdir=tmp_path, interval=1
    )

    async def scenario():
        task = asyncio.create_task(loop())
        for _ in range(400):
            if _scriptless_scraper["runs"] >= 2:
                break  # first run failed, second run succeeded
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _scriptless_scraper["runs"] >= 2

    asyncio.run(scenario())


def test_default_registry_registers_scheduler_for_file_sources(monkeypatch):
    import app.workers as workers_module
    from app.runtime_config import JobSourceConfig

    fake_sources = (
        JobSourceConfig(
            id="linkedin",
            adapter="app.adapters.sources.scraper_file:LinkedInFileJobSource",
            poll_interval=300,
            enabled=True,
        ),
    )

    class _FakeRuntime:
        enabled_workers = ("linkedin",)

    monkeypatch.setattr(workers_module, "JOB_SOURCES", fake_sources)
    monkeypatch.setattr(workers_module, "RUNTIME", _FakeRuntime())

    class _FakeSource:
        id = "fake"

    def _fake_build_source(source_id, **settings):
        return _FakeSource()

    monkeypatch.setattr(workers_module, "build_source", _fake_build_source)

    registry = workers_module.default_registry()
    registered_ids = {worker_id for worker_id, _ in registry.factories()}

    assert "scraper_scheduler" in registered_ids


def test_default_registry_skips_scheduler_without_file_sources(monkeypatch):
    import app.workers as workers_module
    from app.runtime_config import JobSourceConfig

    fake_sources = (
        JobSourceConfig(
            id="freehub",
            adapter="app.adapters.sources.freehub:FreeHubJobSource",
            poll_interval=60,
            enabled=True,
        ),
    )

    class _FakeRuntime:
        enabled_workers = ("freehub",)

    monkeypatch.setattr(workers_module, "JOB_SOURCES", fake_sources)
    monkeypatch.setattr(workers_module, "RUNTIME", _FakeRuntime())

    class _FakeSource:
        id = "fake"

    def _fake_build_source(source_id, **settings):
        return _FakeSource()

    monkeypatch.setattr(workers_module, "build_source", _fake_build_source)

    registry = workers_module.default_registry()
    registered_ids = {worker_id for worker_id, _ in registry.factories()}

    assert "scraper_scheduler" not in registered_ids


def test_scheduler_reports_dead_after_sustained_failures(tmp_path, _scriptless_scraper):
    """A scraper that keeps failing must eventually report its worker as
    dead (not just loop forever printing errors) so healthcheck.py's
    existing bad-state handling can fail the container -- this is the
    fix for 'worker liveness confused with source freshness': the loop
    staying alive forever must not mask LinkedIn/Wuzzuf data going stale.
    conftest._reset_worker_liveness clears the registry around this test,
    so there is no need for the _beatless fixture here -- we want the
    real beats. _scriptless_scraper drives run outcomes directly (see
    that fixture) so the state transitions are deterministic instead of
    costing a full python subprocess spawn per failure."""
    from app.heartbeat import liveness
    from app.scraper_scheduler import CONSECUTIVE_FAILURE_THRESHOLD

    _scriptless_scraper["exit_codes"] = [1]  # every run fails, forever

    loop = scraper_scheduler_factory(
        script_path=tmp_path / "scraper.py", workdir=tmp_path, interval=0.05
    )

    async def scenario():
        task = asyncio.create_task(loop())
        for _ in range(1000):
            entry = liveness.state("scraper_scheduler")
            if entry and entry["state"] == "dead":
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = liveness.state("scraper_scheduler")
        assert entry is not None
        assert entry["state"] == "dead"

    asyncio.run(scenario())
    assert CONSECUTIVE_FAILURE_THRESHOLD >= 1  # sanity: threshold is meaningful


def test_scheduler_recovers_to_alive_after_a_successful_run_following_failures(
    tmp_path, _scriptless_scraper
):
    """Once a run succeeds again, the worker must self-heal back to
    'alive' with no restart required -- sustained failure is reported,
    not a permanent quarantine."""
    from app.heartbeat import liveness
    from app.scraper_scheduler import CONSECUTIVE_FAILURE_THRESHOLD

    _scriptless_scraper["exit_codes"] = [1] * CONSECUTIVE_FAILURE_THRESHOLD + [0]

    loop = scraper_scheduler_factory(
        script_path=tmp_path / "scraper.py", workdir=tmp_path, interval=0.05
    )

    async def scenario():
        task = asyncio.create_task(loop())
        # Wait until it goes dead...
        for _ in range(1000):
            entry = liveness.state("scraper_scheduler")
            if entry and entry["state"] == "dead":
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("worker never reported dead after sustained failures")
        # ...then wait until it recovers.
        for _ in range(1000):
            entry = liveness.state("scraper_scheduler")
            if entry and entry["state"] == "alive":
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        entry = liveness.state("scraper_scheduler")
        assert entry is not None and entry["state"] == "alive"

    asyncio.run(scenario())