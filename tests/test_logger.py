"""
DBLogger schema-migration tests.

The audit-log database was originally produced by an Excel->SQLite
export that used capitalized table names and spreadsheet-flavored
underscore columns ("Job_UUID", "Filter_Time_ms"). Because SQLite
matches table names case-insensitively, the current lowercase
CREATE TABLE IF NOT EXISTS silently matches those legacy tables and
keeps their mismatched columns -- so every INSERT with the new spaced
column names fails with "table X has no column named Y" (the exact
crash this migration fixes). initialize() must detect such tables and
rebuild them in place under the current schema, preserving every
recognizable row.
"""

import sqlite3
import uuid

from app.logger import (
    JOB_HEADERS,
    GEMINI_HEADERS,
    NOTIFICATION_HEADERS,
    ERROR_HEADERS,
    NOTIFICATION_GUARD_HEADERS,
    _column_defs,
    _legacy_column_names,
    logger,
)

_NS = uuid.UUID("6f6e6465-7370-4a6f-6273-7570706f7274")


def _quoted(names):
    return ", ".join(f'"{name}"' for name in names)


def _placeholders(count):
    return ", ".join("?" for _ in range(count))


def _create_legacy_db(path):
    """Create a database shaped like the historical Excel->SQLite export
    with one Jobs row, one Errors row and one NotificationGuard row."""
    conn = sqlite3.connect(path)

    conn.execute(
        f'CREATE TABLE "Jobs" ({_column_defs(_legacy_column_names(JOB_HEADERS))});'
    )
    conn.execute(
        f'CREATE TABLE "Gemini" ({_column_defs(_legacy_column_names(GEMINI_HEADERS))});'
    )
    conn.execute(
        f'CREATE TABLE "Notifications" ({_column_defs(_legacy_column_names(NOTIFICATION_HEADERS))});'
    )
    conn.execute(
        f'CREATE TABLE "Errors" ({_column_defs(_legacy_column_names(ERROR_HEADERS))});'
    )
    conn.execute(
        'CREATE TABLE "NotificationGuard" '
        f'({_column_defs(_legacy_column_names(NOTIFICATION_GUARD_HEADERS))});'
    )

    job_uuid = str(uuid.uuid5(_NS, "freelancer:1001"))

    values = [
        "2026-08-01T10:00:00",
        job_uuid,
        "freelancer:1001",
        "Freelancer",
        "Dashboard in Excel",
        "Build a dashboard",
        "raw text",
        "Dashboard in Excel\nBuild a dashboard",
        "Acme",
        "https://example.com/jobs/1001",
        "accept",
        "core_positive_clean",
        "data_analysis",
        "",
        "1", "0", "1", "4", "0",
        "1", "0",
        "Power BI(3/data_analysis)", "Excel(2/data_analysis)", "", "",
        "0", "",
        "1", "0", "", "Sent", "Accepted", "12.5",
    ]
    assert len(values) == len(JOB_HEADERS)

    conn.execute(
        f'INSERT INTO "Jobs" ({_quoted(_legacy_column_names(JOB_HEADERS))}) '
        f"VALUES ({_placeholders(len(values))})",
        values,
    )
    conn.execute(
        'INSERT INTO "Errors" ("Timestamp", "Job_UUID", "Module", "Error") '
        "VALUES (?, ?, ?, ?)",
        ("2026-08-01T10:01:00", job_uuid, "FreeHub Worker", "boom"),
    )
    conn.execute(
        'INSERT INTO "NotificationGuard" ('
        '"Timestamp", "Job_UUID", "Source", "Title", "Original_Decision", '
        '"Guard_Decision", "Provider", "Model", "Response_Time_ms", "Error") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-01T10:02:00", job_uuid, "Freelancer", "Dashboard in Excel",
            "accept", "notify", "groq", "llama-3.3-70b-versatile", "123.4", "",
        ),
    )
    conn.commit()
    conn.close()
    return job_uuid


def _table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _table_columns(conn, name):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]


