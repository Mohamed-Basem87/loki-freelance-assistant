"""Generic source worker for every JobSource implementation."""
import asyncio
import inspect
from app.ports import Worker
from app.heartbeat import liveness, STATE_ALIVE

# Sub-window cadence for liveness beats during idle sleeps AND during
# long-running job processing. The healthcheck flags a critical "alive"
# worker as stale/hung when its last beat is older than 3 *
# heartbeat_interval (45s at the default 15s cadence). A legitimate
# process_job() call (FreeHub poll -> many jobs -> classification ->
# multiple LLM attempts -> persistence -> notification) can exceed that
# window, so the worker must beat during active processing as well as
# during idle sleep.
_LIVENESS_BEAT_CADENCE_SECONDS = 10.0

class SourceWorker(Worker):
    id = "source"
    def __init__(self, source, process_job, interval, recovery=None, logger=None):
        self.source, self.process_job, self.interval, self.recovery = source, process_job, interval, recovery
        self._logger = logger
        if self._logger is None:
            from app.dependencies import logger as _default_logger
            self._logger = _default_logger

    async def _call(self, value, *args):
        result = value(*args)
        return await result if inspect.isawaitable(result) else result

    async def _idle_loop(self, duration):
        """Sleep through the poll interval while still reporting liveness
        on a sub-window cadence, so a source whose poll interval is longer
        than the healthcheck staleness window is never flagged stale/hung
        while legitimately idle between polls."""
        remaining = float(duration)
        while True:
            step = min(_LIVENESS_BEAT_CADENCE_SECONDS, remaining)
            await asyncio.sleep(step)
            remaining -= step
            if remaining <= 0:
                break
            liveness.beat(self.source.id, STATE_ALIVE)

    async def _beat_during_processing(self):
        """Background task: beat liveness on a fixed cadence while a
        job is being processed, so a long-running process_job() call
        (classification, LLM, notification) cannot make the worker appear
        stale to the healthcheck."""
        try:
            while True:
                liveness.beat(self.source.id, STATE_ALIVE)
                await asyncio.sleep(_LIVENESS_BEAT_CADENCE_SECONDS)
        except asyncio.CancelledError:
            return

    async def run(self):
        if self.recovery is not None:
            await self.recovery.recover()
        # Report per-worker liveness so a healthy global heartbeat cannot
        # mask a dead FreeHub/source polling worker (see app/heartbeat.py).
        liveness.beat(self.source.id, STATE_ALIVE)
        while True:
            try:
                jobs = await self.source.poll()
                for job in jobs:
                    beat_task = asyncio.create_task(self._beat_during_processing())
                    try:
                        await self._call(self.process_job, job, self.source)
                        await self.source.mark_seen(job)
                    except Exception as exc:
                        from app.job_processor import ClassificationPendingError
                        if isinstance(exc, ClassificationPendingError):
                            # Expected control-flow from process_job (P3-B):
                            # the job is durably pending/claimed on another
                            # path, so this worker neither owns the retry nor
                            # failed. Do NOT mark it seen (that would falsely
                            # retire a still-pending job) and do NOT record
                            # it as a worker error (any underlying LLM
                            # failure was already audited where it happened,
                            # in app.job_processor). The durable pending row
                            # stays reachable by the retry sweeps.
                            continue
                        await self._logger.log_error(self.source.id, exc, job.get("job_id", job.get("uid", "")))
                    finally:
                        beat_task.cancel()
                        try:
                            await beat_task
                        except asyncio.CancelledError:
                            pass
            except Exception as exc:
                await self._logger.log_error(self.source.id, exc)
            liveness.beat(self.source.id, STATE_ALIVE)
            await self._idle_loop(self.interval)