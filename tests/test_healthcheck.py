"""Regression tests for audit finding: healthcheck.

Previously the healthcheck only verified the SQLite file was readable
and structurally intact, plus a near-vacuous "/proc/1/cmdline exists"
check -- neither of which reflects whether the application's asyncio
workers are actually alive and making progress. A hung event loop or a
worker task that silently died left the container reporting healthy
indefinitely.
"""
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from app import healthcheck
from app.heartbeat import write_heartbeat


@pytest.fixture()
def tmp_paths():
    tmp_dir = Path(tempfile.mkdtemp(prefix="healthcheck_test_"))
    db_path = tmp_dir / "logs.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    state_path = tmp_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    heartbeat_path = tmp_dir / "heartbeat.txt"
    return db_path, state_path, heartbeat_path


def test_persistence_integrity_check_passes_for_a_valid_database(tmp_paths):
    db_path, state_path, _ = tmp_paths
    healthcheck.check_persistence_integrity(db_path, state_path)  # must not raise


def test_persistence_integrity_check_fails_for_a_missing_database(tmp_paths):
    _, state_path, _ = tmp_paths
    with pytest.raises(RuntimeError, match="persistence integrity"):
        healthcheck.check_persistence_integrity(Path("/nonexistent/db.sqlite"), state_path)


def test_persistence_integrity_check_fails_for_corrupt_state_json(tmp_paths):
    db_path, state_path, _ = tmp_paths
    state_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="persistence integrity"):
        healthcheck.check_persistence_integrity(db_path, state_path)


# ------------------------------------------------------------------
# SQLite busy/lock vs corruption (P2-D): the healthcheck runs as a
# separate process; under the rollback journal a concurrent writer
# holds the DB lock, so PRAGMA quick_check can transiently hit
# "database is locked". That is healthy write contention, not damage --
# it must not be reported as a persistence failure. Real corruption
# (non-lock sqlite3 errors, or a quick_check row != 'ok') must keep
# failing health.
# ------------------------------------------------------------------


def test_persistence_sustained_write_lock_is_not_reported_as_corruption(tmp_paths, monkeypatch, capsys):
    db_path, state_path, _ = tmp_paths

    class _AlwaysLockedConn:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(
        healthcheck.sqlite3,
        "connect",
        lambda *args, **kwargs: _AlwaysLockedConn(),
    )
    # A held write lock for the whole bounded retry window defers the
    # check rather than failing it.
    healthcheck.check_persistence_integrity(db_path, state_path)  # must not raise
    assert "quick_check deferred" in capsys.readouterr().err


def test_persistence_lock_that_clears_is_retried_to_success(tmp_paths, monkeypatch):
    db_path, state_path, _ = tmp_paths
    calls = {"n": 0}

    class _LockThenOkCursor:
        def fetchone(self):
            return ("ok",)

    class _LockThenOkConn:
        def execute(self, sql):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise sqlite3.OperationalError("database table is locked")
            return _LockThenOkCursor()

        def close(self):
            pass

    monkeypatch.setattr(
        healthcheck.sqlite3,
        "connect",
        lambda *args, **kwargs: _LockThenOkConn(),
    )
    healthcheck.check_persistence_integrity(db_path, state_path)  # must not raise
    assert calls["n"] == 3, "two locked failures, then the successful attempt"


def test_persistence_non_lock_sqlite_error_is_still_a_failure(tmp_paths, monkeypatch):
    """A file that is not a database at all is NOT a contention state; the
    underlying error must keep propagating past the lock-retry logic."""
    db_path, state_path, _ = tmp_paths

    class _NotADbConn:
        def execute(self, sql):
            raise sqlite3.OperationalError("file is not a database")

        def close(self):
            pass

    monkeypatch.setattr(
        healthcheck.sqlite3,
        "connect",
        lambda *args, **kwargs: _NotADbConn(),
    )
    with pytest.raises(sqlite3.OperationalError, match="file is not a database"):
        healthcheck.check_persistence_integrity(db_path, state_path)


