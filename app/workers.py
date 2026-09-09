"""Worker registry. The composition root supplies runtime-owned surfaces."""
import asyncio
from app.runtime_config import JOB_SOURCES, RUNTIME
from app.adapters.sources.registry import build as build_source
from app.source_worker import SourceWorker
from app.heartbeat import liveness, STATE_ALIVE, STATE_SHUTDOWN, STATE_DEAD


class WorkerRegistry:
    """Owns every long-running worker task plus its cleanup.

    run() uses structured concurrency (asyncio.TaskGroup): if any one
    worker raises, every sibling worker is cancelled instead of being
    left running unsupervised in the background. Every registered
    shutdown hook (HTTP transports, and other resources workers hold)
    runs exactly once on the way out, whether run() finishes normally,
    is cancelled, or a worker raised -- so a failure in one worker
    cannot leak the resources another worker owns.

    Individual workers (SourceWorker, the retry loops) already catch
    and log their own steady-state errors and keep looping, so in
    practice this only changes behavior for a worker that fails before
    entering its own loop (e.g. Telegram client startup) -- previously
    that left every other worker task orphaned (asyncio.gather does not
    cancel siblings when one member raises); now it triggers a
    controlled, whole-process shutdown instead.
    """

    def __init__(self, factories=()):
        self._workers = list(factories)
        self._shutdown_hooks = []

    def register(self, worker_id, factory):
        self._workers.append((worker_id, factory))
        return factory

    def register_shutdown(self, hook):
        """Register a zero-arg cleanup callable (sync or async), run
        once after every worker has stopped."""
        self._shutdown_hooks.append(hook)
        return hook

    def factories(self):
        return tuple(self._workers)

    async def _run_tracked(self, worker_id, factory):
        """Run one worker factory with liveness tracking.

        The worker is registered in the shared liveness registry before it
        starts. A long-running worker is expected to loop forever inside its
        factory; if it ever *returns* rather than running forever, that is an
        unexpected exit (a silent death) and is recorded as dead so the
        healthcheck can observe it. On clean cancellation (TaskGroup shutdown)
        it is recorded as "shutdown". On an exception it is recorded as dead.

        Individual workers that want finer-grained progress reporting beat
        themselves via the shared `liveness` registry (e.g. the Telegram
        worker beats each live iteration and reports "reconnecting" during its
        bounded reconnect backoff). This wrapper guarantees that even a worker
        with no internal instrumentation is still observable as dead the
        moment its task ends unexpectedly.
        """
        liveness.register(worker_id, STATE_ALIVE)
        try:
            await factory()
        except asyncio.CancelledError:
            liveness.set_state(worker_id, STATE_SHUTDOWN)
            raise
        except Exception:
            liveness.set_state(worker_id, STATE_DEAD)
            raise
        # A worker factory that returns (rather than looping forever or
        # raising) has stopped unexpectedly -- this is the silent-death case.
        liveness.set_state(worker_id, STATE_DEAD)

    async def run(self):
        try:
            async with asyncio.TaskGroup() as tg:
                for worker_id, factory in self._workers:
                    tg.create_task(self._run_tracked(worker_id, factory))
        finally:
            await self._run_shutdown_hooks()

    async def _run_shutdown_hooks(self):
        for hook in self._shutdown_hooks:
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                print(f"[SHUTDOWN] cleanup hook {hook!r} failed: {exc!r}")


async def _process_source_job(job, source):
    from app.job_processor import process_job
    normalized = source.normalize(job) if hasattr(source, "normalize") else job
    job_id = normalized.get("job_id", normalized.get("uid"))
    if job_id is None or str(job_id).strip() == "":
        raise ValueError(f"Source {source.id!r} returned a job without a stable job_id/uid")
    identity_source = normalized.get("identity_source", source.identity_source)
    await process_job(job=normalized, job_id=str(job_id), identity_source=str(identity_source))


