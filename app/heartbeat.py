"""Lightweight worker/runtime heartbeat.

app/healthcheck.py runs as a separate, short-lived subprocess (that is
how Docker HEALTHCHECK works), so it cannot inspect the running
application's event loop directly. Before this module existed, the
only signal it had was "is the SQLite file readable and structurally
intact", which says nothing about whether the actual asyncio workers
(FreeHub polling, classification retries, notification retries) are
still alive and making progress -- a hung event loop or a worker task
that silently died still leaves the database perfectly readable.

heartbeat_loop() is registered as an ordinary WorkerRegistry worker; it
writes an aggregated liveness snapshot (including per-worker status) to
a small JSON file on a fixed cadence. healthcheck.py then treats a
missing or stale heartbeat file -- or a stale *critical* worker within
it -- as unhealthy, giving it a real (if approximate) signal for
worker/runtime health that is independent of persistence integrity.

Per-worker liveness (BUG #2 remediation):

  The old heartbeat was a single global timestamp written by
  heartbeat_loop. That meant a critical worker could die silently
  (e.g. the Telegram worker's run_until_disconnected() returning
  before the reconnect fix) while heartbeat_loop -- a separate,
  always-healthy task -- kept beating, so the container appeared
  healthy even though the one worker that matters was dead.

  Fix: a shared in-process WorkerLiveness registry maps each
  registered worker id to its state ({alive|reconnecting|dead}) and a
  monotonic last-beat. Workers call beat() as they make progress; the
  Telegram worker explicitly reports a "reconnecting" state during its
  backoff so the healthcheck can recognize that as temporarily
  degraded rather than instantly unhealthy, and a worker marked dead
  for any reason (or one that simply stops beating) is observable.

  heartbeat_loop() serializes this registry into the heartbeat file as
  JSON, so healthcheck.py can see both the global loop tick and every
  worker's individual status. A dead Telegram worker can no longer be
  hidden behind a live heartbeat loop.
"""
import asyncio
import json
import threading
import time
from pathlib import Path

from app.runtime_config import RUNTIME

# Resolved once, consistently with every other configured path in the
# process (see runtime_config.RuntimePolicy.heartbeat_file_path and its
# docstring note on _resolve_path -- audit finding: configuration
# consistency). Do not re-read HEARTBEAT_FILE_PATH from the environment
# here or in healthcheck.py; import RUNTIME instead so there is exactly
# one interpretation of this path in the whole process tree.
HEARTBEAT_FILE_PATH = Path(RUNTIME.heartbeat_file_path)
HEARTBEAT_INTERVAL_SECONDS = RUNTIME.heartbeat_interval_seconds


# Worker lifecycle states a worker can report. These are the on-disk
# vocabulary healthcheck.py interprets.
STATE_ALIVE = "alive"
STATE_RECONNECTING = "reconnecting"
STATE_DEAD = "dead"
STATE_SHUTDOWN = "shutdown"

# Human-facing classification of a worker's runtime progress. These names
# document the RUNNING/HEALTHY/RETRYING/STALLED/DEAD vocabulary the audit
# asks for and map onto the on-disk states healthcheck.py actually
# interprets: RUNNING/HEALTHY both report "alive" (the worker's task is
# present and beating); RETRYING reports "alive" too -- an actively
# retrying/backing-off worker is healthy, not degraded (like the Telegram
# "reconnecting" state); DEAD reports "dead" and STALLED is the one the
# healthcheck infers on its own (an "alive" worker whose beats stopped
# coming within the staleness window). Keeping these as constants on the
# liveness registry means the retry workers and the healthcheck stay in
# lockstep without hardcoding magic strings in each worker.
STATE_RUNNING = STATE_ALIVE
STATE_HEALTHY = STATE_ALIVE
STATE_RETRYING = STATE_ALIVE
_STALLED = "stalled"
STATE_STALLED = _STALLED
STATE_DEAD = STATE_DEAD

# Sub-window cadence for liveness beats during a worker's long idle/backoff
# sleeps. The healthcheck flags a critical "alive" worker as stale/hung when
# its last beat is older than 3 * heartbeat_interval (45s at the default 15s
# cadence). A retry/poll interval (e.g. 60s+) may exceed that window, so
# reporting liveness only once per loop iteration would flag a healthy worker
# stale while it merely sleeps between sweeps. Beating during the sleep keeps
# the maximum beat gap far inside the staleness window regardless of the
# configured interval.
LIVENESS_BEAT_CADENCE_SECONDS = 10.0


