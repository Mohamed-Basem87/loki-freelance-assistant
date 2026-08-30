import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
import uuid


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
    "Category ID",
    "Category Selection Method",
    "Category Candidates",
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
    "category_id": "Category ID",
    "category_selection_method": "Category Selection Method",
    "category_candidates": "Category Candidates",
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
    # The category the guard settled on for a "notify" decision: either
    # the job's original keyword-matched category (unchanged) or
    # "full_stack" when the guard determined the job is broader than
    # the tiering system's single-category match. Persisted alongside
    # "Guard Decision" in the same insert so the two facts are always
    # written atomically together -- a resumed job can never observe
    # "notify" without also knowing which category it was "notify"
    # for. Empty for "do_not_notify"/"error" rows, where no category
    # choice is meaningful.
    "Guard Category",
]

USER_HEADERS = [
    "User ID",
    "Telegram User ID",
    "Username",
    "First Name",
    "Destination Type",
    "Categories",
    "Sources",
    "Is Active",
    "Created At",
    "Updated At",
]

CATEGORY_HEADERS = [
    "Category ID",
    "Name",
    "Description",
    "Enabled",
    "Created At",
]

USER_NOTIFICATION_HEADERS = [
    "Notification ID",
    "Job UUID",
    "User ID",
    "Telegram User ID",
    "Category ID",
    "Status",
    "Attempts",
    "Last Error",
    "Created At",
    "Updated At",
    "Next Attempt At",
]

