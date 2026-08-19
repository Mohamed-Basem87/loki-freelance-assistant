import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3


# The audit log lives in a SQLite database file next to docker-compose.yml
# (bind-mounted read/write, never baked into the image), so it is directly
# visible/inspectable on the host and survives container rebuilds.
DB_FILE = Path(__file__).resolve().parent.parent / "loki_freelance_bot.db"

# All DBLogger reads/writes must go through this single worker thread
# (see DBLogger.run below). Funneling every access through one dedicated
# thread keeps them strictly serial (no two logger calls ever touch the
# database at the same time) without ever blocking the event loop.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-logger")


# ------------------------------------------------------------------
# Jobs table -- one row per job, reflecting the tiered decision engine.
#
# Compared to the old scoring model, "Score" is gone (there is no
# single number driving the decision anymore) and is replaced with the
# actual evidence trail: which core/supporting keywords fired on each
# side, and the plain-English `reason` the decision table returned.
# This is what makes the log self-explanatory without re-deriving the
# math by hand.
# ------------------------------------------------------------------

JOB_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Job ID",
    "Source",
    "Title",
    "Description",
    "Raw Message",
    "Filter Text",
    "Company",
    "URL",

    "Decision",
    "Decision Reason",

    "Categories",
    "Negative Categories",

    "Has Core Positive",
    "Has Core Negative",
    "Core Positive Hit Count",
    "Supporting Positive Weight",
    "Supporting Negative Weight",

    "Title Core Positive",
    "Title Core Negative",

    "Core Positive Matches",
    "Supporting Positive Matches",
    "Core Negative Matches",
    "Supporting Negative Matches",

    "Hard Reject",
    "Hard Reject Matches",

    "Notify Directly",
    "Needs Gemini",
    "Gemini Decision",
    "Notification Status",
    "Final Decision",
    "Filter Time (ms)",
]

# snake_case keyword -> SQL column name (the human-readable header).
COLUMN_MAP = {
    "timestamp": "Timestamp",
    "job_uuid": "Job UUID",
    "job_id": "Job ID",
    "source": "Source",

    "title": "Title",
    "description": "Description",
    "raw_message": "Raw Message",
    "filter_text": "Filter Text",

    "company": "Company",
    "url": "URL",

    "decision": "Decision",
    "decision_reason": "Decision Reason",

    "categories": "Categories",
    "negative_categories": "Negative Categories",

    "has_core_positive": "Has Core Positive",
    "has_core_negative": "Has Core Negative",
    "core_positive_hit_count": "Core Positive Hit Count",
    "supporting_positive_weight": "Supporting Positive Weight",
    "supporting_negative_weight": "Supporting Negative Weight",

    "title_core_positive": "Title Core Positive",
    "title_core_negative": "Title Core Negative",

    "core_positive_matches": "Core Positive Matches",
    "supporting_positive_matches": "Supporting Positive Matches",
    "core_negative_matches": "Core Negative Matches",
    "supporting_negative_matches": "Supporting Negative Matches",

    "hard_reject": "Hard Reject",
    "hard_reject_matches": "Hard Reject Matches",

    "notify_directly": "Notify Directly",
    "needs_gemini": "Needs Gemini",
    "gemini_decision": "Gemini Decision",
    "notification_status": "Notification Status",
    "final_decision": "Final Decision",
    "filter_time_ms": "Filter Time (ms)",
}

GEMINI_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Decision Before",
    "Reason Before",
    "Prompt Tokens",
    "Completion Tokens",
    "Response Time (ms)",
    "Decision",
    "Confidence",
]

NOTIFICATION_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Platform",
    "Status",
]

ERROR_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Module",
    "Error",
]

CATEGORY_DECISION_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Category ID",
    "Category Name",
    "Decision",
    "Reason",
    "Keyword Decision",
    "AI Used",
    "LLM Decision",
    "LLM Confidence",
    "Evidence JSON",
]


NOTIFICATION_GUARD_HEADERS = [
    "Timestamp",
    "Job UUID",
    "Source",
    "Title",
    "Original Decision",
    "Guard Decision",
    "Provider",
    "Model",
    "Response Time (ms)",
    "Error",
]


def _column_defs(headers, primary_key=None):
    defs = [f'"{header}" TEXT' for header in headers]
    if primary_key is not None:
        defs[primary_key] = defs[primary_key].replace("TEXT", "TEXT PRIMARY KEY")
    return ", ".join(defs)


