"""Regression tests for worker lifecycle (audit finding: worker
lifecycle robustness).

Previously WorkerRegistry.run() was a bare asyncio.gather() over every
worker factory: if one worker raised, asyncio.gather propagated that
exception immediately but left every *other* worker task running
unsupervised in the background (orphaned tasks), and there was no
mechanism to release resources (HTTP sessions, etc.) those other
workers held.
"""
import asyncio

import pytest

from app.workers import WorkerRegistry


@pytest.fixture(autouse=True)
def _fresh_liveness_per_test():
    """Every worker here writes liveness via WorkerRegistry.register and
    _run_tracked; the early tests in this file use the default registry,
    which targets app.workers.liveness (a reference to the shared
    app.heartbeat.liveness singleton an order-sensitive test_healthcheck
    and the running app rely on being empty except for the app's own
    workers). Rebinding app.workers.liveness to a fresh instance keeps
    every test here inside the file's own contract: never mutate the
    process-level liveness singleton other test modules rely on."""
    import app.workers as workers_module
    from app.heartbeat import WorkerLiveness

    original = workers_module.liveness
    workers_module.liveness = WorkerLiveness()
    try:
        yield
    finally:
        workers_module.liveness = original


def test_one_worker_failing_cancels_every_sibling_worker():
    cancelled = {"count": 0}
    started = asyncio.Event()

    async def good_worker():
        try:
            await started.wait()
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled["count"] += 1
            raise

    async def bad_worker():
        await asyncio.sleep(0)
        started.set()
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    registry = WorkerRegistry()
    registry.register("good-1", good_worker)
    registry.register("good-2", good_worker)
    registry.register("bad", bad_worker)

    async def scenario():
        with pytest.raises(ExceptionGroup) as excinfo:
            await registry.run()
        assert any(isinstance(e, RuntimeError) for e in excinfo.value.exceptions)

    asyncio.run(scenario())
    assert cancelled["count"] == 2, (
        "every sibling worker must be cancelled, not left running, when "
        "one worker fails"
    )


def test_shutdown_hooks_run_exactly_once_even_when_a_worker_fails():
    hook_calls = {"sync": 0, "async": 0}

    def sync_hook():
        hook_calls["sync"] += 1

    async def async_hook():
        hook_calls["async"] += 1

    async def bad_worker():
        raise RuntimeError("boom")

    registry = WorkerRegistry()
    registry.register("bad", bad_worker)
    registry.register_shutdown(sync_hook)
    registry.register_shutdown(async_hook)

    async def scenario():
        with pytest.raises(ExceptionGroup):
            await registry.run()

    asyncio.run(scenario())
    assert hook_calls == {"sync": 1, "async": 1}


def test_a_failing_shutdown_hook_does_not_block_other_shutdown_hooks():
    hook_calls = {"second": 0}

    def failing_hook():
        raise RuntimeError("cleanup failed")

    def second_hook():
        hook_calls["second"] += 1

    async def quick_worker():
        return None

    registry = WorkerRegistry()
    registry.register("quick", quick_worker)
    registry.register_shutdown(failing_hook)
    registry.register_shutdown(second_hook)

    asyncio.run(registry.run())
    assert hook_calls["second"] == 1


def test_normal_completion_still_runs_shutdown_hooks():
    hook_calls = {"count": 0}

    async def quick_worker():
        return None

    def hook():
        hook_calls["count"] += 1

    registry = WorkerRegistry()
    registry.register("quick", quick_worker)
    registry.register_shutdown(hook)

    asyncio.run(registry.run())
    assert hook_calls["count"] == 1


def test_default_registry_rejects_two_job_sources_sharing_one_adapter(monkeypatch):
    """Regression test for F-4: generalizing the Telegram-only duplicate
    driver guard. Two JOB_SOURCES entries that resolve to the same
    adapter class (e.g. two FreeHub-flavored configs) must not each be
    allowed to start their own SourceWorker polling loop against the
    same underlying adapter -- that would silently double-ingest it.
    """
    import app.workers as workers_module
    from app.runtime_config import JobSourceConfig

    fake_sources = (
        JobSourceConfig(
            id="freehub", adapter="app.adapters.sources.freehub:FreeHubJobSource",
            poll_interval=60, enabled=True,
        ),
        JobSourceConfig(
            id="freehub_dup", adapter="app.adapters.sources.freehub:FreeHubJobSource",
            poll_interval=60, enabled=True,
        ),
    )

    class _FakeRuntime:
        enabled_workers = ("freehub", "freehub_dup")

    monkeypatch.setattr(workers_module, "JOB_SOURCES", fake_sources)
    monkeypatch.setattr(workers_module, "RUNTIME", _FakeRuntime())

    with pytest.raises(RuntimeError, match="freehub.*freehub_dup|freehub_dup.*freehub"):
        workers_module.default_registry()