async def sleep_with_beats(seconds, worker_id, state=STATE_ALIVE, cadence=None):
    """Sleep for ``seconds`` while beating the worker's liveness at a
    fixed sub-window cadence.

    The healthcheck's staleness detection is a function of when the last
    beat happened, not of the worker's loop cadence. A worker whose idle
    sleep (poll or retry interval) is longer than the staleness window
    must therefore beat *during* the sleep rather than only once per loop
    iteration. This helper interleaves small sleeps with liveness beats so
    the maximum gap between beats is always ``cadence`` (independent of
    ``seconds``), and a healthy idle worker is never misreported as
    stalled/dead.
    """
    cadence = LIVENESS_BEAT_CADENCE_SECONDS if cadence is None else float(cadence)
    remaining = float(seconds)
    while remaining > 0:
        step = min(cadence, remaining)
        await asyncio.sleep(step)
        remaining -= step
        liveness.beat(worker_id, state)



class WorkerLiveness:
    """In-process registry of per-worker liveness.

    Thread-safe: workers beat from their own event-loop tasks, but the
    registry may also be written from a non-loop context, so all access
    is guarded by a lock. Kept deliberately simple -- it only records
    id -> (state, monotonic last-beat) -- it carries no business logic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._states = {}

    def register(self, worker_id, state=STATE_ALIVE):
        with self._lock:
            self._states.setdefault(worker_id, {
                "state": state,
                "last_beat": time.monotonic(),
            })

    def beat(self, worker_id, state=STATE_ALIVE):
        with self._lock:
            self._states[worker_id] = {
                "state": state,
                "last_beat": time.monotonic(),
            }

    def state(self, worker_id):
        with self._lock:
            entry = self._states.get(worker_id)
        if entry is None:
            return None
        return dict(entry)

    def set_state(self, worker_id, state):
        with self._lock:
            entry = self._states.get(worker_id)
            if entry is None:
                entry = {"state": state, "last_beat": time.monotonic()}
                self._states[worker_id] = entry
            else:
                entry["state"] = state

    def snapshot(self):
        """Return {worker_id: {"state": ..., "last_beat": monotonic}}."""
        with self._lock:
            return {wid: dict(e) for wid, e in self._states.items()}

    def ids(self):
        with self._lock:
            return list(self._states.keys())


# Single shared registry for the whole process.
liveness = WorkerLiveness()


def write_heartbeat(now=None, path=None):
    """Write the aggregated liveness snapshot to the heartbeat file.

    The file now carries a JSON payload: a global loop tick timestamp
    (backward compatible with code that read a bare float) plus the
    per-worker registry snapshot.

    ``tick_monotonic`` is captured in the same write as the worker
    snapshot, on the same ``time.monotonic()`` clock the workers beat
    on. healthcheck.py (a separate short-lived subprocess) measures a
    worker's staleness as ``tick_monotonic - last_beat_monotonic`` --
    a within-file comparison that never crosses process/clock domains,
    unlike subtracting an arbitrary monotonic value read from disk.
    """
    now = time.time() if now is None else now
    path = HEARTBEAT_FILE_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    payload = {
        "tick": now,
        "tick_monotonic": time.monotonic(),
        "workers": {
            wid: {
                "state": entry["state"],
                "last_beat_monotonic": entry["last_beat"],
            }
            for wid, entry in liveness.snapshot().items()
        },
    }

    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(path)  # atomic rename on POSIX: never a half-written file


async def heartbeat_loop(interval=None, path=None):
    """Runs forever as a registered worker.

    Writes immediately on the first tick (so the heartbeat file exists
    quickly after startup, well inside the Docker healthcheck's
    start_period) and then on a fixed cadence. This beats the registry
    loop's own liveness so a stalled heartbeat loop is itself detectable,
    and the aggregated per-worker snapshot is what healthcheck reads.
    """
    interval = HEARTBEAT_INTERVAL_SECONDS if interval is None else interval
    liveness.beat("__heartbeat__")
    while True:
        write_heartbeat(path=path)
        liveness.beat("__heartbeat__")
        await asyncio.sleep(interval)
