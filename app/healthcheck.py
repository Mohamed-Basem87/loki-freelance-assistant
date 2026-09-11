"""Container health probe.

Runs as a short-lived subprocess (standard Docker HEALTHCHECK CMD
pattern), so it cannot inspect the running application's event loop
directly -- it can only check what's observable from the outside:
files on disk. It checks three genuinely distinct things and reports
which one failed, rather than folding them into a single "SQLite is
readable" signal:

  1. Persistence integrity  -- the SQLite database file exists and
     passes PRAGMA quick_check, and the JSON state file (if present)
     parses.
  2. Process health         -- the container's process table is
     observable at all (a minimal, cheap sanity check).
  3. Worker/runtime health  -- app.heartbeat.heartbeat_loop, a
     registered worker in the running application, has written a
     fresh heartbeat recently; every critical worker recorded in
     that snapshot reports an acceptable state (alive, or reconnecting
     which is a bounded backoff state, or shutdown which is a graceful
     exit); and no critical worker in the "alive" state has gone stale
     -- stopped beating/progressing while its task is still present.
     This is the only one of the three that actually reflects whether
     the asyncio workers are alive and making progress: the database
     can be perfectly intact while every worker task has died or the
     event loop has hung.

     The per-worker status closes the "heartbeat alive but a critical
     worker is dead" gap: the global heartbeat loop can keep ticking
     even while a critical ingestion worker (e.g. Telegram or FreeHub)
     has died, so health must inspect each worker individually, not
     just the aggregate tick. The per-worker staleness check closes the
     remaining gap: a worker whose task never ended and whose state
     stayed "alive" but which quietly stopped beating is otherwise
     indistinguishable from a healthy idle worker forever.

A container can fail (1) while passing (3), or vice versa -- report
whichever failed rather than a single undifferentiated "unhealthy".

Health model:
  - HEALTHY: All critical ingestion paths operational, database healthy
  - DEGRADED: Ingestion healthy but background subsystems have issues
    (retry workers, notification delivery workers)
  - UNHEALTHY: Ingestion failure, database/state corruption, or
    critical worker death
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

from app.runtime_config import RUNTIME

# A worker reporting "reconnecting" (bounded exponential backoff, e.g.
# the Telegram worker) is temporarily degraded, not dead: do NOT mark the
# container unhealthy for a bounded reconnection. A worker reporting
# "shutdown" is an intentional TaskGroup shutdown (only set on clean
# CancelledError in WorkerRegistry._run_tracked, never on an accidental
# death) -- the process is on its way out and the global tick will go
# stale on its own, so an intentional shutdown must not raise a spurious
# per-worker alarm. Anything else (dead, or missing/never registered) is
# a real problem.
_ACCEPTABLE_STATES = {"alive", "reconnecting", "shutdown"}

# Health states
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNHEALTHY = "UNHEALTHY"

# Adapter-path -> liveness key, for resolving a job-source config entry
# to the per-worker liveness key its worker actually beats. SourceWorker
# beats `source.id` (JobSource.id), so most entries map to themselves
# (FreeHubJobSource.id == "freehub"); the canonical live Telegram worker
# beats the liveness key "telegram" even though its job-source entry is
# configured as id "telegram_channels" -- the key the distributed workers
# (TelegramChannelWorker) and the heartbeat snapshot agree on.
_ADAPTER_LIVENESS_IDS = {
    "app.adapters.sources.freehub:FreeHubJobSource": "freehub",
    "app.adapters.sources.telegram:TelegramChannelJobSource": "telegram",
}

# Any job-source adapter under this module is backed by app.scraper_scheduler
# (a subprocess that refreshes a JSON snapshot on its own cadence) rather
# than a live network poller. The poller worker (e.g. "linkedin"/"wuzzuf")
# only proves the snapshot is being *read*; it says nothing about whether
# the snapshot is still being *refreshed* -- the scraper can fail every
# run forever (see scraper_scheduler.CONSECUTIVE_FAILURE_THRESHOLD) while
# the poller keeps calmly re-reading stale data and beating "alive". When
# any such source is actually running, the scheduler's own liveness key
# is just as critical as the poller's.
_SCRAPER_ADAPTER_MODULE_PREFIX = "app.adapters.sources.scraper_file:"
_SCRAPER_SCHEDULER_WORKER_ID = "scraper_scheduler"

# Persistence workers (app.logger.DBLogger, app.state.StateManager) report
# their own liveness key only when they have actually run at least once,
# and only ever move it to "dead" -- once quarantined after a timed-out
# operation, every later call re-raises before it could beat "alive"
# again, so "dead" here can only mean "still quarantined". Unlike the
# critical-worker set above, these are not required to be *present*
# (a freshly-started process, or one under test, may never have touched
# the DB/state backend yet) -- only a positive "dead" report fails
# health. This is deliberately independent of expected_critical_workers:
# persistence quarantine must fail the container regardless of which
# ingestion workers happen to be enabled in a given deployment.
_PERSISTENCE_WORKER_IDS = ("db_worker", "state_worker")


def expected_critical_workers(enabled_workers=None, job_sources=None):
    """Resolve the worker liveness keys the healthcheck must require from
    configuration, instead of a hardcoded list.

    The critical work is *enabled ingestion* -- every enabled job source
    whose id is in ENABLED_WORKERS must be present in the heartbeat
    snapshot -- plus the Telegram live worker when ENABLED_WORKERS asks
    for it (that worker is enabled through ENABLED_WORKERS directly, not
    through the job-sources registry), plus the heartbeat loop's own beat
    (advisory: it proves the event loop is turning, but is not itself an
    ingested data path). The classification/notification retry loops and the
    user-notification worker DO beat per-worker liveness (see
    heartbeat.sleep_with_beats) but are deliberately excluded here: they are
    not ingestion paths and are only enabled on some deployments, so requiring
    their liveness keys unconditionally would make an otherwise-healthy
    container fail health whenever a deployment does not run them. They remain
    observable via the snapshot for operators who opt to require them.

    Both ``enabled_workers`` and ``job_sources`` are injectable (tests),
    defaulting to the real runtime configuration. This mirrors the
    selection in app.workers.default_registry (/ enabled_source_ids):
    a job source only runs when it is both ``enabled`` and present in
    ENABLED_WORKERS, so the healthcheck requires exactly that same set.
    """
    import app.runtime_config as _rc

    enabled_workers = (
        set(enabled_workers)
        if enabled_workers is not None
        else set(getattr(_rc.RUNTIME, "enabled_workers", ()))
    )
    job_sources = (
        job_sources
        if job_sources is not None
        else getattr(_rc, "JOB_SOURCES", ())
    )

    critical = {
        _ADAPTER_LIVENESS_IDS.get(cfg.adapter, cfg.id)
        for cfg in job_sources
        if cfg.enabled and cfg.id in enabled_workers
    }
    if "telegram" in enabled_workers:
        critical.add("telegram")
    if any(
        cfg.enabled
        and cfg.id in enabled_workers
        and cfg.adapter.startswith(_SCRAPER_ADAPTER_MODULE_PREFIX)
        for cfg in job_sources
    ):
        critical.add(_SCRAPER_SCHEDULER_WORKER_ID)
    critical.add("__heartbeat__")
    return critical


# Per-worker staleness window, derived from the existing heartbeat
# configuration: a critical worker in state "alive" whose last beat is
# older than this many ticks of the configured snapshot cadence has
# stopped beating/progressing even though its task is still technically
# present. Three snapshots' worth of slack keeps normal scheduling jitter
# (a beat that lands just after a snapshot write) from causing false
# failures while still flagging a stuck worker within ~45s at the default
# 15s cadence -- worse than dead-but-recorded (instant) but far sooner
# than the global 90s tick expiry, and importantly it can NEVER be hidden
# by a still-ticking global heartbeat.
_WORKER_STALE_MULTIPLIER = 3
_WORKER_STALE_AFTER_SECONDS = _WORKER_STALE_MULTIPLIER * RUNTIME.heartbeat_interval_seconds


# SQLite writes are serialized to a single dedicated thread inside the
# application process, but the healthcheck is a SEPARATE process that
# opens its own read-only connection. Under the rollback journal a
# concurrent writer transaction holds the database lock, so `PRAGMA
# quick_check` (a read) can transiently hit "database is locked" / "database
# table is locked" / "database is busy" -- healthy write contention, NOT
# corruption. Corruption surfaces as a non-lock ValueError/DatabaseError
# ("database disk image is malformed", "file is not a database", ...) or a
# quick_check result other than 'ok', and those must keep failing health.
_LOCKED_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
)
_QUICK_CHECK_RETRIES = 3
_QUICK_CHECK_RETRY_DELAY_SECONDS = 0.5


def _is_locked_error(exc):
    message = str(exc).lower()
    return any(marker in message for marker in _LOCKED_ERROR_MARKERS)


def _quick_check_result(conn):
    """Run PRAGMA quick_check, transparently retrying a transient SQLite
    write-lock, and return the first row, or None when the database stays
    locked for the whole bounded retry window.

    A persistently locked database is a *contention* state, not evidence
    of corruption: quick_check could not authenticate the file, but
    nothing indicates it is damaged. The persistent-lock case is reported
    (see check_persistence_integrity) instead of raised, so the container
    is not torn down over a healthy-but-busy write lock. Every other
    sqlite3 error, and every quick_check row other than 'ok', is real
    corruption detection and keeps raising."""
    retries = _QUICK_CHECK_RETRIES
    while True:
        try:
            return conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            if not _is_locked_error(exc):
                raise
            if retries <= 0:
                return None
            time.sleep(_QUICK_CHECK_RETRY_DELAY_SECONDS)
            retries -= 1


def check_persistence_integrity(db_path, state_path):
    if not db_path.exists() or not db_path.is_file():
        raise RuntimeError(f"persistence integrity: database file is missing: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        result = _quick_check_result(conn)
        if result is None:
            # Bounded write-lock contention from the live writer process.
            # Not corruption; report instead of failing health (P2-D).
            print(
                "[HEALTHCHECK] persistence quick_check deferred: SQLite "
                "reported a held write lock (database is locked) for the "
                "whole retry window; this is healthy contention with the "
                "writer process, not corruption, so it is not treated as "
                "a persistence failure.",
                file=sys.stderr,
            )
        elif not result or result[0] != "ok":
            raise RuntimeError(f"persistence integrity: sqlite quick_check failed: {result!r}")
    finally:
        conn.close()
    if state_path.exists():
        with state_path.open(encoding="utf-8") as handle:
            try:
                json.load(handle)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"persistence integrity: state file is not valid JSON: {exc}") from exc


def check_process_health():
    if not Path("/proc/1/cmdline").exists():
        raise RuntimeError("process health: process table is unavailable")


def _read_heartbeat_payload(heartbeat_path):
    """Read the heartbeat JSON snapshot, returning (tick, workers,
    tick_monotonic).

    ``tick_monotonic`` may be None for a legacy/incomplete payload; the
    staleness check then falls back to state-only (matching the existing
    backward-compat handling of the pre-per-worker plain-float format).
    """
    if not heartbeat_path.exists():
        raise RuntimeError(
            f"worker/runtime health: heartbeat file not found: {heartbeat_path} "
            "(no worker has reported alive yet)"
        )
    try:
        raw = heartbeat_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"worker/runtime health: heartbeat file is unreadable: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Backward compatibility with the pre-per-worker plain-float format.
        try:
            tick = float(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"worker/runtime health: heartbeat file is unreadable: {exc}"
            ) from exc
        return tick, {}, None

    tick = data.get("tick")
    if tick is None:
        raise RuntimeError("worker/runtime health: heartbeat file has no tick timestamp")
    workers = data.get("workers") or {}
    tick_monotonic = data.get("tick_monotonic")
    return tick, workers, tick_monotonic


def check_worker_runtime_health(
    heartbeat_path,
    max_age_seconds,
    critical_workers=None,
    worker_stale_after_seconds=_WORKER_STALE_AFTER_SECONDS,
):
    """
    Returns tuple of (health_state, details).
    health_state is one of HEALTHY, DEGRADED, UNHEALTHY.
    details is a dict with information about the check results.
    """
    # Handle missing heartbeat file
    if not heartbeat_path.exists():
        return UNHEALTHY, {"checks": {"heartbeat_file": {"status": "fail", "reason": "heartbeat file not found"}}}

    tick, workers, tick_monotonic = _read_heartbeat_payload(heartbeat_path)

    # Which workers are critical is a property of the deployment's
    # configuration, not of this function: derive it from the enabled
    # worker/job-source set (see expected_critical_workers) and only fall
    # back to an explicit set when a caller has a concrete reason to
    # override it. Never anchor a stale hardcoded list here.
    if critical_workers is None:
        critical_workers = expected_critical_workers()

    details = {
        "critical_workers": list(critical_workers),
        "workers_present": list(workers.keys()) if workers else [],
        "checks": {}
    }

    # 1. The aggregate loop tick must be fresh (the event loop is turning).
    age = time.time() - float(tick)
    if age > max_age_seconds:
        details["checks"]["heartbeat_stale"] = {
            "status": "fail",
            "age_seconds": round(age, 1),
            "max_age_seconds": max_age_seconds
        }
        return UNHEALTHY, details

    details["checks"]["heartbeat_stale"] = {
        "status": "pass",
        "age_seconds": round(age, 1),
        "max_age_seconds": max_age_seconds
    }

    # 1.5. A quarantined persistence worker (DB or state) must fail health
    #    immediately and unconditionally, regardless of critical_workers.
    #    See app.logger.DBLogger.run / app.state.StateManager.run: once
    #    either backend is quarantined after a timed-out operation, every
    #    future call fails closed until process restart, and the app can
    #    no longer persist anything -- but the separate, out-of-process
    #    check_persistence_integrity() cannot see this in-process flag: a
    #    quarantined worker holds no SQLite lock, so that check alone
    #    would keep passing. Only fires on a positive "dead" report --
    #    a worker that has simply never run yet (fresh process, or a test
    #    that never touches the DB/state backend) is not required to be
    #    present here, unlike the critical-worker check below.
    if workers:
        quarantined = [
            wid for wid in _PERSISTENCE_WORKER_IDS
            if (workers.get(wid) or {}).get("state") == "dead"
        ]
        if quarantined:
            details["checks"]["persistence_worker_quarantined"] = {
                "status": "fail",
                "workers": quarantined,
            }
            return UNHEALTHY, details

    # 2. Every critical worker must be present and in an acceptable state.
    #    A dead (or silently-missing) critical worker must fail health even
    #    when the aggregate tick is fresh -- this is the explicit
    #    "Telegram worker dead + heartbeat alive must NOT appear healthy"
    #    guarantee. The global tick proves the loop turns; only a per-worker
    #    check proves the worker itself is alive.
    #
    #    The per-worker check only runs when the snapshot actually carries
    #    worker data (the JSON format written by app.heartbeat.WorkerLiveness).
    #    A legacy plain-float heartbeat carries no per-worker information, so
    #    it can only attest to aggregate freshness (backward compatibility:
    #    an old-format heartbeat must not spuriously fail on "missing" workers
    #    it never recorded).
    if workers:
        missing = []
        bad_state = []
        stale = []
        reconnecting = []
        for wid in critical_workers:
            entry = workers.get(wid)
            if entry is None:
                missing.append(wid)
                continue
            state = entry.get("state")
            if state not in _ACCEPTABLE_STATES:
                bad_state.append(f"{wid}={state!r}")
                continue

            if state == "reconnecting":
                reconnecting.append(wid)
                continue

            # Staleness: an "alive" worker that has stopped beating is
            # hung even though its task is still present and its state
            # never changed -- exactly the "present but stopped making
            # progress" case a state-only check cannot see.
            #
            # "reconnecting" is explicitly exempt: it *is* the bounded
            # backoff state, and its beats naturally space out to the
            # backoff delay (up to Telegram's 60s cap), so applying the
            # normal freshness window would falsely alarm a worker that
            # is intentionally sleeping. "shutdown" is exempt for the
            # same reason it is acceptable: the process is exiting.
            # Staleness is measured within the file (tick_monotonic -
            # last_beat_monotonic) so no clock crosses a process
            # boundary, and only when the payload actually records both
            # timestamps (forward/backward compatible otherwise).
            if (
                state == "alive"
                and tick_monotonic is not None
            ):
                last_beat = entry.get("last_beat_monotonic")
                if last_beat is not None:
                    beat_age = float(tick_monotonic) - float(last_beat)
                    if beat_age > worker_stale_after_seconds:
                        stale.append(
                            f"{wid} (last beat {beat_age:.0f}s before snapshot, "
                            f"max {worker_stale_after_seconds:.0f}s)"
                        )

        if missing:
            details["checks"]["missing_workers"] = {
                "status": "fail",
                "missing": missing
            }
            return UNHEALTHY, details

        if bad_state:
            details["checks"]["bad_state"] = {
                "status": "fail",
                "workers": bad_state
            }
            return UNHEALTHY, details

        if stale:
            details["checks"]["stale_workers"] = {
                "status": "fail",
                "workers": stale
            }
            return UNHEALTHY, details

        details["checks"]["workers_healthy"] = {
            "status": "pass",
            "workers": list(critical_workers),
            "reconnecting": reconnecting
        }

        # If any critical worker is reconnecting, the overall state is DEGRADED
        if reconnecting:
            details["checks"]["workers_reconnecting"] = {
                "status": "degraded",
                "workers": reconnecting
            }
            return DEGRADED, details

        return HEALTHY, details

    # No workers in snapshot (legacy format) - only heartbeat tick is available
    details["checks"]["workers_healthy"] = {
        "status": "pass",
        "note": "legacy heartbeat format - only aggregate tick checked"
    }
    return HEALTHY, details


def main():
    # Read every path/threshold from RUNTIME, the same resolved
    # configuration the running application itself uses -- not
    # separate os.getenv() calls with their own hardcoded defaults
    # and no path resolution against BASE_DIR (audit finding:
    # configuration consistency; see RuntimePolicy.heartbeat_file_path).
    db_path = Path(RUNTIME.database_file_path)
    state_path = Path(RUNTIME.state_file_path)
    heartbeat_path = Path(RUNTIME.heartbeat_file_path)
    heartbeat_max_age = RUNTIME.heartbeat_max_age_seconds

    try:
        check_process_health()
    except RuntimeError as exc:
        print(f"{UNHEALTHY}: {exc}")
        raise SystemExit(1)

    try:
        check_persistence_integrity(db_path, state_path)
    except RuntimeError as exc:
        print(f"{UNHEALTHY}: {exc}")
        raise SystemExit(1)

    health_state, details = check_worker_runtime_health(heartbeat_path, heartbeat_max_age)

    if health_state == UNHEALTHY:
        # Find the failing check for the message
        failed_checks = [k for k, v in details.get("checks", {}).items() if v.get("status") == "fail"]
        msg = f"{UNHEALTHY}: worker/runtime health failed: {', '.join(failed_checks)}"
        print(msg)
        raise SystemExit(1)
    elif health_state == DEGRADED:
        degraded_checks = [k for k, v in details.get("checks", {}).items() if v.get("status") == "degraded"]
        msg = f"{DEGRADED}: {', '.join(degraded_checks)}"
        print(msg)
        raise SystemExit(0)  # DEGRADED is still a passing healthcheck for Docker
    else:
        print(f"{HEALTHY}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        raise SystemExit(1)