def test_default_registry_allows_distinct_adapters(monkeypatch):
    """Sanity check: distinct adapters behind distinct JOB_SOURCES
    entries must still be allowed to register their own workers."""
    import app.workers as workers_module
    from app.runtime_config import JobSourceConfig

    fake_sources = (
        JobSourceConfig(
            id="freehub", adapter="app.adapters.sources.freehub:FreeHubJobSource",
            poll_interval=60, enabled=True,
        ),
        JobSourceConfig(
            id="telegram_channels",
            adapter="app.adapters.sources.telegram:TelegramChannelJobSource",
            poll_interval=60, enabled=True,
        ),
    )

    class _FakeRuntime:
        enabled_workers = ("freehub", "telegram_channels")

    monkeypatch.setattr(workers_module, "JOB_SOURCES", fake_sources)
    monkeypatch.setattr(workers_module, "RUNTIME", _FakeRuntime())

    class _FakeSource:
        id = "fake"

    def _fake_build_source(source_id, **settings):
        return _FakeSource()

    monkeypatch.setattr(workers_module, "build_source", _fake_build_source)

    registry = workers_module.default_registry()
    registered_ids = {worker_id for worker_id, _ in registry.factories()}
    assert "freehub" in registered_ids
    assert "telegram_channels" in registered_ids


# ------------------------------------------------------------------
# Per-worker liveness tracking (BUG #2 remediation): WorkerRegistry must
# record each worker's end state in the shared WorkerLiveness registry so
# the healthcheck can tell an unexpected death from an intentional
# shutdown. We use a fresh WorkerLiveness per test to avoid mutating the
# process-level `liveness` singleton that other tests rely on.
# ------------------------------------------------------------------


def test_worker_that_returns_is_recorded_dead():
    """A worker factory that returns rather than looping forever is a
    silent death -- it must be observable as dead, not healthy."""
    from app.heartbeat import WorkerLiveness

    real_liveness = WorkerLiveness()
    worker_id = "silent-death"

    async def quick_worker():
        return None

    import app.workers as workers_module
    original = workers_module.liveness
    workers_module.liveness = real_liveness
    try:
        asyncio.run(WorkerRegistry()._run_tracked(worker_id, quick_worker))
    finally:
        workers_module.liveness = original

    assert real_liveness.state(worker_id)["state"] == "dead", (
        "a worker factory that returns instead of looping forever must be "
        "recorded as dead so the healthcheck does not report it healthy"
    )


def test_worker_cancelled_during_shutdown_is_recorded_shutdown():
    """On TaskGroup shutdown the worker is cancelled -- that is an
    intentional stop, recorded as 'shutdown', not 'dead'."""
    from app.heartbeat import WorkerLiveness
    import app.workers as workers_module

    real_liveness = WorkerLiveness()
    original = workers_module.liveness
    workers_module.liveness = real_liveness
    worker_id = "cancellable"

    async def long_worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise

    try:
        async def scenario():
            reg = WorkerRegistry()
            reg.register(worker_id, long_worker)
            task = asyncio.create_task(reg._run_tracked(worker_id, long_worker))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
    finally:
        workers_module.liveness = original

    assert real_liveness.state(worker_id)["state"] == "shutdown", (
        "cancellation during shutdown must be recorded as 'shutdown', not 'dead'"
    )


def test_worker_that_raises_is_recorded_dead():
    """An unexpected exception kills the worker -- recorded dead so the
    healthcheck fails until the process is restarted."""
    from app.heartbeat import WorkerLiveness
    import app.workers as workers_module

    real_liveness = WorkerLiveness()
    original = workers_module.liveness
    workers_module.liveness = real_liveness
    worker_id = "explodes"

    async def bad_worker():
        raise RuntimeError("boom")

    try:
        async def scenario():
            reg = WorkerRegistry()
            reg.register(worker_id, bad_worker)
            try:
                await reg._run_tracked(worker_id, bad_worker)
            except RuntimeError:
                pass

        asyncio.run(scenario())
    finally:
        workers_module.liveness = original

    assert real_liveness.state(worker_id)["state"] == "dead"