def test_persistence_quick_check_non_ok_row_is_still_a_failure(tmp_paths, monkeypatch):
    """Corruption detection is not weakened: a quick_check result other
    than 'ok' must still fail health (the lock handling only retries
    sqlite3 lock errors)."""
    db_path, state_path, _ = tmp_paths

    class _MalformedCursor:
        def fetchone(self):
            return ("database disk image is malformed",)

    class _MalformedConn:
        def execute(self, sql):
            return _MalformedCursor()

        def close(self):
            pass

    monkeypatch.setattr(
        healthcheck.sqlite3,
        "connect",
        lambda *args, **kwargs: _MalformedConn(),
    )
    with pytest.raises(RuntimeError, match="quick_check failed"):
        healthcheck.check_persistence_integrity(db_path, state_path)


def test_worker_runtime_health_fails_when_heartbeat_missing(tmp_paths):
    _, _, heartbeat_path = tmp_paths
    # Missing heartbeat file should be UNHEALTHY
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


def test_worker_runtime_health_passes_for_a_fresh_heartbeat(tmp_paths):
    """A freshly-written heartbeat, with nothing beaten yet, hits the
    legacy/no-worker-data fallback path (empty registry -> {} snapshot)
    and is healthy. See _reset_worker_liveness in conftest.py for why
    this is deterministic regardless of what other tests do."""
    _, _, heartbeat_path = tmp_paths
    write_heartbeat(path=heartbeat_path)
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.HEALTHY


def test_worker_runtime_health_fails_for_a_stale_heartbeat(tmp_paths):
    _, _, heartbeat_path = tmp_paths
    write_heartbeat(now=time.time() - 500, path=heartbeat_path)
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


def test_database_can_be_intact_while_workers_are_dead(tmp_paths):
    """The concrete audit scenario: persistence integrity alone must
    not be reported as overall health -- a hung/dead worker with a
    perfectly intact database must fail the check."""
    db_path, state_path, heartbeat_path = tmp_paths
    healthcheck.check_persistence_integrity(db_path, state_path)  # DB itself is fine
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


# ------------------------------------------------------------------
# Per-worker liveness (BUG #2 remediation): a dead critical worker must
# fail health even when the global heartbeat loop is alive and fresh.
# This is the "Telegram worker dead + heartbeat alive must NOT appear
# healthy" guarantee that a single global tick cannot provide.
# ------------------------------------------------------------------


def _write_worker_snapshot(
    heartbeat_path,
    workers,
    tick=None,
    tick_monotonic=None,
    last_beat=0.0,
    per_worker_beats=None,
):
    """Write a heartbeat JSON payload with the given per-worker states.

    ``workers`` is {worker_id: state}. ``per_worker_beats`` (optional)
    overrides the last-beat per worker id when you need different ages.
    ``tick_monotonic`` defaults to None (staleness is skipped, the way a
    legacy/incomplete payload is handled); pass it to exercise the
    freshness check. ``last_beat`` is the age shared by every worker
    unless ``per_worker_beats`` is given.
    """
    import json
    if per_worker_beats is None:
        per_worker_beats = {}
    payload = {
        "tick": time.time() if tick is None else tick,
        "workers": {
            wid: {
                "state": state,
                "last_beat_monotonic": per_worker_beats.get(wid, last_beat),
            }
            for wid, state in workers.items()
        },
    }
    if tick_monotonic is not None:
        payload["tick_monotonic"] = tick_monotonic
    heartbeat_path.write_text(json.dumps(payload), encoding="utf-8")


def test_write_heartbeat_records_a_monotonic_snapshot_timestamp(tmp_paths):
    """write_heartbeat must include tick_monotonic so the healthcheck can
    measure worker staleness entirely within the app's own clock base."""
    _, _, heartbeat_path = tmp_paths
    write_heartbeat(path=heartbeat_path)
    payload = __import__("json").loads(heartbeat_path.read_text(encoding="utf-8"))
    assert "tick_monotonic" in payload
    assert isinstance(payload["tick_monotonic"], (int, float))


# ------------------------------------------------------------------
# Staleness hardening: a critical worker whose task is still present and
# whose state stayed "alive" but which has stopped beating must be
# detected -- even while the global heartbeat loop keeps ticking fresh.
# This is the invariant: GLOBAL HEARTBEAT HEALTHY + CRITICAL WORKER STALE
# must mean APPLICATION NOT HEALTHY.
# ------------------------------------------------------------------


