import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parent.parent / "database" / "state.json"

_FREEHUB_KEY = "_freehub_seen"

# Cross-source dedup records are stored as:
# {
#     "project_id": {"job_uuid": "...", "claimed_at": 1234567890.0}
# }
#
# Keeping the timestamp lets us prune old IDs instead of growing
# state.json forever.
_CROSS_SOURCE_SEEN_KEY = "_cross_source_seen"
_CROSS_SOURCE_TTL_SECONDS = 30 * 24 * 60 * 60

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-persist")

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
# fixed it. _run_with_timeout below exists to turn that same failure
# mode into a loud, recoverable error instead of a silent, permanent
# freeze the next time it happens.
_STATE_TIMEOUT_SECONDS = 30


class StuckExecutorError(RuntimeError):
    """Raised when a state-persistence call didn't return within
    _STATE_TIMEOUT_SECONDS. See the comment above _STATE_TIMEOUT_SECONDS
    for why this exists. The stuck worker thread cannot be forcibly
    killed and will leak until the process restarts, but a fresh
    executor is installed immediately so no further call queues up
    behind it -- this specific call still fails (its caller's existing
    per-item exception handling, e.g. the Telegram/FreeHub worker
    loops, is what recovers it, same as any other job-level failure),
    but ingestion as a whole is not permanently blocked by it."""


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
        this is defending against. On timeout, the presumed-stuck
        executor is replaced with a fresh one before raising, so this
        one call fails but future calls are not queued behind a
        permanently blocked thread.
        """
        global _EXECUTOR

        loop = asyncio.get_running_loop()

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs)),
                timeout=_STATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="state-persist"
            )
            func_name = getattr(func, "__name__", repr(func))
            print(
                f"[STATE] {func_name} did not return within "
                f"{_STATE_TIMEOUT_SECONDS}s -- its worker thread is "
                f"presumed permanently stuck (e.g. a stalled disk "
                f"write) and has been replaced so ingestion isn't "
                f"silently blocked forever. This one call failed; the "
                f"caller's normal retry/recovery path handles it from "
                f"here."
            )
            raise StuckExecutorError(
                f"{func_name} did not complete within "
                f"{_STATE_TIMEOUT_SECONDS}s; its worker thread was "
                f"replaced."
            ) from None

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


state = StateManager()