def test_migrates_legacy_jobs_table_in_place(tmp_path):
    db = tmp_path / "legacy.db"
    job_uuid = _create_legacy_db(str(db))

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        conn = sqlite3.connect(db)
        try:
            names = _table_names(conn)
            assert "jobs" in names
            assert "Jobs" not in names
            assert _table_columns(conn, "jobs") == JOB_HEADERS
            assert "Job UUID" in _table_columns(conn, "jobs")
            assert not any(name.startswith("_migrating_") for name in names)
        finally:
            conn.close()

        assert logger.count_jobs() == 1
        job = logger.get_last_job()
        assert job["Job UUID"] == job_uuid
        assert job["Title"] == "Dashboard in Excel"
        assert job["Final Decision"] == "Accepted"
        assert logger.has_job(job_uuid)
    finally:
        logger.close()
        logger.path = original_path


def test_migrates_legacy_errors_table(tmp_path):
    db = tmp_path / "legacy_errors.db"
    job_uuid = _create_legacy_db(str(db))

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        conn = sqlite3.connect(db)
        try:
            assert _table_columns(conn, "errors") == ERROR_HEADERS
            assert _table_columns(conn, "gemini") == GEMINI_HEADERS
            assert _table_columns(conn, "notifications") == NOTIFICATION_HEADERS
            rows = conn.execute(
                'SELECT "Job UUID", "Module", "Error" FROM errors'
            ).fetchall()
            assert rows == [(job_uuid, "FreeHub Worker", "boom")]
        finally:
            conn.close()
    finally:
        logger.close()
        logger.path = original_path


def test_merges_legacy_notification_guard(tmp_path):
    db = tmp_path / "legacy_guard.db"
    job_uuid = _create_legacy_db(str(db))

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        conn = sqlite3.connect(db)
        try:
            assert _table_columns(conn, "notification_guard") == NOTIFICATION_GUARD_HEADERS
            assert "NotificationGuard" not in _table_names(conn)
            rows = conn.execute(
                'SELECT "Job UUID", "Guard Decision", "Model" FROM notification_guard'
            ).fetchall()
            assert rows == [(job_uuid, "notify", "llama-3.3-70b-versatile")]
        finally:
            conn.close()
    finally:
        logger.close()
        logger.path = original_path


def test_initialize_is_idempotent_on_current_schema(tmp_path):
    """A database already on the current schema must be left untouched --
    regression guard for the production DB after its one-off migration."""
    db = tmp_path / "current.db"
    job_uuid = str(uuid.uuid5(_NS, "freelancer:2002"))

    conn = sqlite3.connect(db)
    conn.execute(
        f"CREATE TABLE jobs ({_column_defs(JOB_HEADERS, primary_key=1)});"
    )
    values = [
        "2026-08-02T09:00:00", job_uuid, "freelancer:2002", "Freelancer",
        "SQL Dashboard", "Build it", "raw", "SQL Dashboard\nBuild it",
        "", "", "accept", "core_positive_clean", "data_analysis", "",
        "1", "0", "1", "4", "0", "1", "0",
        "Power BI(3/data_analysis)", "Excel(2/data_analysis)", "", "",
        "0", "", "1", "0", "", "Sent", "Accepted", "9.0",
    ]
    conn.execute(
        f'INSERT INTO jobs ({_quoted(JOB_HEADERS)}) VALUES ({_placeholders(len(values))})',
        values,
    )
    conn.commit()
    conn.close()

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        assert logger.count_jobs() == 1
        job = logger.get_job(job_uuid)
        assert job["Title"] == "SQL Dashboard"

        conn = sqlite3.connect(db)
        try:
            assert _table_columns(conn, "jobs") == JOB_HEADERS
            assert not any(
                name.startswith("_migrating_") for name in _table_names(conn)
            )
        finally:
            conn.close()
    finally:
        logger.close()
        logger.path = original_path


def test_initialize_creates_current_schema_on_fresh_db(tmp_path):
    db = tmp_path / "fresh.db"

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        conn = sqlite3.connect(db)
        try:
            expected = {
                "jobs": JOB_HEADERS,
                "gemini": GEMINI_HEADERS,
                "notifications": NOTIFICATION_HEADERS,
                "errors": ERROR_HEADERS,
                "notification_guard": NOTIFICATION_GUARD_HEADERS,
            }
            names = _table_names(conn)
            for table, headers in expected.items():
                assert table in names
                assert _table_columns(conn, table) == headers
        finally:
            conn.close()
    finally:
        logger.close()
        logger.path = original_path
