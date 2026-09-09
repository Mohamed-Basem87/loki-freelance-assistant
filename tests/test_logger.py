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
        f'CREATE TABLE "Jobs" ({_column_defs(_legacy_column_names([h for h in JOB_HEADERS if h != "Identity Source"]))});'
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
        "1", "0", "", "Sent", "Accepted", "", "", "", "12.5",
        "",
    ]
    assert len(values) == len(JOB_HEADERS) - 1

    conn.execute(
        f'INSERT INTO "Jobs" ({_quoted(_legacy_column_names([h for h in JOB_HEADERS if h != "Identity Source"]))}) '
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
        "2026-08-02T09:00:00", job_uuid, "freelancer:2002", "freelancer", "Freelancer",
        "SQL Dashboard", "Build it", "raw", "SQL Dashboard\nBuild it",
        "", "", "accept", "core_positive_clean", "data_analysis", "",
        "1", "0", "1", "4", "0", "1", "0",
        "Power BI(3/data_analysis)", "Excel(2/data_analysis)", "", "",
        "0", "", "1", "0", "", "Sent", "Accepted", "", "", "", "9.0",
        "",
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


def test_log_gemini_persists_provider(tmp_path):
    """log_gemini must store the winning provider in the gemini audit
    table (Provider column) so an arbitration log entry identifies its
    source provider without reading it from a reason string."""
    db = tmp_path / "gemini.db"
    job_uuid = str(uuid.uuid4())

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        logger.log_gemini(
            job_uuid=job_uuid,
            decision_before="needs_gemini",
            reason_before="ambiguous",
            prompt_tokens="",
            completion_tokens="",
            response_time_ms=42,
            decision="Accepted",
            confidence=90,
            provider="gemini",
            save=False,
        )
        logger.save()

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT Provider FROM gemini WHERE \"Job UUID\" = ?",
                (job_uuid,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "expected a gemini audit row"
        assert row[0] == "gemini"
    finally:
        logger.close()
        logger.path = original_path
    """
    P0-1 regression test, mirroring the audit's own reproduction: a
    kill between DROP "Jobs" and ALTER TABLE ... RENAME under the old
    autocommit-per-statement code left `_migrating_jobs` present
    (fully built under the current schema) and no `jobs`/`Jobs` table
    at all. The old code's next initialize() found neither name and
    silently created a brand-new, empty `jobs` table -- destroying
    the entire audit history with zero errors.

    _rebuild_table now wraps DROP+RENAME in one transaction, so this
    exact interrupted state can no longer be produced going forward --
    but a database already left in it (by a pre-fix deployment) must
    still be recovered, not silently wiped. That's what
    _recover_orphaned_migrations does.
    """
    db = tmp_path / "orphaned.db"
    job_uuid = str(uuid.uuid5(_NS, "freelancer:3003"))

    conn = sqlite3.connect(db)
    # Simulate the fully-built temp table a crash left behind between
    # the DROP and the RENAME -- no "jobs"/"Jobs" table exists at all.
    conn.execute(
        f'CREATE TABLE "_migrating_jobs" ({_column_defs(JOB_HEADERS, primary_key=1)});'
    )
    values = [
        "2026-08-03T09:00:00", job_uuid, "freelancer:3003", "freelancer", "Freelancer",
        "Orphaned Row", "Should survive", "raw", "Orphaned Row\nShould survive",
        "", "", "accept", "core_positive_clean", "data_analysis", "",
        "1", "0", "1", "4", "0", "1", "0",
        "Power BI(3/data_analysis)", "Excel(2/data_analysis)", "", "",
        "0", "", "1", "0", "", "Sent", "Accepted", "", "", "", "9.0",
        "",
    ]
    conn.execute(
        f'INSERT INTO "_migrating_jobs" ({_quoted(JOB_HEADERS)}) '
        f"VALUES ({_placeholders(len(values))})",
        values,
    )
    conn.commit()
    conn.close()

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()  # must not silently start with an empty jobs table

        names = _table_names(sqlite3.connect(db))
        assert "jobs" in {n.lower() for n in names}
        assert not any(n.startswith("_migrating_") for n in names)

        assert logger.count_jobs() == 1
        job = logger.get_job(job_uuid)
        assert job is not None
        assert job["Title"] == "Orphaned Row"
    finally:
        logger.close()
        logger.path = original_path


def test_repeated_initialize_never_duplicates_merged_legacy_rows(tmp_path):
    """
    P0-2 regression test: gemini/notifications/errors/notification_guard
    have no unique constraint, so INSERT OR IGNORE alone gives zero
    duplicate protection -- the only thing that ever prevented a
    crash-then-retry from re-inserting every legacy row on every
    subsequent restart is the INSERT and the legacy-table DROP
    committing atomically together. Simulate two full restarts against
    the same legacy database and confirm the row appears exactly once,
    not once per restart.
    """
    db = tmp_path / "legacy_repeat.db"
    job_uuid = _create_legacy_db(str(db))

    original_path = logger.path
    logger.close()
    try:
        logger.path = db

        logger.initialize()
        logger.close()

        logger.path = db
        logger.initialize()  # second "restart" against the same file

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                'SELECT "Job UUID" FROM notification_guard'
            ).fetchall()
            assert rows == [(job_uuid,)]

            rows = conn.execute('SELECT "Job UUID" FROM errors').fetchall()
            assert rows == [(job_uuid,)]
        finally:
            conn.close()

        assert logger.count_jobs() == 1
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


def _create_job_row(job_uuid, *, title="Test Job", desc="A test job"):
    created = logger.create_job_if_absent(
        legacy_job_uuid=None,
        job_uuid=job_uuid,
        job_id="9876",
        source="freelancer",
        identity_source="freelancer:9876",
        title=title,
        description=desc,
        raw_message=desc,
        filter_text=f"{title}\n{desc}",
        company="",
        url="",
        filter_result={},
        filter_time_ms=0,
        save=False,
    )
    logger.save()
    assert created is True
    return logger._conn


def test_update_job_rejects_unknown_field_name(tmp_path):
    """update_job must fail loudly (ValueError) on an unknown field name
    instead of silently ignoring it and dropping the intended write --
    the "fail loudly" remediation for a typo silently corrupting state."""
    import pytest
    from app.logger import COLUMN_MAP

    db = tmp_path / "update_loud.db"
    job_uuid = str(uuid.uuid4())

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        _create_job_row(job_uuid)

        with pytest.raises(ValueError, match="unknown field"):
            logger.update_job(job_uuid, not_a_real_field="x")

        # A valid field still works.
        assert logger.update_job(job_uuid, title="Renamed") is True
        row = logger.get_job(job_uuid)
        assert row["Title"] == "Renamed"
    finally:
        logger.close()
        logger.path = original_path


def test_update_job_rejects_typo_field_name(tmp_path):
    """A near-miss typo (e.g. final_decisionn instead of final_decision)
    must also raise -- this is the exact class of bug the loud failure is
    meant to catch."""
    import pytest

    db = tmp_path / "update_typo.db"
    job_uuid = str(uuid.uuid4())

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        _create_job_row(job_uuid)

        with pytest.raises(ValueError):
            logger.update_job(job_uuid, final_decisionn="Accepted")
    finally:
        logger.close()
        logger.path = original_path


def test_user_uniqueness_migration_dedups_and_adds_index(tmp_path):
    """Legacy duplicate users rows (same Telegram User ID) must be
    deduped by the migration and a UNIQUE index created so the invariant
    is enforced going forward -- without losing their merged preferences
    or re-pointing of queued deliveries."""
    db = tmp_path / "users_migrate.db"

    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE users ('
        '"User ID" TEXT, "Telegram User ID" TEXT, "Username" TEXT, '
        '"First Name" TEXT, "Destination Type" TEXT, "Categories" TEXT, '
        '"Sources" TEXT, "Is Active" TEXT, "Created At" TEXT, "Updated At" TEXT)'
    )
    conn.execute(
        'CREATE TABLE user_notifications ('
        '"Notification ID" TEXT, "Job UUID" TEXT, "User ID" TEXT, '
        '"Telegram User ID" TEXT, "Category ID" TEXT, "Status" TEXT, '
        '"Claimed At" TEXT, "Attempts" TEXT, "Last Error" TEXT, '
        '"Created At" TEXT, "Updated At" TEXT, "Next Attempt At" TEXT)'
    )
    # survivor (newest rowid) and an older duplicate referencing a user id
    # that also has a queued notification row.
    conn.execute(
        "INSERT INTO users VALUES ('u-old', '90001', 'u', 'Old', 'user', "
        "'', '', '1', 't1', 't1')"
    )
    conn.execute(
        "INSERT INTO users VALUES ('u-new', '90001', 'u', 'New', 'user', "
        "'data_analysis', 'mostaql', '1', 't2', 't2')"
    )
    conn.execute(
        "INSERT INTO user_notifications VALUES "
        "('n1', 'job1', 'u-old', '90001', 'data_analysis', 'Pending', "
        "'', '0', '', 't1', 't1', '')"
    )
    conn.commit()
    conn.close()

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        conn = sqlite3.connect(db)
        try:
            rows = conn.execute('SELECT * FROM users').fetchall()
        finally:
            conn.close()

        # Exactly one surviving user, with merged preferences from the
        # older duplicate (the NEWEST rowid survives).
        assert len(rows) == 1
        assert rows[0][0] == "u-new"

        # The queued notification referencing the removed duplicate's user
        # id was re-pointed at the survivor.
        conn = sqlite3.connect(db)
        try:
            n = conn.execute(
                'SELECT "User ID" FROM user_notifications WHERE "Notification ID" = ?',
                ("n1",),
            ).fetchone()
            assert n[0] == "u-new"

            # The unique index now exists and enforces the invariant.
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            assert "idx_users_telegram_user_id" in indexes

            # Enforcement: a second INSERT with the same telegram id fails.
            import pytest as _pytest
            with _pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO users VALUES ('u-dup', '90001', 'x', 'X', "
                    "'user', '', '', '1', 't3', 't3')"
                )
        finally:
            conn.close()
    finally:
        logger.close()
        logger.path = original_path


# ------------------------------------------------------------------
# P2-A: shutdown vs a quarantined executor.
#
# run() cannot kill a stuck worker thread, so it quarantines the DB
# backend (_executor_poisoned=True) and waits for a process restart.
# shutdown() must therefore NOT close the connection while that thread
# may still be mid-statement on it -- closing it would tear the
# connection out from under a running worker for no benefit. These
# tests plug a private executor in (monkeypatched) so calling
# shutdown() cannot permanently disable the module's real shared
# executor for the rest of the session.
# ------------------------------------------------------------------


def test_shutdown_leaves_connection_open_when_executor_is_poisoned(tmp_path, monkeypatch):
    import app.logger as logger_module
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        logger_module,
        "_EXECUTOR",
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-logger-test"),
    )
    db = tmp_path / "poisoned.db"

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()
        # Simulate the state run() leaves behind after an operation
        # timed out on the worker thread (see DBLogger.run).
        monkeypatch.setattr(logger, "_executor_poisoned", True)

        logger.shutdown()

        assert logger._conn is not None, (
            "a quarantined executor may still have a worker thread mid-"
            "statement on the connection; shutdown must leak the connection "
            "to the OS rather than close it under that thread (P2-A)"
        )
    finally:
        logger.close()
        logger.path = original_path
        monkeypatch.undo()


def test_shutdown_closes_connection_on_the_healthy_path(tmp_path, monkeypatch):
    import app.logger as logger_module
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        logger_module,
        "_EXECUTOR",
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-logger-test"),
    )
    db = tmp_path / "healthy.db"

    original_path = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        logger.shutdown()

        assert logger._conn is None, (
            "a healthy shutdown must close the connection so the process "
            "exits with no open file descriptor to the audit database"
        )
        # Idempotent: a second shutdown is a no-op, and the executor may
        # still be owned by a caller who calls shutdown twice.
        logger.shutdown()
    finally:
        logger.close()
        logger.path = original_path
        monkeypatch.undo()
