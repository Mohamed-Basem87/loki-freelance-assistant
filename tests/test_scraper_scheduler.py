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


def test_scheduler_factory_runs_scraper_immediately_then_idles(tmp_path, _beatless):
    script = tmp_path / "scraper.py"
    script.write_text(
        'import pathlib\npathlib.Path("jobs_results.json").write_text("[]")\n',
        encoding="utf-8",
    )
    out_file = tmp_path / "jobs_results.json"

    loop = scraper_scheduler_factory(
        script_path=script, workdir=tmp_path, interval=60
    )

    async def scenario():
        task = asyncio.create_task(loop())
        for _ in range(100):
            if out_file.exists():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert out_file.exists(), "scheduler should run the scraper on startup"

    asyncio.run(scenario())


def test_scheduler_keeps_looping_across_failed_runs(tmp_path, _beatless):
    """A failing scrape must not escape the worker loop (TaskGroup would
    otherwise shut the whole app down); it logs and retries next cycle."""
    script = tmp_path / "scraper.py"
    state = tmp_path / "state.txt"
    script.write_text(
        "import pathlib, sys\n"
        f'p = pathlib.Path(r"{state}")\n'
        "count = int(p.read_text()) if p.exists() else 0\n"
        "count += 1\n"
        "p.write_text(str(count))\n"
        "if count == 1:\n"
        "    sys.exit(9)\n",
        encoding="utf-8",
    )

    loop = scraper_scheduler_factory(script_path=script, workdir=tmp_path, interval=1)

    async def scenario():
        task = asyncio.create_task(loop())
        for _ in range(400):
            if state.exists():
                count = int(state.read_text())
                if count >= 2:
                    break  # first run failed, second run succeeded
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert int(state.read_text()) >= 2

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