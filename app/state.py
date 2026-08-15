import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


STATE_FILE = Path(__file__).resolve().parent.parent / "database" / "state.json"

# Top-level key holding FreeHub's per-source "seen project uid" lists.
# Kept separate from the per-channel Telegram watermarks (which live
# as bare top-level keys, e.g. "-1001234": 5678) so the two schemas
# don't collide.
_FREEHUB_KEY = "_freehub_seen"

# Single dedicated worker thread for all state-file persistence, same
# pattern as app.logger.DBLogger's _EXECUTOR. StateManager.save()
# does blocking filesystem I/O (temp-file write + os.replace); calling
# it directly from a coroutine (as set_last_message_id/
# set_freehub_seen used to) blocks the single shared event loop for
# the duration of that write, and since Telegram (app.handlers.
# telegram) and FreeHub (app.freehub) both persist state concurrently
# under asyncio.gather, two saves could also race on the same
# STATE_FILE/temp_path if simply offloaded to the default
# asyncio.to_thread pool (which allows more than one thread at once).
        # Funneling every state write through one dedicated thread makes
        # writes strictly serial -- exactly the same guarantee the DB
        # logger already relies on -- without blocking the event loop.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-persist")


class StateCorruptionError(RuntimeError):
    """Raised when state.json exists but cannot be parsed, and no
    usable .bak snapshot exists to recover from either. Deliberately
    fatal: the alternative (silently falling back to an empty state)
    is indistinguishable from a genuine first run and causes every
    channel/source watermark to reset to never-seen, dropping
    everything posted since the last good save with zero errors
    logged (see the audit's P0-3)."""


class StateManager:
    def __init__(self):
        self.data = {}

    def load(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        if not STATE_FILE.exists():
            # Genuinely nothing on disk yet -- this is the only case
            # that should be treated as a real first run.
            self.data = {}
            self.save()
            return

        try:
            self.data = json.loads(STATE_FILE.read_text())
            return
        except Exception as e:
            parse_error = e

        # The file exists but is not valid JSON -- a corrupted write,
        # not a first run. Never silently zero the watermark here, or
        # every channel/source looks like "never seen" and recovery
        # skips the backfill entirely (permanent, unlogged job loss).
        # Try the last known-good snapshot before giving up.
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
                # Persist the recovered data as the current file so a
                # later crash doesn't have to fall back twice, and
                # refresh the backup pointer.
                self.save()
                return

        raise StateCorruptionError(
            f"{STATE_FILE} exists but is not valid JSON ({parse_error}), "
            f"and no usable backup was found at {backup_path}. Refusing to "
            "start with a silently-reset state, since every channel/source "
            "watermark would look like a first run and skip backfilling "
            "everything posted since the last good save. Restore a known-"
            "good state.json (or delete it only if you are certain this "
            "deployment has never run before) and restart."
        )

    def save(self):
        # Write atomically: a crash or power-loss mid-write to the
        # *actual* state file would corrupt it, and load() treats a
        # corrupt file as "no state at all" -- silently resetting
        # every channel/source back to never-seen and triggering a
        # full re-recovery (duplicate notifications) on next startup.
        # Writing to a temp file and rename()-ing over the real path
        # (same pattern already used by app.logger.DBLogger.save)
        # means the real file is only ever replaced by a complete,
        # valid write.
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        temp_path = STATE_FILE.with_suffix(".tmp.json")

        temp_path.write_text(
            json.dumps(
                self.data,
                indent=4,
            )
        )

        os.replace(temp_path, STATE_FILE)

        # Keep a last-known-good snapshot alongside the live file. If
        # the live file is later found corrupted (partial write,
        # truncated by a power loss, etc.), load() falls back to this
        # instead of silently treating the deployment as brand new.
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
        """
        Run a bound StateManager method (currently only the mutating
        setters need this -- load()/save() called directly are fine
        since they only happen at startup, before any concurrent
        Telegram/FreeHub tasks exist) on the single dedicated
        state-persistence thread and await its result.

        Async callers (app.handlers.telegram, app.freehub) must use
        the async_set_last_message_id/async_set_freehub_seen wrappers
        below instead of calling set_last_message_id/
        set_freehub_seen directly, for the same reason job_processor
        etc. must go through DBLogger.run() instead of calling
        logger methods directly: it keeps every write strictly
        serialized on one thread and off the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs))

    def get_last_message_id(self, channel_id):
        return int(self.data.get(str(channel_id), 0))

    def set_last_message_id(self, channel_id, message_id):
        self.data[str(channel_id)] = message_id
        self.save()

    async def async_set_last_message_id(self, channel_id, message_id):
        """Async-safe wrapper around set_last_message_id -- see run()."""
        await self.run(self.set_last_message_id, channel_id, message_id)

    # ------------------------------------------------------------
    # FreeHub dedup persistence.
    #
    # Previously this lived entirely in an in-memory deque in
    # app.freehub, which meant every process restart silently reset
    # it -- any project posted between shutdown and the next
    # successful poll was never recovered (worse than the Telegram
    # side, which at least persisted a watermark). Persisting the
    # same "seen" list here, alongside the Telegram state, gives
    # FreeHub the same restart-survives-recovery guarantee.
    # ------------------------------------------------------------

    def get_freehub_seen(self, source: str) -> list:
        return list(self.data.get(_FREEHUB_KEY, {}).get(source, []))

    def set_freehub_seen(self, source: str, seen_ids: list):
        bucket = self.data.setdefault(_FREEHUB_KEY, {})
        bucket[source] = list(seen_ids)
        self.save()

    async def async_set_freehub_seen(self, source: str, seen_ids: list):
        """Async-safe wrapper around set_freehub_seen -- see run()."""
        await self.run(self.set_freehub_seen, source, seen_ids)


state = StateManager()