_CREATE_TABLES = (
    f'CREATE TABLE IF NOT EXISTS jobs ({_column_defs(JOB_HEADERS, primary_key=1)});',
    f'CREATE TABLE IF NOT EXISTS gemini ({_column_defs(GEMINI_HEADERS)});',
    f'CREATE TABLE IF NOT EXISTS notifications ({_column_defs(NOTIFICATION_HEADERS)});',
    f'CREATE TABLE IF NOT EXISTS errors ({_column_defs(ERROR_HEADERS)});',
    f'CREATE TABLE IF NOT EXISTS notification_guard ({_column_defs(NOTIFICATION_GUARD_HEADERS)});',
    f'CREATE TABLE IF NOT EXISTS category_decisions ({_column_defs(CATEGORY_DECISION_HEADERS)});',
)


# ------------------------------------------------------------------
# Legacy schema migration.
#
# The first SQLite export of the audit log (converted from the old
# Excel workbook) created its tables under capitalized names with
# spreadsheet-derived column names: "Jobs", "Gemini", "Notifications",
# "Errors", "NotificationGuard", with columns like "Job_UUID" /
# "Filter_Time_ms". The current schema uses lowercase table names and
# human-readable spaced column names ("Job UUID", "Filter Time (ms)").
#
# SQLite matches table names case-insensitively, so the new
# CREATE TABLE IF NOT EXISTS for the lowercase name silently matches
# the legacy capitalized table and keeps its mismatched columns -- and
# every INSERT using the new column names then fails with "table X has
# no column named Y". _migrate_schema() detects and fixes that before
# the CREATE TABLE loop: any table shadowing a current name is rebuilt
# under the current schema with every recognizable row carried over, so
# the audit history survives and the app self-heals an old database.
# ------------------------------------------------------------------
_LEGACY_TABLE_NAMES = {
    "jobs": "Jobs",
    "gemini": "Gemini",
    "notifications": "Notifications",
    "errors": "Errors",
    "notification_guard": "NotificationGuard",
}

# (table name, expected headers, primary-key column index or None)
_MIGRATABLE_TABLES = (
    ("jobs", JOB_HEADERS, 1),
    ("gemini", GEMINI_HEADERS, None),
    ("notifications", NOTIFICATION_HEADERS, None),
    ("errors", ERROR_HEADERS, None),
    ("notification_guard", NOTIFICATION_GUARD_HEADERS, None),
)


def _legacy_column_names(headers):
    """Map each current header to the column name the legacy tables
    used for the same field. Legacy names are a spreadsheet-flavored
    rendering of the same headers: spaces became underscores and
    "(ms)" became "ms" ("Job UUID" -> "Job_UUID",
    "Filter Time (ms)" -> "Filter_Time_ms")."""
    return [
        header.replace(" ", "_").replace("(ms)", "ms")
        for header in headers
    ]


def _join_matches(matches):
    """Render a list of {"keyword", "weight", "category"} dicts as a
    compact, human-readable string for a cell."""
    if not matches:
        return ""
    return ", ".join(
        f"{m['keyword']}({m['weight']}/{m['category']})" for m in matches
    )