def default_registry(runtime=None):
    registry=WorkerRegistry()
    enabled = set(RUNTIME.enabled_workers)
    # Exactly one canonical production implementation per source
    # (architectural invariant, audited requirement):
    #   - FreeHub runs exclusively through FreeHubJobSource on the generic
    #     SourceWorker. There is no separate hand-rolled FreeHub worker.
    #   - Telegram's single implementation is TelegramChannelJobSource
    #     (app.adapters.sources.telegram); app.handlers.telegram is a thin
    #     compatibility re-export of it. That one implementation has two
    #     possible drivers: the live worker ('telegram', via
    #     TelegramChannelWorker) and the poll-mode SourceWorker
    #     ('telegram_channels'). Enabling both drivers would silently
    #     double-ingest the same channels with two watermark loops, so that
    #     misconfiguration is rejected here at composition time instead of
    #     silently racing at runtime.
    telegram_sources = {
        cfg for cfg in JOB_SOURCES
        if cfg.id.lower() == "telegram_channels" and cfg.enabled
    }
    if "telegram" in enabled and telegram_sources:
        raise RuntimeError(
            "Configured Telegram ingestion twice: the 'telegram' live worker "
            "(TelegramChannelWorker) and the 'telegram_channels' poll-mode "
            "JobSource are two drivers of the same TelegramChannelJobSource "
            "and are mutually exclusive production paths. Enable only one: "
            "disable the JobSource, or remove 'telegram' from "
            "ENABLED_WORKERS."
        )
    # Existing live Telethon channel ingestion remains a registered worker
    # (driving the canonical TelegramChannelJobSource), while every
    # configured JobSource uses the generic source worker. Imports are
    # conditional so a source-only process need not install Telegram/LLM SDKs.
    if "telegram" in enabled:
        from app.adapters.sources.telegram import TelegramChannelWorker
        from app.handlers.telegram import build_telegram_source
        source = (
            runtime.telegram_source
            if runtime is not None and runtime.telegram_source is not None
            else build_telegram_source()
        )
        worker = TelegramChannelWorker(source)
        registry.register("telegram", worker.run)
        if hasattr(source, "aclose"):
            registry.register_shutdown(source.aclose)
    from app.heartbeat import heartbeat_loop
    registry.register("heartbeat", heartbeat_loop)

    # Generalized duplicate-registration guard (audit finding), extending
    # the Telegram-specific check above to every adapter: two JOB_SOURCES
    # entries that resolve to the same adapter class (most notably two
    # entries both wired to FreeHubJobSource) would each get their own
    # SourceWorker polling loop below, both driving the same underlying
    # adapter/API against the same upstream source -- silently
    # double-ingesting it, exactly the same class of bug as running both
    # Telegram drivers at once. This is a config-time invariant, so it is
    # checked once for the whole enabled set rather than per-source.
    _enabled_source_cfgs = [
        cfg for cfg in JOB_SOURCES if cfg.enabled and cfg.id in enabled
    ]
    _adapter_owner_id = {}
    for cfg in _enabled_source_cfgs:
        owner_id = _adapter_owner_id.get(cfg.adapter)
        if owner_id is not None:
            raise RuntimeError(
                f"Configured job source adapter {cfg.adapter!r} twice: "
                f"JOB_SOURCES entries {owner_id!r} and {cfg.id!r} both "
                "resolve to the same adapter class and would each start "
                "their own polling worker against the same underlying "
                "source, double-ingesting it. Enable only one of them."
            )
        _adapter_owner_id[cfg.adapter] = cfg.id

    for cfg in _enabled_source_cfgs:
        source = build_source(cfg.id, **(cfg.settings or {}))
        _logger = runtime.repository if runtime is not None else None
        registry.register(
            cfg.id,
            lambda source=source, interval=cfg.poll_interval, logger=_logger:
            SourceWorker(source, _process_source_job, interval, logger=logger).run(),
        )
        if hasattr(source, "aclose"):
            registry.register_shutdown(source.aclose)
    # The scraper that feeds the LinkedIn/Wuzzuf FileJobSource adapters
    # ships inside the image (see app/scraper_scheduler.py). It only makes
    # sense to schedule it when an enabled JobSource actually reads the
    # scraper snapshot, so the scheduler worker is started exactly when a
    # scraper_file-backed job source is running. It runs the scraper as a
    # subprocess on the source's poll cadence, so there is nothing
    # host-side (no cron, no host script) in the pipeline.
    scraper_file_sources = [
        cfg for cfg in _enabled_source_cfgs
        if str(cfg.adapter or "").startswith("app.adapters.sources.scraper_file:")
    ]
    if scraper_file_sources:
        from app.scraper_scheduler import (
            scraper_scheduler_factory,
            WORKER_ID,
            SCRAPER_SCRIPT,
            SCRAPER_WORKDIR,
        )
        _scraper_interval = min(cfg.poll_interval for cfg in scraper_file_sources)
        registry.register(
            WORKER_ID,
            scraper_scheduler_factory(
                script_path=SCRAPER_SCRIPT,
                workdir=SCRAPER_WORKDIR,
                interval=_scraper_interval,
                worker_id=WORKER_ID,
            ),
        )
    if "classification_retry" in enabled:
        from app.job_processor import classification_retry_loop
        registry.register("classification_retry", lambda: classification_retry_loop(RUNTIME.notification_retry_interval))
    if "notification_retry" in enabled:
        from app.job_processor import notification_retry_loop
        registry.register("notification_retry", lambda: notification_retry_loop(RUNTIME.notification_retry_interval))
    if "user_notifications" in enabled:
        from app.user_bot import user_notification_worker
        if runtime is not None:
            registry.register("user_notifications_polling", lambda: runtime.user_bot.run())
            registry.register("user_notifications", user_notification_worker)
        else:
            registry.register("user_notifications", user_notification_worker)
    return registry