SUBSCRIPTION_EVENT_HEADERS = [
    "Event ID",
    "Telegram User ID",
    "First Name",
    "Username",
    "Event Type",
    "Occurred At",
    "Trigger",
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
    f'CREATE TABLE IF NOT EXISTS users ({_column_defs(USER_HEADERS, primary_key=0)});',
    f'CREATE TABLE IF NOT EXISTS categories ({_column_defs(CATEGORY_HEADERS, primary_key=0)});',
    f'CREATE TABLE IF NOT EXISTS user_notifications ({_column_defs(USER_NOTIFICATION_HEADERS, primary_key=0)});',
    f'CREATE TABLE IF NOT EXISTS subscription_events ({_column_defs(SUBSCRIPTION_EVENT_HEADERS, primary_key=0)});',
    f'CREATE UNIQUE INDEX IF NOT EXISTS idx_user_notifications_job_user '
    f'ON user_notifications ("Job UUID", "User ID");',
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

        self._ensure_current_columns()

        # Fold any pre-feature per-user category subscriptions (legacy
        # user_categories table, if this database still has one) into
        # users.Categories once, then drop the legacy table. No-op for
        # fresh databases and for databases already migrated.
        self._migrate_user_categories_into_users()

        # Seed the category registry into SQLite. The registry is the
        # source of truth for available category definitions; SQLite
        # stores the user-facing selectable catalog.
        from app.categories.registry import enabled_categories
        for profile in enabled_categories():
            self.ensure_category(
                profile.id,
                profile.name,
                profile.description,
                enabled=True,
                save=False,
            )
        self.save()

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

    def _ensure_current_columns(self):
        """Add newly introduced columns to an already-current SQLite DB."""
        for table, headers in (
            ("jobs", JOB_HEADERS),
            ("users", USER_HEADERS),
            ("user_notifications", USER_NOTIFICATION_HEADERS),
            ("notification_guard", NOTIFICATION_GUARD_HEADERS),
        ):
            existing = set(self._table_columns(table))
            for header in headers:
                if header not in existing:
                    self._conn.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{header}" TEXT'
                    )

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

        Callers (job_processor, message_processor, notifier, the Telegram
        handlers, the FreeHub worker, the subscriber worker, and the
        notification guard) must use this instead of calling the
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
            filter_result.get("category_id", ""),
            filter_result.get("category_selection_method", ""),
            filter_result.get("category_candidates", ""),
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
        Return every Jobs row whose private notification workflow is
        durably recorded as started but not yet "Complete" -- i.e.
        "Pending" or containing a failed Telegram leg. Category
        subscriber delivery is durable in user_notifications and is
        retried by the subscriber worker independently.

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

    def get_latest_guard_decision_with_category(self, job_uuid):
        """
        Return (guard_decision, guard_category) from the most recent
        Notification Guard row for a job, or (None, None) if the guard
        was never evaluated for it.

        Unlike get_latest_guard_decision(), this also surfaces the
        category the guard settled on ("full_stack" or the job's
        original category) for a "notify" decision, persisted
        atomically alongside the decision in the same insert (see
        NOTIFICATION_GUARD_HEADERS). This lets a resumed job reapply a
        durable reclassification without re-asking the provider, and
        without ever risking an inconsistent state where a "notify"
        decision is known but which category it applies to is not.
        """
        cursor = self._conn.execute(
            'SELECT "Guard Decision", "Guard Category" FROM notification_guard '
            'WHERE "Job UUID" = ? ORDER BY rowid DESC LIMIT 1',
            (job_uuid,),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)

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
        guard_category="",
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
                guard_category,
            ],
        )

        if save:
            self.save()


    # ------------------------------------------------------------------
    # User/category routing
    # ------------------------------------------------------------------

    def ensure_category(self, category_id, name, description="", enabled=True, save=True):
        self._conn.execute(
            'INSERT OR IGNORE INTO categories '
            '("Category ID", "Name", "Description", "Enabled", "Created At") '
            'VALUES (?, ?, ?, ?, ?)',
            (category_id, name, description, "1" if enabled else "0",
             datetime.now().isoformat()),
        )
        if save:
            self.save()

    def _migrate_user_categories_into_users(self):
        """One-time migration: fold any legacy per-user category rows
        (old user_categories table) into users.Categories, then drop the
        legacy table so it can never be read from or written to again.
        Runs once at startup (see initialize()); a no-op for a fresh
        database or a database that has already been migrated (in either
        case the SELECT below raises OperationalError because the table
        no longer exists).
        """
        try:
            rows = self._conn.execute(
                'SELECT "User ID", "Category ID" FROM user_categories'
            ).fetchall()
        except sqlite3.OperationalError:
            return

        grouped = {}
        for user_id, category_id in rows:
            grouped.setdefault(str(user_id), set()).add(str(category_id).strip().lower())

        for user_id, categories in grouped.items():
            current = self._conn.execute(
                'SELECT "Categories" FROM users WHERE "User ID" = ?',
                (user_id,),
            ).fetchone()
            existing = {
                item.strip().lower()
                for item in ((current[0] if current else "") or "").split(",")
                if item.strip()
            }
            merged = ",".join(sorted(existing | categories))
            self._conn.execute(
                'UPDATE users SET "Categories" = ? WHERE "User ID" = ?',
                (merged, user_id),
            )

        self._conn.execute('DROP TABLE user_categories')

    def ensure_user(self, telegram_user_id, username="", first_name="", save=True):
        now = datetime.now().isoformat()
        cursor = self._conn.execute(
            'SELECT "User ID" FROM users WHERE "Telegram User ID" = ?',
            (str(telegram_user_id),),
        )
        row = cursor.fetchone()
        if row:
            # Do not reactivate an existing user here. ensure_user() is also
            # called by /categories and /sources, and those commands must not
            # silently undo an explicit /stop. Only /start is authoritative
            # for re-subscribing a user.
            self._conn.execute(
                'UPDATE users SET "Username" = ?, "First Name" = ?, '
                '"Destination Type" = "user", "Updated At" = ? '
                'WHERE "User ID" = ?',
                (username or "", first_name or "", now, row[0]),
            )
            user_id = row[0]
        else:
            user_id = str(uuid.uuid4())
            self._conn.execute(
                'INSERT INTO users '
                '("User ID", "Telegram User ID", "Username", "First Name", '
                '"Destination Type", "Is Active", "Created At", "Updated At") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, str(telegram_user_id), username or "", first_name or "",
                 "user", "1", now, now),
            )
        if save:
            self.save()
        return user_id

    def record_subscription_event(
        self,
        telegram_user_id,
        first_name="",
        username="",
        active=True,
        trigger="",
        save=True,
    ):
        """Record a subscription transition and return whether it changed."""
        chat_id = str(telegram_user_id)
        now = datetime.now().isoformat()
        desired = "1" if active else "0"

        row = self._conn.execute(
            'SELECT "Is Active" FROM users WHERE "Telegram User ID" = ?',
            (chat_id,),
        ).fetchone()

        if row is None:
            self.ensure_user(
                telegram_user_id,
                username=username,
                first_name=first_name,
                save=False,
            )
            self._conn.execute(
                'UPDATE users SET "Is Active" = ?, "Username" = ?, "First Name" = ?, '
                '"Updated At" = ? WHERE "Telegram User ID" = ?',
                (desired, username or "", first_name or "", now, chat_id),
            )
            self._conn.execute(
                'INSERT INTO subscription_events '
                '("Event ID", "Telegram User ID", "First Name", "Username", '
                '"Event Type", "Occurred At", "Trigger") '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    str(uuid.uuid4()),
                    chat_id,
                    first_name or "",
                    username or "",
                    "subscribed" if active else "unsubscribed",
                    now,
                    trigger or "",
                ),
            )
            if save:
                self.save()
            return True

        current = str(row[0])

        if current == desired:
            self._conn.execute(
                'UPDATE users SET "Username" = ?, "First Name" = ?, '
                '"Updated At" = ? WHERE "Telegram User ID" = ?',
                (username or "", first_name or "", now, chat_id),
            )
            if save:
                self.save()
            return False

        self._conn.execute(
            'UPDATE users SET "Is Active" = ?, "Username" = ?, "First Name" = ?, '
            '"Updated At" = ? WHERE "Telegram User ID" = ?',
            (desired, username or "", first_name or "", now, chat_id),
        )
        self._conn.execute(
            'INSERT INTO subscription_events '
            '("Event ID", "Telegram User ID", "First Name", "Username", '
            '"Event Type", "Occurred At", "Trigger") '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                str(uuid.uuid4()),
                chat_id,
                first_name or "",
                username or "",
                "subscribed" if active else "unsubscribed",
                now,
                trigger or "",
            ),
        )
        if save:
            self.save()
        return True

    def ensure_channel_destination(self, telegram_chat_id, title="", save=True):
        """Register a Telegram channel as a normal subscription destination."""
        now = datetime.now().isoformat()
        chat_id = str(telegram_chat_id)
        cursor = self._conn.execute(
            'SELECT "User ID" FROM users WHERE "Telegram User ID" = ?',
            (chat_id,),
        )
        row = cursor.fetchone()
        if row:
            destination_id = row[0]
            self._conn.execute(
                'UPDATE users SET "Username" = ?, "First Name" = ?, '
                '"Destination Type" = "channel", "Is Active" = "1", "Updated At" = ? '
                'WHERE "User ID" = ?',
                (title or "", title or "", now, destination_id),
            )
        else:
            destination_id = str(uuid.uuid4())
            self._conn.execute(
                'INSERT INTO users '
                '("User ID", "Telegram User ID", "Username", "First Name", '
                '"Destination Type", "Is Active", "Created At", "Updated At") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (destination_id, chat_id, title or "", title or "",
                 "channel", "1", now, now),
            )
        if save:
            self.save()
        return destination_id

    def get_destination(self, telegram_chat_id):
        cursor = self._conn.execute(
            'SELECT * FROM users WHERE "Telegram User ID" = ?',
            (str(telegram_chat_id),),
        )
        row = cursor.fetchone()
        return self._row_to_dict(cursor, row) if row else None

    def set_user_category(self, user_id, category_id, enabled=True, save=True):
        """Add/remove one category preference stored directly on the user row.

        Categories are stored as a comma-separated list on users.Categories,
        the same pattern used for users.Sources (see set_user_source). The
        legacy user_categories table is migration-only: its data is folded
        into this column once at startup and the table is then dropped
        (see _migrate_user_categories_into_users), so nothing at runtime
        reads or writes it.
        """
        category = str(category_id or "").strip().lower()
        if not category:
            return

        row = self._conn.execute(
            'SELECT "Categories" FROM users WHERE "User ID" = ?',
            (str(user_id),),
        ).fetchone()
        if row is None:
            return

        selected = {
            item.strip().lower()
            for item in (row[0] or "").split(",")
            if item.strip()
        }
        if enabled:
            selected.add(category)
        else:
            selected.discard(category)

        value = ",".join(sorted(selected))
        self._conn.execute(
            'UPDATE users SET "Categories" = ?, "Updated At" = ? '
            'WHERE "User ID" = ?',
            (value, datetime.now().isoformat(), str(user_id)),
        )
        if save:
            self.save()

    def get_user_categories(self, user_id):
        row = self._conn.execute(
            'SELECT "Categories" FROM users WHERE "User ID" = ?',
            (str(user_id),),
        ).fetchone()
        if not row or not row[0]:
            return []
        return [
            item.strip().lower()
            for item in row[0].split(",")
            if item.strip()
        ]

    def set_user_source(self, user_id, source, enabled=True, save=True):
        """Add/remove one source preference stored directly on the user row.

        An empty Sources value means the user has not opted into source
        filtering and therefore receives all sources. Source IDs are stored
        as a comma-separated list because sources are a user preference,
        not independent database entities.
        """
        source = str(source or "").strip().lower()
        if not source:
            return

        row = self._conn.execute(
            'SELECT "Sources" FROM users WHERE "User ID" = ?',
            (str(user_id),),
        ).fetchone()
        if row is None:
            return

        selected = {
            item.strip().lower()
            for item in (row[0] or "").split(",")
            if item.strip()
        }
        if enabled:
            selected.add(source)
        else:
            selected.discard(source)

        value = ",".join(sorted(selected))
        self._conn.execute(
            'UPDATE users SET "Sources" = ?, "Updated At" = ? '
            'WHERE "User ID" = ?',
            (value, datetime.now().isoformat(), str(user_id)),
        )
        if save:
            self.save()

    def get_user_sources(self, user_id):
        row = self._conn.execute(
            'SELECT "Sources" FROM users WHERE "User ID" = ?',
            (str(user_id),),
        ).fetchone()
        if not row or not row[0]:
            return []
        return [
            item.strip().lower()
            for item in row[0].split(",")
            if item.strip()
        ]

    def get_category_subscribers(self, category_id, source=""):
        category_id = str(category_id or "").strip().lower()
        normalized_source = str(source or "").strip().lower()

        cursor = self._conn.execute(
            'SELECT u."User ID", u."Telegram User ID", '
            'u."Destination Type", u."Categories", u."Sources" '
            'FROM users u WHERE u."Is Active" = "1"'
        )

        aliases = {
            "mostaql": ("mostaql", "مستقل"),
            "nafezly": ("nafezly", "نفذلي"),
            "kafiil": ("kafiil", "كفيل"),
            "freelancer": ("freelancer",),
        }

        def category_matches(stored):
            # Empty categories preserves the previous "no subscription" meaning.
            if not stored:
                return False
            return category_id in {
                item.strip().lower()
                for item in stored.split(",")
                if item.strip()
            }

        def source_matches(stored):
            # Empty source preference means all sources.
            if not stored or not normalized_source:
                return True
            selected = {
                item.strip().lower()
                for item in stored.split(",")
                if item.strip()
            }
            for source_id in selected:
                if source_id in aliases and any(
                    alias in normalized_source for alias in aliases[source_id]
                ):
                    return True
            return False

        return [
            {
                "user_id": row[0],
                "telegram_user_id": row[1],
                "destination_type": row[2] or "user",
            }
            for row in cursor.fetchall()
            if (
                (row[2] or "user") != "user"
                or (
                    category_matches(row[3])
                    and source_matches(row[4])
                )
            )
        ]

    def queue_user_notifications(self, job_uuid, category_id, source="", save=True):
        subscribers = self.get_category_subscribers(category_id, source)
        now = datetime.now().isoformat()
        queued = 0

        for subscriber in subscribers:
            cursor = self._conn.execute(
                'INSERT OR IGNORE INTO user_notifications '
                '("Notification ID", "Job UUID", "User ID", "Telegram User ID", '
                '"Category ID", "Status", "Attempts", "Last Error", '
                '"Created At", "Updated At", "Next Attempt At") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(uuid.uuid4()),
                    job_uuid,
                    str(subscriber["user_id"]),
                    str(subscriber["telegram_user_id"]),
                    category_id,
                    "Pending",
                    "0",
                    "",
                    now,
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                queued += 1

        if save:
            self.save()
        return queued

    def set_user_active(self, telegram_user_id, active, save=True):
        self.set_destination_active(telegram_user_id, active, save=save)

    def set_destination_active(self, telegram_chat_id, active, save=True):
        self._conn.execute(
            'UPDATE users SET "Is Active" = ?, "Updated At" = ? '
            'WHERE "Telegram User ID" = ?',
            ("1" if active else "0", datetime.now().isoformat(), str(telegram_chat_id)),
        )
        if save:
            self.save()

    def reset_sending_user_notifications(self, save=True):
        self._conn.execute(
            'UPDATE user_notifications SET "Status" = "Pending", '
            '"Next Attempt At" = ? WHERE "Status" = "Sending"',
            (datetime.now().isoformat(),),
        )
        if save:
            self.save()

    def claim_pending_user_notifications(self, limit=20):
        """Claim a batch for delivery; all DB access is serialized."""
        now = datetime.now().isoformat()
        cursor = self._conn.execute(
            'SELECT un.*, u."Destination Type" AS "Destination Type" '
            'FROM user_notifications un '
            'JOIN users u ON u."User ID" = un."User ID" '
            'WHERE (un."Status" = "Pending" OR un."Status" = "Failed" '
            '       OR un."Status" = "RateLimited") '
            'AND u."Is Active" = "1" '
            # RetryAfter is Telegram backpressure, not a failed delivery.
            # The RetryAfter handler in user_bot.py records it as its own
            # "RateLimited" status (never "Failed"), so those rows stay
            # claimable past the normal failure attempt budget; the
            # server-requested retry time is still honored via Next
            # Attempt At below. Genuine failures ("Failed") still stop
            # being claimed once Attempts reaches the cap.
            'AND (CAST(un."Attempts" AS INTEGER) < 5 '
            '     OR un."Status" = "RateLimited") '
            'AND (un."Next Attempt At" IS NULL OR un."Next Attempt At" = "" '
            'OR un."Next Attempt At" <= ?) '
            'ORDER BY un.rowid LIMIT ?',
            (now, int(limit)),
        )
        rows = [self._row_to_dict(cursor, row) for row in cursor.fetchall()]

        for row in rows:
            attempts = int(row.get("Attempts") or 0) + 1
            self._conn.execute(
                'UPDATE user_notifications SET "Status" = "Sending", '
                '"Attempts" = ?, "Updated At" = ? '
                'WHERE "Notification ID" = ? AND '
                '("Status" = "Pending" OR "Status" = "Failed" '
                ' OR "Status" = "RateLimited")',
                (str(attempts), now, row["Notification ID"]),
            )
            row["Status"] = "Sending"
            row["Attempts"] = str(attempts)

        self.save()
        return rows

    def update_user_notification(
        self,
        notification_id,
        status,
        attempts=None,
        last_error="",
        next_attempt_at=None,
        save=True,
    ):
        sets = ['"Status" = ?', '"Updated At" = ?']
        values = [status, datetime.now().isoformat()]
        if attempts is not None:
            sets.append('"Attempts" = ?')
            values.append(str(attempts))
        if last_error is not None:
            sets.append('"Last Error" = ?')
            values.append(last_error)
        if next_attempt_at is not None:
            sets.append('"Next Attempt At" = ?')
            values.append(next_attempt_at)
        elif status == "Sent":
            sets.append('"Next Attempt At" = ?')
            values.append("")
        values.append(str(notification_id))
        self._conn.execute(
            f'UPDATE user_notifications SET {", ".join(sets)} '
            f'WHERE "Notification ID" = ?',
            values,
        )
        if save:
            self.save()


logger = DBLogger()


def initialize_database():
    logger.initialize()