class DBLogger:

    def __init__(self):
        self.path = DB_FILE
        self._conn = None

    def initialize(self):
        """Create the database file and schema if missing, then keep a
        single connection for the life of the process. All access is
        serialized through the dedicated logger worker thread (see
        run()), so a shared connection is safe."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists() and not self.path.is_file():
            raise RuntimeError(
                f"Audit log path is not a file: {self.path}. "
                "When Docker bind-mounts a database file whose host "
                "path does not exist, it creates a directory instead. "
                "Create the file on the host first, e.g. "
                "`touch loki_freelance_bot.db`, then restart the container."
            )

        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        # Autocommit: every statement is its own transaction. Explicit
        # BEGIN IMMEDIATE/COMMIT is still used where atomicity across
        # multiple statements matters (create_job_if_absent).
        #
        # The default rollback journal (NOT WAL) is used deliberately:
        # the database file is bind-mounted into the container as a
        # single file, so WAL's -wal/-shm sidecar files would live in
        # the container's writable layer and be discarded on container
        # recreate -- losing any commits not yet checkpointed. The
        # rollback journal writes every committed transaction straight
        # into the bind-mounted file itself.
        self._conn.isolation_level = None

        self._migrate_schema()

        for ddl in _CREATE_TABLES:
            self._conn.execute(ddl)

    def _table_names(self):
        return {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    def _table_columns(self, name):
        return [
            row[1] for row in self._conn.execute(f'PRAGMA table_info("{name}")')
        ]

    def _copy_columns(self, headers, source_columns):
        """Which current headers can be carried over from a legacy table,
        as a list of (target column, source column) pairs. A column that
        already has the current spelling maps to itself."""
        legacy_columns = _legacy_column_names(headers)
        mapping = []
        for index, header in enumerate(headers):
            if header in source_columns:
                mapping.append((header, header))
            elif legacy_columns[index] in source_columns:
                mapping.append((header, legacy_columns[index]))
        return mapping

    def _rebuild_table(self, source, target, headers, pk_index):
        """Replace `source` (a table with a legacy/partial schema) with a
        table named `target` under the current schema, copying every row
        whose columns are recognizable.

        The temp-table create/copy, the DROP of `source`, and the final
        RENAME are wrapped in one explicit transaction. SQLite DDL is
        transactional, so a crash or kill anywhere in this sequence
        rolls back to the pre-migration state on next open instead of
        leaving an orphaned `_migrating_*` table and an empty `target`
        (see _recover_orphaned_migrations for cleanup of databases that
        were already left in that state by a pre-fix version of this
        method)."""
        mapping = self._copy_columns(headers, self._table_columns(source))
        if not mapping:
            raise RuntimeError(
                f"Table {source!r} has an unrecognized schema; refusing to "
                "guess. Expected columns "
                f"{headers}, found {self._table_columns(source)}."
            )

        temp = f"_migrating_{target}"

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(f'DROP TABLE IF EXISTS "{temp}"')
            self._conn.execute(
                f'CREATE TABLE "{temp}" '
                f"({_column_defs(headers, primary_key=pk_index)});"
            )
            target_columns = ", ".join(f'"{name}"' for name, _ in mapping)
            source_columns = ", ".join(f'"{name}"' for _, name in mapping)
            self._conn.execute(
                f'INSERT OR IGNORE INTO "{temp}" ({target_columns}) '
                f'SELECT {source_columns} FROM "{source}"'
            )
            self._conn.execute(f'DROP TABLE "{source}"')
            self._conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{target}"')
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _merge_legacy_rows(self, target, source, headers):
        """Copy any rows from a separately-spelled legacy table into an
        existing current-schema table, then drop the legacy table.

        Wrapped in one explicit transaction so the INSERT and the DROP
        commit together: with no unique constraint on these append-only
        tables, INSERT OR IGNORE cannot detect a duplicate on its own,
        so the only thing preventing a crash-then-retry from
        re-inserting every legacy row on every subsequent restart is
        never letting the DROP survive without the INSERT (or vice
        versa)."""
        mapping = self._copy_columns(headers, self._table_columns(source))

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if mapping:
                target_columns = ", ".join(f'"{name}"' for name, _ in mapping)
                source_columns = ", ".join(f'"{name}"' for _, name in mapping)
                self._conn.execute(
                    f'INSERT OR IGNORE INTO "{target}" ({target_columns}) '
                    f'SELECT {source_columns} FROM "{source}"'
                )
            self._conn.execute(f'DROP TABLE "{source}"')
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _recover_orphaned_migrations(self):
        """Startup recovery for databases left behind by the pre-fix
        version of _rebuild_table, which committed DROP and RENAME as
        two independent autocommit statements. A crash between them
        leaves `_migrating_<table>` present and `<table>` missing; the
        table itself is already fully built under the current schema
        (the temp table is only renamed after the copy completes), so
        recovery is just finishing the rename that the earlier crash
        interrupted -- not re-deriving or guessing any data."""
        names = self._table_names()
        for table, _headers, _pk_index in _MIGRATABLE_TABLES:
            temp = f"_migrating_{table}"
            current = next(
                (name for name in names if name.lower() == table.lower()), None
            )
            if temp in names and current is None:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{table}"')
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                names = self._table_names()

    def _migrate_table(self, table, headers, pk_index):
        """Bring one logical table up to the current schema, migrating
        rows from any legacy table that currently shadows it."""
        names = self._table_names()
        current = next(
            (name for name in names if name.lower() == table.lower()), None
        )
        legacy = _LEGACY_TABLE_NAMES.get(table)
        legacy_name = legacy if legacy in names else None

        # Nothing exists yet -- the CREATE TABLE IF NOT EXISTS loop
        # below will make it with the current schema.
        if current is None and legacy_name is None:
            return

        if current is None:
            # Only the legacy spelling exists (e.g. NotificationGuard
            # with no notification_guard yet): create the current table
            # and let the merge below populate it.
            self._conn.execute(
                f"CREATE TABLE {table} ({_column_defs(headers, primary_key=pk_index)});"
            )
            current = table

        if self._table_columns(current) == headers:
            # Current schema already in place. A separately-spelled
            # legacy table can still exist next to it (NotificationGuard
            # alongside notification_guard): fold its rows in and drop it.
            if legacy_name is not None and legacy_name != current:
                self._merge_legacy_rows(current, legacy_name, headers)
            return

        # The table shadowing our name has mismatched columns -- rebuild
        # it with the current schema, carrying over recognizable rows.
        self._rebuild_table(current, table, headers, pk_index)

        if legacy_name is not None and legacy_name != current:
            self._merge_legacy_rows(table, legacy_name, headers)

    def _migrate_schema(self):
        """Bring any pre-existing tables up to the current schema (see
        _LEGACY_TABLE_NAMES). A no-op for a database that already
        matches; migrates in place, preserving all recognizable rows."""
        self._recover_orphaned_migrations()
        for table, headers, pk_index in _MIGRATABLE_TABLES:
            self._migrate_table(table, headers, pk_index)

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def save(self):
        """Compatibility no-op: writes are committed immediately in
        autocommit mode. Kept so existing callers that call
        `logger.run(logger.save)` keep working unchanged."""
        if self._conn is None:
            return
        self._conn.commit()

    async def run(self, func, *args, **kwargs):
        """
        Run a bound DBLogger method (has_job, create_job,
        update_job, log_gemini, log_notification, log_error,
        log_notification_guard, save, ...) on the single dedicated
        logger thread and await its result.

        Callers (job_processor, message_processor, notifier,
        channel_notifier, the Telegram handlers, the FreeHub worker,
        the notification guard) must use this instead of calling the
        methods directly -- routing everything through the one worker
        thread makes every read/write strictly serial and keeps all
        blocking database I/O off the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs))

    # ------------------------------------------------------------------
    # Generic row helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, cursor, row):
        return dict(zip([column[0] for column in cursor.description], row))

    def count_jobs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return row[0]

    def count_notifications(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM notifications").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def has_job(self, job_uuid) -> bool:
        """Cheap existence check so callers can skip reprocessing a
        job they've already logged (see app.job_processor dedup)."""
        row = self._conn.execute(
            'SELECT 1 FROM jobs WHERE "Job UUID" = ?',
            (job_uuid,),
        ).fetchone()
        return row is not None

    def create_job(
        self,
        job_uuid,
        job_id="",
        source="",
        title="",
        description="",
        raw_message="",
        filter_text="",
        company="",
        url="",
        filter_result=None,
        filter_time_ms=None,
        save=True,
    ):
        """
        `filter_result` is expected to be the dict returned by
        `filters.keyword_filter()`. Passing the whole dict (instead of
        a dozen individual keyword arguments) keeps this call in sync
        automatically as the filter's evidence trail evolves.

        `save=False` lets a caller defer the commit and batch several
        updates for the same job -- see app.job_processor.process_job.
        """

        filter_result = filter_result or {}

        columns = ", ".join(f'"{header}"' for header in JOB_HEADERS)
        placeholders = ", ".join("?" for _ in JOB_HEADERS)

        values = [
            datetime.now().isoformat(),
            job_uuid,
            job_id,
            source,

            title,
            description,
            raw_message,
            filter_text,

            company,
            url,

            filter_result.get("decision", ""),
            filter_result.get("reason", ""),

            ", ".join(filter_result.get("categories", []) or []),
            ", ".join(filter_result.get("negative_categories", []) or []),

            filter_result.get("has_core_positive", False),
            filter_result.get("has_core_negative", False),
            filter_result.get("core_positive_hit_count", 0),
            filter_result.get("supporting_positive_weight", 0),
            filter_result.get("supporting_negative_weight", 0),

            filter_result.get("title_core_positive", False),
            filter_result.get("title_core_negative", False),

            _join_matches(filter_result.get("positive_core_matches")),
            _join_matches(filter_result.get("positive_supporting_matches")),
            _join_matches(filter_result.get("negative_core_matches")),
            _join_matches(filter_result.get("negative_supporting_matches")),

            filter_result.get("hard_reject", False),
            ", ".join(filter_result.get("hard_reject_matches", []) or []),

            filter_result.get("notify_directly", False),
            filter_result.get("needs_gemini", False),
            "",
            "",
            "",
            filter_time_ms,
        ]

        self._conn.execute(
            f"INSERT OR IGNORE INTO jobs ({columns}) VALUES ({placeholders})",
            values,
        )

        if save:
            self.save()

    def create_job_if_absent(
        self,
        *,
        legacy_job_uuid=None,
        save=False,
        **kwargs,
    ):
        """
        Atomically check the canonical and optional legacy UUIDs and
        create the canonical row only when neither identity already
        exists.

        Runs inside an explicit transaction (on the dedicated logger
        worker thread through DBLogger.run()), so concurrent
        process_job() calls cannot both pass a separate has_job()
        check and then insert duplicate rows. The PRIMARY KEY on
        "Job UUID" is a second, database-level guard against
        duplicates.
        """
        job_uuid = kwargs["job_uuid"]

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self.has_job(job_uuid):
                self._conn.execute("COMMIT")
                return False

            if (
                legacy_job_uuid
                and legacy_job_uuid != job_uuid
                and self.has_job(legacy_job_uuid)
            ):
                self._conn.execute("COMMIT")
                return False

            self.create_job(save=False, **kwargs)
            self._conn.execute("COMMIT")
            return True
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_job(self, job_uuid):
        """
        Return the durable Jobs-table fields for one job (keyed by the
        human-readable column names, e.g. "Notification Status"), or
        None.

        This is intentionally a read-only operation and must be invoked
        through DBLogger.run() like every other database access.
        """
        cursor = self._conn.execute(
            'SELECT * FROM jobs WHERE "Job UUID" = ?',
            (job_uuid,),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_dict(cursor, row)

    def get_incomplete_notification_jobs(self):
        """
        Return every Jobs row whose notification workflow is durably
        recorded as started but not yet "Complete" -- i.e. "Pending"
        or containing a "Failed" leg for either channel.

        Nothing in the codebase previously swept this set: a
        transient Telegram rate limit, a blocked chat ID, or a
        Notification Guard provider outage all leave a row exactly
        like this, and it stayed here forever with no retry (see the
        audit's P1-1). This is what the periodic retry sweep in
        app.job_processor.retry_incomplete_notifications() reads from.
        """
        cursor = self._conn.execute(
            'SELECT * FROM jobs '
            'WHERE "Notification Status" IS NOT NULL '
            'AND "Notification Status" != \'\' '
            'AND "Notification Status" != \'Complete\' '
            'AND "Notification Status" != \'Suppressed\''
        )
        return [self._row_to_dict(cursor, row) for row in cursor.fetchall()]

    def get_last_job(self):
        """
        Return the most recently inserted Jobs row (keyed by the
        human-readable column names), or None. Used by tests.
        """
        cursor = self._conn.execute(
            "SELECT * FROM jobs ORDER BY rowid DESC LIMIT 1"
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return self._row_to_dict(cursor, row)

    def update_job(self, job_uuid, save=True, **fields):

        if not self.has_job(job_uuid):
            return False

        sets = []
        values = []

        for key, value in fields.items():

            if key not in COLUMN_MAP:
                continue

            if isinstance(value, list):
                value = ", ".join(value)

            sets.append(f'"{COLUMN_MAP[key]}" = ?')
            values.append(value)

        if not sets:
            return True

        values.append(job_uuid)

        self._conn.execute(
            f'UPDATE jobs SET {", ".join(sets)} WHERE "Job UUID" = ?',
            values,
        )

        if save:
            self.save()

        return True

    # ------------------------------------------------------------------
    # Append-only tables
    # ------------------------------------------------------------------

    def log_gemini(
        self,
        job_uuid,
        decision_before,
        reason_before,
        prompt_tokens,
        completion_tokens,
        response_time_ms,
        decision,
        confidence,
        save=True,
    ):

        columns = ", ".join(f'"{header}"' for header in GEMINI_HEADERS)
        placeholders = ", ".join("?" for _ in GEMINI_HEADERS)

        self._conn.execute(
            f"INSERT INTO gemini ({columns}) VALUES ({placeholders})",
            [
                datetime.now().isoformat(),
                job_uuid,
                decision_before,
                reason_before,
                prompt_tokens,
                completion_tokens,
                response_time_ms,
                decision,
                confidence,
            ],
        )

        if save:
            self.save()

    def log_category_decision(
        self,
        job_uuid,
        category_id,
        category_name,
        decision,
        reason,
        keyword_decision="",
        ai_used=False,
        llm_decision="",
        llm_confidence="",
        evidence_json="",
        save=True,
    ):
        columns = ", ".join(f'"{header}"' for header in CATEGORY_DECISION_HEADERS)
        placeholders = ", ".join("?" for _ in CATEGORY_DECISION_HEADERS)

        self._conn.execute(
            f"INSERT INTO category_decisions ({columns}) VALUES ({placeholders})",
            [
                datetime.now().isoformat(),
                job_uuid,
                category_id,
                category_name,
                decision,
                reason,
                keyword_decision,
                ai_used,
                llm_decision,
                llm_confidence,
                evidence_json,
            ],
        )

        if save:
            self.save()

    def get_category_decisions(self, job_uuid):
        cursor = self._conn.execute(
            'SELECT * FROM category_decisions WHERE "Job UUID" = ? ORDER BY rowid',
            (job_uuid,),
        )
        return [self._row_to_dict(cursor, row) for row in cursor.fetchall()]

    def log_notification(
        self,
        job_uuid,
        platform,
        status,
        save=True,
    ):

        columns = ", ".join(f'"{header}"' for header in NOTIFICATION_HEADERS)
        placeholders = ", ".join("?" for _ in NOTIFICATION_HEADERS)

        self._conn.execute(
            f"INSERT INTO notifications ({columns}) VALUES ({placeholders})",
            [
                datetime.now().isoformat(),
                job_uuid,
                platform,
                status,
            ],
        )

        if save:
            self.save()

    def log_error(
        self,
        module,
        error,
        job_uuid="",
        save=True,
    ):

        columns = ", ".join(f'"{header}"' for header in ERROR_HEADERS)
        placeholders = ", ".join("?" for _ in ERROR_HEADERS)

        self._conn.execute(
            f"INSERT INTO errors ({columns}) VALUES ({placeholders})",
            [
                datetime.now().isoformat(),
                job_uuid,
                module,
                str(error),
            ],
        )

        if save:
            self.save()

    def get_latest_guard_decision(self, job_uuid):
        """
        Return the most recent Notification Guard "Guard Decision"
        recorded for a job ("notify" / "do_not_notify" / "error"), or
        None if the guard was never evaluated for it.

        Used by the retry sweep (app.job_processor) to tell a genuine
        content-based rejection ("do_not_notify" -- a final decision,
        not worth re-asking the guard about every sweep) apart from a
        provider outage ("error" -- transient, must keep being
        retried) so P1-1's generic retry doesn't compound with P1-2's
        fail-closed guard into hammering Groq forever over a job it
        has already genuinely rejected.
        """
        cursor = self._conn.execute(
            'SELECT "Guard Decision" FROM notification_guard '
            'WHERE "Job UUID" = ? ORDER BY rowid DESC LIMIT 1',
            (job_uuid,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def log_notification_guard(
        self,
        job_uuid,
        source,
        title,
        original_decision,
        guard_decision,
        provider,
        model,
        response_time_ms,
        error="",
        save=True,
    ):

        columns = ", ".join(f'"{header}"' for header in NOTIFICATION_GUARD_HEADERS)
        placeholders = ", ".join("?" for _ in NOTIFICATION_GUARD_HEADERS)

        self._conn.execute(
            f"INSERT INTO notification_guard ({columns}) VALUES ({placeholders})",
            [
                datetime.now().isoformat(),
                job_uuid,
                source,
                title,
                original_decision,
                guard_decision,
                provider,
                model,
                response_time_ms,
                error,
            ],
        )

        if save:
            self.save()


logger = DBLogger()


def initialize_database():
    logger.initialize()
