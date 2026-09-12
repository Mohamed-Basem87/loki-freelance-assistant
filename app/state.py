import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.runtime_config import RUNTIME
from app.heartbeat import liveness, STATE_ALIVE, STATE_DEAD


STATE_FILE = Path(RUNTIME.state_file_path)

_FREEHUB_KEY = "_freehub_seen"
_FREEHUB_PENDING_KEY = "_freehub_pending"
_FREEHUB_CURSOR_KEY = "_freehub_backfill_cursor"

# Cross-source dedup records are stored as:
# {
#     "project_id": {"job_uuid": "...", "claimed_at": 1234567890.0}
# }
#
# Keeping the timestamp lets us prune old IDs instead of growing
# state.json forever.
_CROSS_SOURCE_SEEN_KEY = "_cross_source_seen"

# How long a cross-source dedup claim lives before it expires.
#
# This is the window during which the SAME project id advertised by two
# different sources (e.g. FreeHub and Telegram) is recognized as one
# project and only delivered once. After a claim is older than this it is
# pruned from state.json (see _prune_cross_source_seen) and a project id
# can be claimed again -- which is intentional: the value is the work
# against repeat notification of a project that is *actively* being
# advertised. A project reposted/re-listed after the window may reasonably
# be treated as new, and the bounded window is what keeps state.json from
# growing without bound over the life of the process.
#
# 30 days was chosen as ample margin over any realistic reprocessing
# horizon: retry sweeps (classification_retry / notification_retry) and
# crash-recovery re-claims all complete far within it, so a legitimate
# retry of an already-owned job never loses its claim (see
# claim_cross_source_project), while genuinely distinct re-listings
# eventually clear the window. Cross-source dedup is only best-effort at
# the JSON/state layer; the durable per-identity SQLite job_uuid dedup in
# app.job_processor remains the strong guarantee even after this window
# expires.
_CROSS_SOURCE_TTL_SECONDS = 30 * 24 * 60 * 60

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-persist")
_EXECUTOR_POISONED = False

# save() does two rounds of blocking disk I/O (write_text + os.replace,
# for both state.json and its backup) with no timeout of its own. This
# executor has exactly one worker thread, so if that write ever blocks
# forever on the underlying syscall (e.g. a stalled disk, a hung
# network-mounted volume) every future state.run() call -- including
# ones from a completely different job -- queues up behind it forever,
# with no exception and nothing to log. This was diagnosed as the
# likely cause of a real incident (2 Sept 2026) where both Telegram
# and FreeHub ingestion silently stopped producing jobs simultaneously
# while the bot's command interface (which never touches this module)
# kept working, the container stayed alive, and nothing appeared in
# the errors table -- exactly the signature this class of failure
# would produce, with a restart (a fresh executor) being what actually
# fixed it. run() below exists to turn that same failure
# mode into a loud, recoverable error instead of a silent, permanent
# freeze the next time it happens.
_STATE_TIMEOUT_SECONDS = RUNTIME.state_timeout_seconds


class StuckExecutorError(RuntimeError):
    """Raised when a state-persistence call didn't return within
    _STATE_TIMEOUT_SECONDS. See the comment above _STATE_TIMEOUT_SECONDS
    for why this exists.

    The stuck worker thread cannot be forcibly killed, so the shared
    state backend is QUARANTINED rather than replaced:
    _EXECUTOR_POISONED is set and every subsequent state.run() call
    fails closed with this exception until the process restarts. A
    fresh executor is deliberately NOT installed -- it could let a
    second worker mutate the same JSON state concurrently with the
    still-stuck one. The failing call is loud (its caller's existing
    per-item exception handling, e.g. the Telegram/FreeHub worker
    loops, surfaces it), and a restart (a fresh process, hence a fresh
    executor) is the only recovery."""


class StateCorruptionError(RuntimeError):
    """Raised when state.json exists but cannot be parsed and no usable backup exists."""