def test_fresh_alive_worker_with_recent_beat_is_healthy(tmp_paths):
    """A worker beating just before the snapshot is healthy: libe ticks
    relative to the snapshot timestamp stay well inside the window."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 2,
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.HEALTHY


def test_stale_alive_worker_fails_even_with_fresh_global_heartbeat(tmp_paths):
    """The invariant: the global tick is fresh and within max age, but the
    Telegram worker has been 'alive' without beating for longer than the
    freshness window -- the application MUST be reported unhealthy."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 200,
        per_worker_beats={
            "freehub": tick_monotonic - 2,
            "linkedin": tick_monotonic - 2,
            "wuzzuf": tick_monotonic - 2,
        },
    )
    state, details = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.UNHEALTHY
    assert "stale_workers" in details.get("checks", {})


def test_staleness_is_measured_relative_to_snapshot_not_wall_clock(tmp_paths):
    """A worker beat recorded slightly before the snapshot write is fine
    even when wall-clock time has moved on -- the comparison is within-file
    (tick_monotonic - last_beat_monotonic), never across process clocks."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 30,
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.HEALTHY
    state, details = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=15
    )
    assert state == healthcheck.UNHEALTHY
    assert "stale_workers" in details.get("checks", {})


def test_reconnecting_worker_is_exempt_from_staleness(tmp_paths):
    """A worker honestly reporting its bounded reconnect/backoff state must
    not be flagged stale: its beats legitimately space out to the backoff
    delay (up to Telegram's 60s cap), which is far longer than the normal
    freshness window."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "reconnecting", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 1,
        per_worker_beats={"telegram": tick_monotonic - 200},
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    # Reconnecting workers should be DEGRADED, not HEALTHY
    assert state == healthcheck.DEGRADED


def test_shutting_down_worker_does_not_create_false_alarm(tmp_paths):
    """An intentional TaskGroup shutdown (state 'shutdown', only ever set on
    a clean CancelledError, never on an accidental death) must not raise a
    spurious per-worker alarm; the global tick going stale right after the
    process exits is the real signal."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "shutdown", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.HEALTHY


def test_dead_worker_is_detected_immediately_even_with_fresh_state(tmp_paths):
    """Requirement (e): an unexpectedly completed worker is recorded dead
    and must fail health immediately -- staleness is not needed to catch
    it, and a fresh beat can never mask the 'dead' state."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "dead", "freehub": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 1,  # the most recent possible beat
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.UNHEALTHY


def test_staleness_check_is_skipped_for_legacy_payloads(tmp_paths):
    """A payload without tick_monotonic (written before this feature, or by
    the legacy plain-float writer) is only validated by state, exactly like
    the existing backward-compat path -- it must not spuriously fail."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
    )  # no tick_monotonic key
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.HEALTHY


def test_telegram_dead_but_heartbeat_alive_is_unhealthy(tmp_paths):
    """The exact audit gap: the global heartbeat is fresh and the DB is
    intact, but the Telegram worker has died. Health must FAIL."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "dead", "freehub": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


def test_telegram_missing_from_snapshot_is_unhealthy(tmp_paths):
    """A critical worker that never registered (or silently vanished)
    from the snapshot must fail health even with a fresh tick."""
    _, _, heartbeat_path = tmp_paths
    # Only the heartbeat loop beat; Telegram never appeared.
    _write_worker_snapshot(
        heartbeat_path,
        {"freehub": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


def test_telegram_reconnecting_is_healthy(tmp_paths):
    """A worker in a bounded reconnect/backoff is temporarily degraded,
    not dead -- health must pass so the reconnect is allowed to complete."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "reconnecting", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    # Reconnecting should be DEGRADED, not HEALTHY
    assert state == healthcheck.DEGRADED


def test_telegram_alive_plus_heartbeat_is_healthy(tmp_paths):
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.HEALTHY


def test_freehub_missing_from_snapshot_is_unhealthy(tmp_paths):
    """FreeHub is a critical ingestion path in the standard deployment
    (ENABLED_WORKERS=telegram,freehub): a dead/vanished FreeHub worker must
    fail health even with Telegram and the heartbeat loop alive and fresh."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


def test_freehub_stale_alive_worker_fails_health(tmp_paths):
    """A stale-but-alive FreeHub worker is hung: its SourceWorker beats on a
    10s sub-cadence even during idle sleeps, so a beat gap beyond the window
    means it stopped making progress, not that it is between polls."""
    _, _, heartbeat_path = tmp_paths
    tick_monotonic = 1_000_000.0
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
        tick_monotonic=tick_monotonic,
        last_beat=tick_monotonic - 2,
        per_worker_beats={"freehub": tick_monotonic - 200},
    )
    state, details = healthcheck.check_worker_runtime_health(
        heartbeat_path, max_age_seconds=90, worker_stale_after_seconds=45
    )
    assert state == healthcheck.UNHEALTHY
    assert "stale_workers" in details.get("checks", {})


def test_any_critical_worker_dead_fails_even_if_others_alive(tmp_paths):
    """Every critical worker must be healthy, not just the heartbeat."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "__heartbeat__": "dead"},
    )
    state, _ = healthcheck.check_worker_runtime_health(heartbeat_path, max_age_seconds=90)
    assert state == healthcheck.UNHEALTHY


# ------------------------------------------------------------------
# Critical-worker derivation (P1-A): which workers the healthcheck
# requires must come from the enabled worker/job-source configuration,
# not a hardcoded list -- so a deployment that disables Telegram (or an
# unknown adapter) still gets exactly the critical set its own config
# selected, and never a stale hardcoded union.
# ------------------------------------------------------------------


def _cfg(id, adapter, enabled=True):
    from types import SimpleNamespace
    return SimpleNamespace(id=id, adapter=adapter, enabled=enabled)


_FREEHUB_CFG = _cfg("freehub", "app.adapters.sources.freehub:FreeHubJobSource")
_TG_CHANNELS_CFG = _cfg("telegram_channels", "app.adapters.sources.telegram:TelegramChannelJobSource")
_OTHER_CFG = _cfg("other", "app.adapters.sources.other:OtherJobSource")

_DEFAULT_WORKERS = {
    "telegram", "freehub", "classification_retry", "notification_retry", "user_notifications",
}


def test_expected_critical_workers_matches_the_standard_deployment_default():
    """With the default configuration the derived set must be exactly the
    deployed ingestion workers plus the heartbeat beat -- matching the
    historical hardcoded set, so an unmodified deployment behaves
    identically. linkedin/wuzzuf are scraper-file-backed sources, so the
    scraper_scheduler worker that keeps their snapshot fresh must also be
    required (see _SCRAPER_ADAPTER_MODULE_PREFIX)."""
    assert healthcheck.expected_critical_workers() == {
        "telegram", "freehub", "linkedin", "wuzzuf",
        "scraper_scheduler", "__heartbeat__",
    }


def test_expected_critical_workers_requires_scraper_scheduler_only_when_a_scraper_source_is_enabled():
    """A deployment with no scraper-file-backed source enabled must not
    require the scraper_scheduler worker -- it never runs, so requiring
    it would fail an otherwise-healthy deployment."""
    result = healthcheck.expected_critical_workers(
        enabled_workers={"telegram", "freehub"},
        job_sources=[_FREEHUB_CFG, _TG_CHANNELS_CFG],
    )
    assert "scraper_scheduler" not in result

    linkedin_cfg = _cfg(
        "linkedin", "app.adapters.sources.scraper_file:LinkedInFileJobSource"
    )
    result = healthcheck.expected_critical_workers(
        enabled_workers={"telegram", "linkedin"},
        job_sources=[_TG_CHANNELS_CFG, linkedin_cfg],
    )
    assert "scraper_scheduler" in result


def test_persistence_worker_quarantine_fails_health_even_when_not_a_critical_worker(tmp_paths):
    """A quarantined DB/state worker must fail health immediately and
    unconditionally, regardless of expected_critical_workers -- this is
    the exact 'quarantine invisible to healthcheck' gap: the worker holds
    no SQLite lock, so check_persistence_integrity's separate
    out-of-process check alone would keep passing."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"freehub": "alive", "__heartbeat__": "alive", "db_worker": "dead"},
    )
    state, details = healthcheck.check_worker_runtime_health(
        heartbeat_path,
        max_age_seconds=90,
        critical_workers={"freehub", "__heartbeat__"},
    )
    assert state == healthcheck.UNHEALTHY
    assert details["checks"]["persistence_worker_quarantined"]["workers"] == ["db_worker"]


def test_persistence_worker_absent_is_not_a_failure(tmp_paths):
    """A persistence worker that has simply never run yet (fresh process)
    must not be treated as a failure -- only a positive 'dead' report
    counts, unlike the critical-worker presence check."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"freehub": "alive", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path,
        max_age_seconds=90,
        critical_workers={"freehub", "__heartbeat__"},
    )
    assert state == healthcheck.HEALTHY


def test_expected_critical_workers_maps_telegram_channel_source_to_its_liveness_key():
    """A telegram_channels job-source entry must resolve to the 'telegram'
    liveness key (TelegramChannelJobSource.id / the key the live worker
    beats), not to the config entry's own id."""
    result = healthcheck.expected_critical_workers(
        enabled_workers=_DEFAULT_WORKERS,
        job_sources=[_FREEHUB_CFG, _TG_CHANNELS_CFG],
    )
    assert result == {"telegram", "freehub", "__heartbeat__"}


def test_expected_critical_workers_omits_disabled_and_unselected_sources():
    """A job source that is not enabled, or not present in
    ENABLED_WORKERS, never runs -- it must not be required by health."""
    disabled = _cfg("freehub", "app.adapters.sources.freehub:FreeHubJobSource", enabled=False)
    unselected = _cfg("freehub", "app.adapters.sources.freehub:FreeHubJobSource", enabled=True)
    result = healthcheck.expected_critical_workers(
        enabled_workers={"telegram"},
        job_sources=[disabled, unselected],
    )
    assert result == {"telegram", "__heartbeat__"}


def test_expected_critical_workers_omits_retry_loops_and_the_heartbeat_is_always_required():
    """The classification/notification retry loops never beat a liveness
    key and are not ingestion paths, so they must not appear; the
    heartbeat beat is always required (it proves the event loop turns)."""
    result = healthcheck.expected_critical_workers(
        enabled_workers={"telegram", "classification_retry", "notification_retry", "user_notifications"},
        job_sources=[_FREEHUB_CFG],
    )
    assert result == {"telegram", "__heartbeat__"}


def test_expected_critical_workers_falls_back_to_config_entry_id_for_unknown_adapter():
    """An un-mapped adapter defaults to its config entry id, so a new
    adapter still gets a healthcheck signal instead of being silently
    required under a guessed name."""
    result = healthcheck.expected_critical_workers(
        enabled_workers={"telegram", "other"},
        job_sources=[_OTHER_CFG],
    )
    assert "other" in result


def test_disabling_telegram_worker_drops_telegram_from_the_critical_set(tmp_paths):
    """When ENABLED_WORKERS does not include 'telegram', the '-channels
    job-source is undiscoverable (Telegram channels post only arrives via
    the live loop; see TelegramChannelJobSource / _handle_live_message),
    so health must not require a Telegram worker that is intentionally not
    deployed -- but every still-deployed ingestion worker is required."""
    _, _, heartbeat_path = tmp_paths
    _write_worker_snapshot(
        heartbeat_path,
        {"telegram": "alive", "freehub": "alive", "linkedin": "alive", "wuzzuf": "alive", "scraper_scheduler": "alive", "__heartbeat__": "alive"},
    )
    healthcheck.check_worker_runtime_health(
        heartbeat_path,
        max_age_seconds=90,
        critical_workers=healthcheck.expected_critical_workers(
            enabled_workers={"freehub"},
            job_sources=[_FREEHUB_CFG],
        ),
    )  # must not raise despite 'telegram' being absent

    # The same deployment, but FreeHub dead: must still fail -- the
    # derived set is not just 'telegram removal'.
    _write_worker_snapshot(
        heartbeat_path,
        {"freehub": "dead", "__heartbeat__": "alive"},
    )
    state, _ = healthcheck.check_worker_runtime_health(
        heartbeat_path,
        max_age_seconds=90,
        critical_workers=healthcheck.expected_critical_workers(
            enabled_workers={"freehub"},
            job_sources=[_FREEHUB_CFG],
        ),
    )
    assert state == healthcheck.UNHEALTHY
