"""Unit tests for the Postgres adapter's legacy-value normalization.

Regression coverage for the SQLite -> Postgres swap (see
migrations/postgres/0001_init.sql): Postgres enforces INTEGER /
TIMESTAMPTZ / FK types that the loosely-typed SQLite backend accepted
silently, so the adapter normalizes the legacy value shapes at its
boundary (app/adapters/repositories/postgres.py). No database server is
required -- these exercise the pure helpers only.
"""
from datetime import datetime, timezone

from app.adapters.repositories.postgres import (
    _as_int_or_none,
    _load_migration_runner,
    _normalize_timestamp,
    _normalize_update_value,
)


def test_as_int_or_none_coerces_legacy_float():
    assert _as_int_or_none(123.45) == 123
    assert _as_int_or_none(0.0) == 0


def test_as_int_or_none_passes_int_and_bool():
    assert _as_int_or_none(7) == 7
    assert _as_int_or_none(True) == 1
    assert _as_int_or_none(False) == 0


def test_as_int_or_none_string_inputs():
    assert _as_int_or_none("42") == 42
    assert _as_int_or_none("42.9") == 42
    assert _as_int_or_none("") is None
    assert _as_int_or_none("not-a-number") is None


def test_as_int_or_none_none():
    assert _as_int_or_none(None) is None


def test_normalize_timestamp_passthrough_datetime():
    value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _normalize_timestamp(value) is value


def test_normalize_timestamp_epoch_string():
    result = _normalize_timestamp("1754322000.0")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result.timestamp() == 1754322000.0


def test_normalize_timestamp_numeric():
    import time as _t

    epoch = _t.time()
    result = _normalize_timestamp(epoch)
    assert isinstance(result, datetime)
    assert abs(result.timestamp() - epoch) < 1e-6


def test_normalize_timestamp_iso_string():
    result = _normalize_timestamp("2026-01-01T12:00:00+00:00")
    assert isinstance(result, datetime)


def test_normalize_timestamp_blank_or_garbage():
    assert _normalize_timestamp("") is None
    assert _normalize_timestamp("garbage") is None
    assert _normalize_timestamp(None) is None


def test_empty_category_id_normalizes_to_null():
    assert _normalize_update_value("category_id", "") is None
    assert _normalize_update_value("category_id", "backend") == "backend"


def test_update_value_int_coercion():
    assert _normalize_update_value("filter_time_ms", 123.45) == 123
    assert _normalize_update_value("response_time_ms", 12.9) == 12


def test_update_value_epoch_string_to_timestamptz():
    result = _normalize_update_value(
        "classification_retry_not_before", "1754322000.0"
    )
    assert isinstance(result, datetime)
    assert result.tzinfo is not None


def test_update_value_passthrough_for_untyped_fields():
    assert _normalize_update_value("final_decision", "") == ""
    assert _normalize_update_value("notification_status", "Pending") == "Pending"


def test_migration_runner_loads_the_real_script():
    module = _load_migration_runner()
    assert callable(module.run)
    assert module.MIGRATIONS_DIR.name == "postgres"
    assert (module.MIGRATIONS_DIR / "0001_init.sql").exists()