class StateManager:
    def __init__(self):
        self.data = {}

    def load(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if not STATE_FILE.exists():
            self.data = {}
            self.save()
            return

        try:
            self.data = json.loads(STATE_FILE.read_text())
            return
        except Exception as e:
            parse_error = e

        backup_path = STATE_FILE.with_suffix(".bak.json")

        if backup_path.exists():
            try:
                self.data = json.loads(backup_path.read_text())
            except Exception:
                pass
            else:
                print(
                    f"[STATE] {STATE_FILE} is corrupted ({parse_error}); "
                    f"recovered from {backup_path}. Some very recent "
                    "watermark updates made after that backup was written "
                    "may be re-processed."
                )
                self.save()
                return

        raise StateCorruptionError(
            f"{STATE_FILE} exists but is not valid JSON ({parse_error}), "
            f"and no usable backup was found at {backup_path}. Refusing to "
            "start with a silently-reset state."
        )

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        temp_path = STATE_FILE.with_suffix(".tmp.json")

        temp_path.write_text(
            json.dumps(
                self.data,
                indent=4,
            )
        )

        os.replace(temp_path, STATE_FILE)

        backup_path = STATE_FILE.with_suffix(".bak.json")
        backup_temp_path = STATE_FILE.with_suffix(".bak.tmp.json")

        backup_temp_path.write_text(
            json.dumps(
                self.data,
                indent=4,
            )
        )
        os.replace(backup_temp_path, backup_path)

    async def run(self, func, *args, **kwargs):
        """Run a StateManager method on the single dedicated
        state-persistence thread and await its result.

        Wrapped in a timeout specifically because the underlying
        writes are blocking disk I/O with no timeout of their own --
        see the comment above _STATE_TIMEOUT_SECONDS for the incident
        this is defending against. On timeout the shared state backend
        is QUARANTINED: _EXECUTOR_POISONED is set and this and every
        later call raises StuckExecutorError (fail-closed) until the
        process restarts. The stuck worker thread is never forcibly
        killed and no fresh executor is installed -- installing one
        could let two workers mutate the same JSON state concurrently.
        Restart (a fresh executor) is the only safe recovery.
        """
        global _EXECUTOR_POISONED

        if _EXECUTOR_POISONED:
            raise StuckExecutorError(
                "State worker is quarantined after a timed-out operation; "
                "restart the process before reusing the state backend."
            )

        loop = asyncio.get_running_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs)),
                timeout=_STATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # wait_for cancels the Future, not the running thread. Starting
            # another executor here could let two workers mutate the same
            # JSON state concurrently. Quarantine instead; restart is the
            # only safe lifecycle boundary for a potentially stuck writer.
            _EXECUTOR_POISONED = True
            func_name = getattr(func, "__name__", repr(func))
            print(
                f"[STATE] {func_name} timed out after {_STATE_TIMEOUT_SECONDS}s. "
                "The worker thread cannot be killed safely, so the shared "
                "state backend is quarantined until process restart."
            )
            # Same reporting seam as app.logger.DBLogger.run -- see its
            # comment. Once poisoned every future call re-raises at the
            # guard above before reaching the success beat below, so this
            # can never be silently overwritten back to "alive".
            liveness.beat("state_worker", STATE_DEAD)
            raise StuckExecutorError(
                f"{func_name} did not complete within "
                f"{_STATE_TIMEOUT_SECONDS}s; state backend quarantined."
            ) from None
        liveness.beat("state_worker", STATE_ALIVE)
        return result

    def get_last_message_id(self, channel_id):
        return int(self.data.get(str(channel_id), 0))

    def set_last_message_id(self, channel_id, message_id):
        self.data[str(channel_id)] = message_id
        self.save()

    async def async_set_last_message_id(self, channel_id, message_id):
        await self.run(self.set_last_message_id, channel_id, message_id)

    # ------------------------------------------------------------
    # FreeHub dedup persistence.
    # ------------------------------------------------------------

    def get_freehub_seen(self, source: str) -> list:
        return list(self.data.get(_FREEHUB_KEY, {}).get(source, []))

    def set_freehub_seen(self, source: str, seen_ids: list):
        bucket = self.data.setdefault(_FREEHUB_KEY, {})
        bucket[source] = list(seen_ids)
        self.save()

    async def async_set_freehub_seen(self, source: str, seen_ids: list):
        await self.run(self.set_freehub_seen, source, seen_ids)

    def get_freehub_pending(self, source: str) -> list[dict]:
        bucket = self.data.get(_FREEHUB_PENDING_KEY, {})
        values = bucket.get(source, []) if isinstance(bucket, dict) else []
        return list(values) if isinstance(values, list) else []

    def set_freehub_pending(self, source: str, projects: list[dict]):
        bucket = self.data.setdefault(_FREEHUB_PENDING_KEY, {})
        bucket[source] = list(projects)
        self.save()

    async def async_set_freehub_pending(self, source: str, projects: list[dict]):
        await self.run(self.set_freehub_pending, source, projects)

    def get_freehub_backfill_page(self, source: str) -> int:
        bucket = self.data.get(_FREEHUB_CURSOR_KEY, {})
        try:
            return max(2, int(bucket.get(source, 2)))
        except (AttributeError, TypeError, ValueError):
            return 2

    def set_freehub_backfill_page(self, source: str, page: int):
        bucket = self.data.setdefault(_FREEHUB_CURSOR_KEY, {})
        bucket[source] = max(2, int(page))
        self.save()

    async def async_set_freehub_backfill_page(self, source: str, page: int):
        await self.run(self.set_freehub_backfill_page, source, page)

    # ------------------------------------------------------------
    # Cross-source dedup.
    #
    # IMPORTANT: claiming a project is one atomic operation because
    # every mutation runs on the single state executor thread.
    # This closes the check-then-add race between FreeHub and Telegram.
    # ------------------------------------------------------------

    def _prune_cross_source_seen(self, now: float):
        records = self.data.setdefault(_CROSS_SOURCE_SEEN_KEY, {})

        # Migrate the old list format if a previous version created it.
        if isinstance(records, list):
            records = {
                str(project_id): {
                    "job_uuid": "",
                    "claimed_at": now,
                }
                for project_id in records
            }
            self.data[_CROSS_SOURCE_SEEN_KEY] = records

        if not isinstance(records, dict):
            records = {}
            self.data[_CROSS_SOURCE_SEEN_KEY] = records

        cutoff = now - _CROSS_SOURCE_TTL_SECONDS

        expired = []
        for project_id, record in records.items():
            if not isinstance(record, dict):
                expired.append(project_id)
                continue

            claimed_at = record.get("claimed_at", 0)
            try:
                claimed_at = float(claimed_at)
            except (TypeError, ValueError):
                expired.append(project_id)
                continue

            if claimed_at < cutoff:
                expired.append(project_id)

        for project_id in expired:
            records.pop(project_id, None)

        return records

    def claim_cross_source_project(self, project_id: str, job_uuid: str) -> bool:
        """
        Atomically claim a project ID for a job.

        Returns True for the first claimant inside the active dedup
        window, and also for a retry from the same job_uuid that already
        owns the claim. A different job_uuid still loses the claim.
        The check and write happen in the same dedicated executor
        thread, so concurrent FreeHub/Telegram calls cannot both win.

        This idempotent ownership check is important for crash recovery:
        a durable job row may be retried after the process has already
        persisted its cross-source claim but before classification
        finished.
        """
        now = time.time()
        records = self._prune_cross_source_seen(now)

        project_id = str(project_id)
        existing = records.get(project_id)

        if existing is not None:
            # Retrying the same durable job after a crash must not make
            # it lose a claim it already owns. A different job_uuid is
            # still rejected as the cross-source duplicate.
            return existing.get("job_uuid") == job_uuid

        records[project_id] = {
            "job_uuid": job_uuid,
            "claimed_at": now,
        }
        self.save()
        return True

    async def async_claim_cross_source_project(
        self,
        project_id: str,
        job_uuid: str,
    ) -> bool:
        return await self.run(
            self.claim_cross_source_project,
            project_id,
            job_uuid,
        )

    def shutdown(self):
        """Deterministic lifecycle end: shut down the shared state
        persistence executor. Idempotent. Call only at process shutdown
        (see app.bot.run); after this call no state persistence can run."""
        global _EXECUTOR_POISONED
        try:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _EXECUTOR_POISONED = True


state = StateManager()
