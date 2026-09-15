"""Regression tests for the Postgres timeout/quarantine restoration (issue 7).

The Postgres adapter must bound every repository call with REAL bounds
(connect timeout, server-side statement_timeout, pool_timeout, plus an
asyncio.wait_for around the executor dispatch) and QUARANTINE on
timeout -- mirroring the original SQLite backend's StuckExecutorError
behavior -- so a hung DB call fails loudly and marks the "db_worker"
liveness dead instead of stalling source workers forever while the
heartbeat keeps ticking.
"""
import asyncio
import time

import pytest

import app.adapters.repositories.postgres as pg
from app.adapters.repositories.postgres import StuckExecutorError
from app.infra.heartbeat import liveness


class _FakeEngine:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def dispose(self):
        self.calls.append("dispose")


# ------------------------------------------------------------------
# Engine configuration carries real, server-side timeout bounds.
# ------------------------------------------------------------------


def test_engine_created_with_real_timeout_bounds(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        raise RuntimeError("abort construct")

    monkeypatch.setattr(pg, "create_engine", fake_create_engine)

    with pytest.raises(RuntimeError):
        pg.PostgresRepository(
            "postgresql://u:p@h:5432/db",
            database_timeout_seconds=60,
        )

    kwargs = captured["kwargs"]
    assert kwargs["pool_pre_ping"] is True

    # Pool-level: acquiring a connection is bounded, and stale pooled
    # connections are recycled rather than held forever.
    assert kwargs["pool_timeout"] == 60
    assert kwargs["pool_recycle"] == pg._POOL_RECYCLE_SECONDS

    # Connection-level: the libpq handshake/startup is bounded, and
    # Postgres itself aborts any statement that overruns the
    # application-level bound (server-side, so the worker thread really
    # terminates -- a caller-side timeout alone would not).
    connect_args = kwargs["connect_args"]
    assert connect_args["connect_timeout"] == pg._CONNECT_TIMEOUT_SECONDS
    assert connect_args["options"] == "-c statement_timeout=60000"


def test_statement_timeout_scales_with_configured_database_timeout():
    assert pg._statement_timeout_option(0.2) == "-c statement_timeout=200"
    assert pg._statement_timeout_option(1) == "-c statement_timeout=1000"
    assert pg._statement_timeout_option(0) == "-c statement_timeout=1"


# ------------------------------------------------------------------
# run() timeouts -> quarantine -> fail closed, with db_worker dead.
# ------------------------------------------------------------------


def test_run_times_out_and_quarantines_the_repository():
    repo = pg.PostgresRepository(
        "postgresql://u:p@h:5432/db",
        database_timeout_seconds=0.5,
    )

    def slow():
        time.sleep(30)
        return "never"

    with pytest.raises(StuckExecutorError):
        asyncio.run(repo.run(slow))

    assert repo._quarantined is True, "a timed-out call must quarantine the repository"
    state = liveness.state("db_worker")
    assert state is not None and state["state"] == "dead", (
        "the healthcheck's persistence 'db_worker' liveness must report dead"
    )

    # Every later call fails closed until (process) restart.
    with pytest.raises(StuckExecutorError):
        asyncio.run(repo.run(lambda: 1))


def test_run_success_beats_db_worker_alive_and_returns():
    repo = pg.PostgresRepository(
        "postgresql://u:p@h:5432/db",
        database_timeout_seconds=5,
    )

    result = asyncio.run(repo.run(lambda: 42))

    assert result == 42
    state = liveness.state("db_worker")
    assert state is not None and state["state"] == "alive"
    assert repo._quarantined is False


# ------------------------------------------------------------------
# dispose() must not touch the pool under a still-stuck worker.
# ------------------------------------------------------------------


def test_dispose_skips_engine_when_quarantined():
    repo = pg.PostgresRepository("postgresql://u:p@h:5432/db")
    engine_calls = []
    repo._engine = _FakeEngine(engine_calls)

    repo._quarantined = True
    repo.dispose()
    assert engine_calls == [], (
        "disposing the pool while a stuck worker may hold a connection "
        "is unsafe -- shutdown must no-op when quarantined"
    )

    repo._quarantined = False
    repo.dispose()
    assert engine_calls == ["dispose"]