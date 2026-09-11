"""SQLite audit/notification persistence engine.

SINGLE-PROCESS ASSUMPTION (explicit):
This codebase intentionally runs ONE bot process against ONE
``loki_freelance_bot.db`` file. All reads/writes flow through a single
dedicated worker thread (``DBLogger.run`` / the ``_EXECUTOR`` below),
which serializes every statement -- that serialization is what makes
check-then-act sequences (e.g. ``claim_pending_classification``,
``claim_pending_user_notifications``) atomic within this process.

This is NOT atomic across processes. Nothing here implements file-level
locking, a multi-process lease, or WAL coordination, and the schema does
not claim to. Running a second bot process against the same database
file concurrently is unsupported and can produce races that the
single-process design never anticipated. If multi-process operation is
ever required, the claim semantics must move to a real multi-process
transaction coordinator rather than relying on this module's
single-thread serialization.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from app.runtime_config import RUNTIME
import sqlite3
import time

from app.runtime_config import RUNTIME, SOURCES
from app.heartbeat import liveness, STATE_ALIVE, STATE_DEAD
import uuid


# The audit log lives in a SQLite database file next to docker-compose.yml
# (bind-mounted read/write, never baked into the image), so it is directly
# visible/inspectable on the host and survives container rebuilds.
DB_FILE = Path(RUNTIME.database_file_path)

# All DBLogger reads/writes must go through this single worker thread
# (see DBLogger.run below). Funneling every access through one dedicated
# thread keeps them strictly serial (no two logger calls ever touch the
# database at the same time) without ever blocking the event loop.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-logger")

# This executor has exactly one worker thread; if a call ever blocks
# forever (e.g. SQLite hitting a lock that never clears, or the DB
# file living on a stalled volume), every subsequent DBLogger.run()
# call system-wide -- including from a completely unrelated request --
# would queue up behind it forever with no exception and nothing to
# log. See the matching comment in app.state for a real incident this
# same failure shape caused there; this mirrors that module's fix so
# the DB path (which touches far more of the app -- bot commands,
# notifications, and ingestion all depend on it, not just ingestion)
# gets the same protection even though it wasn't implicated in that
# specific incident. 60s rather than app.state's 30s, since a schema
# migration (_rebuild_table, on a large historical table) can
# legitimately take longer than a routine single-row write without
# actually being stuck.
_DB_TIMEOUT_SECONDS = RUNTIME.database_timeout_seconds


class StuckExecutorError(RuntimeError):
    """Raised when a DBLogger call didn't return within
    _DB_TIMEOUT_SECONDS. See the comment above _DB_TIMEOUT_SECONDS.

    The stuck worker thread cannot be forcibly killed and will leak
    until the process restarts, so the shared SQLite backend is
    QUARANTINED rather than replaced: _executor_poisoned is set and
    every subsequent DBLogger.run() call fails closed with this
    exception until process restart. A fresh executor is deliberately
    NOT installed -- it could let a second worker touch the same
    connection concurrently with the still-stuck one. Restart is the
    only safe recovery."""


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
    "Identity Source",
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

    # Explicit classification-retry scheduling (audit finding P1-3):
    # set whenever an LLM classification/arbitration attempt fails and
    # the job is durably parked as Final Decision=Pending / Decision
    # Reason=LLM Error. Holds a unix timestamp (as text) before which
    # process_job() must not attempt another real LLM call for this
    # job, no matter how many independent callers re-surface it (the
    # dedicated classification_retry_loop sweep AND FreeHub's own
    # pending-project rediscovery on every poll both funnel through
    # process_job() and, before this, both could trigger a fresh LLM
    # request for the same job every time they ran -- see the audit's
    # "LLM retry amplification" finding for the quota/cost impact).
    "Classification Retry Not Before",
]

# snake_case keyword -> SQL column name (the human-readable header).
COLUMN_MAP = {
    "timestamp": "Timestamp",
    "job_uuid": "Job UUID",
    "job_id": "Job ID",
    "source": "Source",
    "identity_source": "Identity Source",

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
    "classification_retry_not_before": "Classification Retry Not Before",
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
    "Provider",
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
    "Claimed At",
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

# How long a claimed-but-undelivered "Sending" lease may go untouched
# before it is assumed abandoned (the claiming process crashed or was
# killed mid-delivery) and returned to the "Pending" queue without
# charging the notification an attempt. See
# DBLogger._recover_stale_sending_user_notifications. A single delivery
# (_send_one) normally completes in well under this window; it is only
# ever blown through by a dead process, not by a slow-but-alive one.
USER_NOTIFICATION_CLAIM_LEASE_SECONDS = 300

# Deterministic precedence used when _migrate_user_uniqueness must
# resolve a ("Job UUID", "User ID") collision between a duplicate
# user's queued user_notifications row and a row the survivor already
# owns for the same job (both rows exist because lookup-before-insert
# is only best-effort -- the dedup migration is the first point
# uniqueness is enforced, so legacy data can contain both). The
# row representing the most-completed delivery wins: a real "Sent"
# must outrank one that merely got rate-limited, and a delivery that
# was canceled must not block a live one. Unknown/malformed status
# values sort below every known one (they lose to "Cancelled" and,
# by tie-with-lowest, lose to the survivor).
_USER_NOTIFICATION_STATUS_PRECEDENCE = {
    "Sent": 5,
    "Sending": 4,
    "Pending": 3,
    "RateLimited": 2,
    "Failed": 1,
    "Cancelled": 0,
}


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
    # Queue-claim predicates are exercised on every polling cycle. These
    # indexes avoid repeated full scans as notification volume grows.
    'CREATE INDEX IF NOT EXISTS idx_user_notifications_claim '
    'ON user_notifications ("Status", "Next Attempt At", "Attempts");',
    'CREATE INDEX IF NOT EXISTS idx_jobs_notification_status '
    'ON jobs ("Notification Status");',
    # P2-4: the classification retry sweep (see
    # get_incomplete_classification_jobs above) filters on this exact
    # (Final Decision, Decision Reason) pair on every
    # classification_retry_loop tick. Without a dedicated index this
    # degrades into a full table scan of `jobs` as it grows.
    'CREATE INDEX IF NOT EXISTS idx_jobs_final_decision_reason '
    'ON jobs ("Final Decision", "Decision Reason");',
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


# ------------------------------------------------------------------
# Legacy notification-state normalization.
#
# Historically the code wrote a single-sink plain-text status straight
# into jobs."Notification Status" ("Telegram: Sent", "Telegram:
# Failed", "Telegram: Suppressed"). The current per-sink
# representation is "Sink:<id>=Sent"/"Sink:<id>=Failed", with multiple
# sinks joined by "; ". Existing databases can still contain rows in
# the legacy format.
#
# The legacy format is indistinguishable from "unresolved" to the
# retry sweep and to NotificationService's per-sink parser:
#   * "Telegram: Sent" is selected by get_incomplete_notification_jobs
#     (it is not 'Complete'/'Suppressed') AND its token does not match
#     the "Sink:telegram=" marker, so the send gate does not fire and
#     the Telegram sink is RE-SENT -- a duplicate notification for a
#     message a user already received.
#   * "Telegram: Failed" is re-attempted (which is the desired retry
#     behavior, but only if it is normalized to the per-sink marker so
#     a later sibling sink's success is not clobbered).
#   * "Telegram: Suppressed" is re-sent and rolled to 'Complete',
#     permanently overriding the suppression.
#
# _normalize_legacy_notification_state rewrites the legacy token into
# the current per-sink form (or the terminal 'Suppressed' rollup) so
# every one of those legacy rows means exactly what it always meant,
# without re-sending anything that was already delivered.
# ------------------------------------------------------------------
_LEGACY_NOTIFICATION_SINK = "telegram"
_LEGACY_STATUS_PATTERN = "Telegram: "


def _normalize_legacy_notification_state(value):
    """Translate one job's legacy 'Notification Status' into the current
    per-sink form.

    Accepts a raw column value (any mix of legacy "Telegram: X" tokens
    and current "Sink:<id>=Y" tokens). Returns the normalized string, or
    None if there was no legacy token to translate (so callers can skip
    the write entirely). Terminal 'Suppressed' is preserved as-is.

    Rules (each idempotent):
      * "Telegram: Sent"      -> "Sink:telegram=Sent"   (never re-send)
      * "Telegram: Failed"    -> "Sink:telegram=Failed" (retryable)
      * "Telegram: Suppressed"-> "Suppressed"           (terminal)
      * Already-present current Sink:telegram= markers win over a
        contradicting legacy token (the per-sink record is newer).
      * A row that was wholly "Telegram: Suppressed" becomes the
        terminal "Suppressed" rollup and is excluded from the sweep.
    """
    if not value:
        return None

    parts = [p.strip() for p in str(value).split(";") if p.strip()]
    legacy_token = next(
        (p for p in parts if p.startswith(_LEGACY_STATUS_PATTERN)), None
    )
    if legacy_token is None:
        return None

    legacy_state = legacy_token[len(_LEGACY_STATUS_PATTERN):].strip()

    # Current per-sink tokens for the legacy sink take precedence over
    # the legacy token (they are a later, more precise record).
    sink_marker = f"Sink:{_LEGACY_NOTIFICATION_SINK}="
    has_current_sink = any(p.startswith(sink_marker) for p in parts)
    remaining = [p for p in parts if not p.startswith(_LEGACY_STATUS_PATTERN)]

    if legacy_state == "Suppressed":
        # A whole-job suppression is terminal in the current model.
        return "Suppressed"

    if has_current_sink:
        # The per-sink record is authoritative and already present; just
        # drop the redundant legacy token.
        return "; ".join(remaining) or None

    normalized = " ".join(remaining)
    token = f"{sink_marker}{legacy_state}"
    normalized = "; ".join(p for p in (normalized, token) if p)
    return normalized


class DBLogger:

    def __init__(self):
        self.path = DB_FILE
        self._conn = None
        self.max_user_notification_attempts = RUNTIME.user_bot_max_attempts
        self.user_notification_claim_lease_seconds = USER_NOTIFICATION_CLAIM_LEASE_SECONDS
        self._executor_poisoned = False

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

        # Fold any legacy "Telegram: Sent/Failed/Suppressed" values in
        # jobs."Notification Status" into the current per-sink form, so
        # a legacy "Telegram: Sent" row can never be re-sent by the
        # retry sweep. Runs after the schema/column migrations so the
        # column definitely exists. Idempotent; see
        # _migrate_legacy_notification_states.
        self._migrate_legacy_notification_states()

        # Fold any pre-feature per-user category subscriptions (legacy
        # user_categories table, if this database still has one) into
        # users.Categories once, then drop the legacy table. No-op for
        # fresh databases and for databases already migrated.
        self._migrate_user_categories_into_users()

        # Enforce the DB-level "one row per Telegram User ID" guarantee:
        # dedupe any legacy duplicate users rows (merging their preferences)
        # and create a unique index so it can never recur. Runs after the
        # users table and column migrations exist.
        self._migrate_user_uniqueness()

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

    def _migrate_legacy_notification_states(self):
        """One-time, transactional, idempotent data migration that folds
        any legacy "Telegram: Sent/Failed/Suppressed" rows in
        jobs."Notification Status" into the current per-sink form.

        Runs on every startup but is a strict no-op once every legacy
        token has been normalized (the rewrite is idempotent: a row
        containing no legacy token is left untouched). The whole pass is
        wrapped in a single transaction so a crash mid-write can never
        leave a half-migrated column -- on the next open only the still-
        legacy rows would be rewritten, with every already-normalized
        row preserved.

        The sine qua non is that a legacy "Telegram: Sent" row must
        never be re-sent: without this, the retry sweep re-picks it
        (it is not 'Complete'/'Suppressed') and NotificationService's
        per-sink parser sees no "Sink:telegram=" marker, so the send
        gate does not fire and the message is delivered a second time.
        """
        try:
            rows = self._conn.execute(
                'SELECT rowid, "Notification Status" FROM jobs '
                'WHERE "Notification Status" LIKE ?',
                (f"%{_LEGACY_STATUS_PATTERN}%",),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            # A fresh/empty database (or one whose jobs table is not yet
            # current) has nothing to migrate. Only the missing-table case
            # is swallowed: "database is locked" (or any other real
            # operational failure) must propagate, not be silently
            # papered over as "nothing to do" -- a locked database still
            # leaves legacy rows un-normalized and would otherwise let a
            # legacy "Telegram: Sent" row be re-sent on the next sweep.
            if "no such table" not in str(exc):
                raise
            return

        if not rows:
            return

        updates = []
        for rowid, raw in rows:
            if raw is None:
                continue
            normalized = _normalize_legacy_notification_state(raw)
            if normalized is None:
                continue
            updates.append((normalized, rowid))

        if not updates:
            return

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                'UPDATE jobs SET "Notification Status" = ? WHERE rowid = ?',
                updates,
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def shutdown(self):
        """Deterministic lifecycle end: shut down the shared worker
        executor and close the SQLite connection. Idempotent. After this
        call no further DB operations can run -- call only when the
        process is shutting down (see app.bot.run).

        Quarantine exception (audit finding P2-A): when the executor's
        worker thread is presumed stuck (``self._executor_poisoned``, set
        by run() when an operation timed out), the connection is
        DELIBERATELY NOT closed. A cancelled Future cannot stop the
        thread, so it may still be mid-statement on this connection;
        closing it there would tear the connection out from under a
        running worker for no benefit. The thread and its connection are
        unrecoverable either way, so the only guaranteed-safe cleanup is
        the OS reclaiming the process's file descriptors at process
        exit -- the connection is leaked, never corrupted."""
        global _EXECUTOR
        try:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        if self._executor_poisoned:
            print(
                "[DB] shutdown: the logger worker thread was quarantined "
                "after a timed-out operation; leaving the SQLite connection "
                "open for the OS to reclaim rather than closing it under a "
                "possibly-still-running worker thread."
            )
            return
        self.close()

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

        Wrapped in a timeout for the same reason as app.state.run() --
        see the comment above _DB_TIMEOUT_SECONDS. On timeout the shared
        SQLite backend is QUARANTINED: _executor_poisoned is set and
        this and every later call raises StuckExecutorError (fail-closed)
        until the process restarts. The stuck worker thread cannot be
        killed and no fresh executor is installed (a second worker could
        touch the same connection concurrently), so restart is the only
        safe recovery.
        """
        global _EXECUTOR

        if self._executor_poisoned:
            raise StuckExecutorError(
                "DB worker is quarantined after a timed-out operation; "
                "restart the process before reusing the SQLite connection."
            )

        loop = asyncio.get_running_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs)),
                timeout=_DB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # A cancelled Future cannot stop the underlying thread. Never
            # replace the executor while that worker may still be touching
            # this connection. Quarantine the shared DB backend instead;
            # callers fail closed until process restart.
            self._executor_poisoned = True
            func_name = getattr(func, "__name__", repr(func))
            print(
                f"[DB] {func_name} timed out after {_DB_TIMEOUT_SECONDS}s. "
                "The worker thread cannot be killed safely, so the shared "
                "SQLite backend is quarantined; no new DB operation will "
                "run against it until process restart."
            )
            # Surface the quarantine to the out-of-process healthcheck via
            # the same heartbeat file every other worker reports through
            # (audit finding: the healthcheck's separate sqlite3.connect()
            # persistence check cannot see this in-process flag at all --
            # a quarantined worker holds no lock, so that check alone would
            # keep reporting healthy while the app can no longer persist
            # anything). Once poisoned, every future call re-raises at the
            # guard above before ever reaching the success beat below, so
            # this "dead" report can never be silently overwritten back to
            # "alive" -- it only clears on process restart.
            liveness.beat("db_worker", STATE_DEAD)
            raise StuckExecutorError(
                f"{func_name} did not complete within "
                f"{_DB_TIMEOUT_SECONDS}s; SQLite backend quarantined."
            ) from None
        liveness.beat("db_worker", STATE_ALIVE)
        return result

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
        identity_source="",
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

        NOTE: This database runs in autocommit mode (isolation_level=None).
        Each statement is committed immediately. The `save` parameter is
        retained for API compatibility but has no effect on transaction
        boundaries -- all writes are durable immediately. True transaction
        batching is only available via `create_job_if_absent` which wraps
        its check-and-insert in an explicit BEGIN IMMEDIATE/COMMIT block.
        """

        filter_result = filter_result or {}

        columns = ", ".join(f'"{header}"' for header in JOB_HEADERS)
        placeholders = ", ".join("?" for _ in JOB_HEADERS)

        values = [
            datetime.now().isoformat(),
            job_uuid,
            job_id,
            source,
            identity_source,

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
            "",  # Classification Retry Not Before: unset at creation time.
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

    def claim_pending_classification(self, job_uuid, lease_until):
        """Atomically claim one durably-pending job for a classification
        attempt, so the FreeHub/source re-discovery path and the
        classification-retry worker can never both run an LLM
        arbitration call for the same job at the same time.

        This is a single conditional UPDATE executed on the dedicated
        DBLogger worker thread (see DBLogger.run()), which serializes
        every database access in this process. That serialization is
        what makes the read-eligibility-check-then-write atomic: two
        concurrent callers each attempt this same UPDATE, but SQLite
        applies them one at a time, so only the first can see the row
        still eligible (unclaimed) and flip it to "leased". The second
        call's WHERE clause no longer matches (the not-before it just
        wrote already moved into the future) and its rowcount is 0.

        Returns True if this call won the claim (the row was pending
        and its retry-not-before had already elapsed, or was never
        set), False if the row is not eligible yet or another caller
        already claimed it first.

        The claim is a lease, not a permanent lock: it reuses the
        existing "Classification Retry Not Before" backoff column and
        pushes it out to ``lease_until``. If the classification
        attempt finishes, the caller overwrites that column with the
        real backoff (on failure) or moves the row out of the
        "Pending" state entirely (on success), so the lease value
        never matters again. If the process crashes mid-attempt
        instead, the row simply stays claimed until ``lease_until``
        passes, then becomes eligible again -- bounding, rather than
        eliminating, the restart recovery window instead of leaving
        the row claimed forever.
        """
        cursor = self._conn.execute(
            "UPDATE jobs SET \"Classification Retry Not Before\" = ? "
            "WHERE \"Job UUID\" = ? AND \"Final Decision\" = 'Pending' "
            "AND (\"Classification Retry Not Before\" IS NULL "
            "OR \"Classification Retry Not Before\" = '' "
            "OR CAST(\"Classification Retry Not Before\" AS REAL) <= ?)",
            (str(lease_until), job_uuid, time.time()),
        )
        self.save()
        return cursor.rowcount == 1

    def get_incomplete_classification_jobs(self):
        """Return durably pending jobs whose classification needs retry
        and whose scheduled retry time (if any) has already passed.

        The schedule check mirrors the guard in
        app.job_processor.process_job (see the "Classification Retry
        Not Before" column comment in JOB_HEADERS): a job that failed
        classification very recently is intentionally excluded from
        this sweep until its backoff window elapses, rather than being
        retried on every classification_retry_loop tick regardless of
        how recently it last failed.
        """
        cursor = self._conn.execute(
            "SELECT * FROM jobs WHERE \"Final Decision\" = 'Pending' "
            "AND \"Decision Reason\" = 'LLM Error' "
            "AND (\"Classification Retry Not Before\" IS NULL "
            "OR \"Classification Retry Not Before\" = '' "
            "OR CAST(\"Classification Retry Not Before\" AS REAL) <= ?)",
            (time.time(),),
        )
        return [self._row_to_dict(cursor, row) for row in cursor.fetchall()]

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
        """Update one job row's fields.

        Field names must be valid (snake_case keys in COLUMN_MAP). An
        unknown field name is a programming error -- silently ignoring it
        would drop the intended write with no signal, so it raises loudly
        instead (see the "fail loudly" remediation). This catches typos and
        stale call sites at runtime rather than quietly corrupting the log.

        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """

        if not self.has_job(job_uuid):
            return False

        sets = []
        values = []

        for key, value in fields.items():

            if key not in COLUMN_MAP:
                raise ValueError(
                    f"update_job: unknown field {key!r} for job {job_uuid!r}. "
                    f"Known fields: {sorted(COLUMN_MAP)}"
                )

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
        provider="",
        save=True,
    ):
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT
        is committed immediately.
        """

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
                provider,
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
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT
        is committed immediately.
        """

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
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT
        is committed immediately.
        """

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
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT
        is committed immediately.
        """
        self._conn.execute(
            'INSERT OR IGNORE INTO categories '
            '("Category ID", "Name", "Description", "Enabled", "Created At") '
            'VALUES (?, ?, ?, ?, ?)',
            (category_id, name, description, "1" if enabled else "0",
             datetime.now().isoformat()),
        )
        if save:
            self.save()

    def _migrate_user_uniqueness(self):
        """Establish the DB-level guarantee that a Telegram User ID maps to
        at most one users row, and enforce it with a unique index.

        The app keeps users idempotent via ensure_user/ensure_channel_
        destination (lookup-before-insert) running on the single serialized
        logger thread, so new writes already cannot create duplicates. This
        migration closes the remaining gap for *legacy* data that may contain
        duplicates (from an older schema or a one-off mis-write) and then
        makes uniqueness a hard database invariant going forward.

        Dedup policy (runs once, then the index is created):
          * grouped by "Telegram User ID"
          * the row with the MAX rowid (the most-recently inserted) is kept
            as the surviving user; every older duplicate's subscription
            preferences (Categories/Sources) are merged into the survivor
            before the duplicate is removed.
          * The survivor's explicit Is Active state and Destination Type are
            authoritative and are NOT overridden by older duplicates -- this
            preserves the subscription lifecycle semantics where /stop on the
            newest record must not be undone by an older active duplicate.
          * pending/active user_notifications rows that referenced the
            duplicate "User ID" are re-pointed at the survivor so no queued
            notification is orphaned or duplicated by the merge. If the
            survivor already owns a row for the same job -- the
            ("Job UUID", "User ID") unique index forbids two -- the
            collision is resolved by delivery progress (see
            _USER_NOTIFICATION_STATUS_PRECEDENCE): the row that is further
            along (e.g. durably "Sent") is kept and the other deleted, so
            the dedup can never lose a real delivery or fail the migration
            from the unique-index violation of a naive re-point.
          * subscription_events rows are historical and left as-is.

        Idempotent and transactional: the dedup and index creation are one
        commit, so a crash mid-migration rolls back cleanly.
        """
        # The users table may not exist yet on a fresh database at the point
        # this is called from initialize(); CREATE TABLE runs first, so it
        # always exists here. Guard anyway for safety.
        if "users" not in self._table_names():
            return

        dup_rows = self._conn.execute(
            'SELECT "Telegram User ID", COUNT(*) AS c FROM users '
            'GROUP BY "Telegram User ID" HAVING c > 1'
        ).fetchall()

        dup_ids = [row[0] for row in dup_rows]

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for telegram_id in dup_ids:
                rows = self._conn.execute(
                    'SELECT rowid, "User ID", "Categories", "Sources", '
                    '"Is Active", "Destination Type" FROM users '
                    'WHERE "Telegram User ID" = ? ORDER BY rowid ASC',
                    (telegram_id,),
                ).fetchall()
                if not rows:
                    continue
                survivor_rowid, survivor_user_id, survivor_cats, survivor_sources, \
                    survivor_active, survivor_dst = rows[-1]
                for rowid, user_id, cats, sources, active, dst in rows[:-1]:
                    # Merge preferences into the survivor.
                    merged_cats = self._merge_csv(survivor_cats, cats)
                    merged_sources = self._merge_csv(survivor_sources, sources)
                    # The survivor (newest row by rowid) is the authoritative
                    # record. Its explicit Is Active state must NOT be
                    # overridden by an older duplicate's active state --
                    # that would silently reactivate a user whose newer
                    # state is inactive/stopped (audit finding: subscription
                    # lifecycle semantics). Only preferences (Categories,
                    # Sources) are merged; active state and destination
                    # type remain the survivor's.
                    active_value = survivor_active
                    self._conn.execute(
                        'UPDATE users SET "Categories" = ?, "Sources" = ?, '
                        '"Is Active" = ? WHERE "User ID" = ?',
                        (merged_cats, merged_sources, active_value, survivor_user_id),
                    )
                    # Re-point queued deliveries that targeted the duplicate
                    # user id so nothing is orphaned, one row at a time: a
                    # naive bulk re-point collides with a row the survivor
                    # already owns for the same job ("Job UUID" + "User ID"
                    # is unique), raising an IntegrityError that aborts the
                    # whole migration. Resolve each collision by delivery
                    # progress (Sent > Sending > Pending > RateLimited >
                    # Failed > Cancelled): the more-completed row survives,
                    # the less-completed one is removed, so the dedup can
                    # never lose a real delivery.
                    for notif_row in self._conn.execute(
                        'SELECT "Notification ID", "Job UUID", "Status" '
                        'FROM user_notifications WHERE "User ID" = ?',
                        (user_id,),
                    ).fetchall():
                        notif_id, job_uuid, dup_status = notif_row
                        survivor_notif = self._conn.execute(
                            'SELECT "Status" FROM user_notifications '
                            'WHERE "User ID" = ? AND "Job UUID" = ?',
                            (survivor_user_id, job_uuid),
                        ).fetchone()
                        if survivor_notif is None:
                            self._conn.execute(
                                'UPDATE user_notifications SET "User ID" = ?, '
                                '"Telegram User ID" = ? '
                                'WHERE "Notification ID" = ?',
                                (survivor_user_id, telegram_id, notif_id),
                            )
                            continue
                        dup_rank = _USER_NOTIFICATION_STATUS_PRECEDENCE.get(
                            str(dup_status or "").strip(), -1
                        )
                        survivor_rank = _USER_NOTIFICATION_STATUS_PRECEDENCE.get(
                            str(survivor_notif[0] or "").strip(), -1
                        )
                        if dup_rank > survivor_rank:
                            # The duplicate's own row is the further-along
                            # delivery (e.g. durably "Sent" while the
                            # survivor's is merely "Pending"): keep it in
                            # place of the survivor's row for this job.
                            # Delete the survivor's row first so the re-point
                            # does not violate the unique index.
                            self._conn.execute(
                                'DELETE FROM user_notifications '
                                'WHERE "User ID" = ? AND "Job UUID" = ?',
                                (survivor_user_id, job_uuid),
                            )
                            self._conn.execute(
                                'UPDATE user_notifications SET "User ID" = ?, '
                                '"Telegram User ID" = ? '
                                'WHERE "Notification ID" = ?',
                                (survivor_user_id, telegram_id, notif_id),
                            )
                        else:
                            # Survivor's row is equal-or-further-along (ties
                            # keep the survivor); the duplicate's lesser row
                            # for this job is dropped.
                            self._conn.execute(
                                'DELETE FROM user_notifications '
                                'WHERE "Notification ID" = ?',
                                (notif_id,),
                            )
                    # Remove the duplicate user row.
                    self._conn.execute(
                        'DELETE FROM users WHERE rowid = ?', (rowid,)
                    )
                    survivor_cats, survivor_sources, survivor_active = (
                        merged_cats, merged_sources, active_value
                    )

            # Enforce uniqueness going forward. A unique index cannot be
            # created if duplicates remain, so it runs inside the same
            # transaction after dedup.
            self._conn.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_user_id '
                'ON users ("Telegram User ID")'
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _merge_csv(*values):
        merged = set()
        for value in values:
            for item in (value or "").split(","):
                item = item.strip().lower()
                if item:
                    merged.add(item)
        return ",".join(sorted(merged))

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
        except sqlite3.OperationalError as exc:
            # A database that never had the legacy table (or already
            # migrated it away) has nothing to fold in. Only the
            # missing-table case is swallowed: a "database is locked" or
            # other operational failure must propagate rather than being
            # mistaken for "already migrated".
            if "no such table" not in str(exc):
                raise
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
        """Register a Telegram channel as a normal subscription destination.

        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT/UPDATE
        is committed immediately.
        """
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
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """
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

        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
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

        import re
        aliases = {profile.id: tuple(alias.casefold() for alias in profile.aliases) for profile in SOURCES}

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
            if normalized_source in aliases:
                # Job source is a canonical id. A subscriber must match if
                # they stored that canonical id OR any of its display aliases
                # (e.g. "mostaql" or "مستقل"). Symmetric with the alias
                # fallback loop below, which handles alias-form job sources.
                return any(
                    source_id in aliases[normalized_source] for source_id in selected
                )
            for source_id in selected:
                if source_id in aliases and any(
                    re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_source)
                    for alias in aliases[source_id]
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
            if category_matches(row[3])
            and source_matches(row[4])
            and (row[2] or "user") in {"user", "channel"}
        ]

    def queue_user_notifications(self, job_uuid, category_id, source="", save=True):
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each INSERT
        is committed immediately.
        """
        subscribers = self.get_category_subscribers(category_id, source)
        now = datetime.now().isoformat()
        queued = 0

        for subscriber in subscribers:
            cursor = self._conn.execute(
                'INSERT OR IGNORE INTO user_notifications '
                '("Notification ID", "Job UUID", "User ID", "Telegram User ID", '
                '"Category ID", "Status", "Claimed At", "Attempts", "Last Error", '
                '"Created At", "Updated At", "Next Attempt At") '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(uuid.uuid4()),
                    job_uuid,
                    str(subscriber["user_id"]),
                    str(subscriber["telegram_user_id"]),
                    category_id,
                    "Pending",
                    "",
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
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """
        self.set_destination_active(telegram_user_id, active, save=save)

    def set_destination_active(self, telegram_chat_id, active, save=True):
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """
        self._conn.execute(
            'UPDATE users SET "Is Active" = ?, "Updated At" = ? '
            'WHERE "Telegram User ID" = ?',
            ("1" if active else "0", datetime.now().isoformat(), str(telegram_chat_id)),
        )
        if save:
            self.save()

    def cancel_pending_user_notifications(self, telegram_user_id, save=True):
        """Discard this user's not-yet-delivered queue on /stop.

        /stop is treated as "opt out", not "pause and deliver later":
        a user who unsubscribes and resubscribes weeks or months later
        should not be hit with a burst of every job that was queued
        for them while they were inactive (audit finding: user
        subscription semantics). claim_pending_user_notifications()
        already excludes inactive users, so without this, those rows
        just sit as "Pending"/"Failed"/"RateLimited" indefinitely and
        would all become claimable again the moment /start flips
        "Is Active" back to 1 -- this call is what actually discards
        them instead of merely deferring them.

        Only touches still-outstanding rows; anything already "Sent"
        is history, not backlog, and is left alone.

        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """
        cursor = self._conn.execute(
            'UPDATE user_notifications SET "Status" = "Cancelled", '
            '"Claimed At" = "", '
            '"Updated At" = ? WHERE "Telegram User ID" = ? '
            'AND ("Status" = "Pending" OR "Status" = "Failed" '
            '     OR "Status" = "RateLimited")',
            (datetime.now().isoformat(), str(telegram_user_id)),
        )
        if save:
            self.save()
        return cursor.rowcount

    def reset_sending_user_notifications(self, save=True):
        """
        NOTE: This database runs in autocommit mode. The `save` parameter
        is retained for API compatibility but has no effect -- each UPDATE
        is committed immediately.
        """
        self._conn.execute(
            'UPDATE user_notifications SET "Status" = "Pending", '
            '"Next Attempt At" = ?, "Claimed At" = "" '
            'WHERE "Status" = "Sending"',
            (datetime.now().isoformat(),),
        )
        if save:
            self.save()

    def _recover_stale_sending_user_notifications(self, now=None):
        """Re-open deliveries left "Sending" by a process that died
        before finishing them, without charging an attempt.

        A row enters "Sending" only when claim_pending_user_
        notifications stamps its lease immediately before delivery. If
        the claiming process is killed mid-delivery the row would
        otherwise sit in "Sending" forever -- and before the lease was
        introduced, every restart also charged one attempt for a
        delivery that never actually happened (see the audit finding:
        crashes during delivery exhausted a notification's whole
        attempt budget without a single real send, because
        reset_sending_user_notifications re-opened rows whose Attempts
        the claim had already incremented). Now a "Sending" row whose
        lease is empty, or older than
        user_notification_claim_lease_seconds, is returned to
        "Pending" with its Attempts count untouched. Runs at the top
        of every claim, so a dead claim gets re-opened as soon as the
        next poll tick arrives.
        """
        now = now or datetime.now().isoformat()
        cutoff = (
            datetime.now() - timedelta(
                seconds=float(self.user_notification_claim_lease_seconds)
            )
        ).isoformat()
        self._conn.execute(
            'UPDATE user_notifications SET "Status" = "Pending", '
            '"Next Attempt At" = ?, "Claimed At" = "", "Updated At" = ? '
            'WHERE "Status" = "Sending" '
            'AND ("Claimed At" IS NULL OR "Claimed At" = "" '
            '     OR "Claimed At" <= ?)',
            (now, now, cutoff),
        )

    def claim_pending_user_notifications(self, limit=20):
        """Claim a batch for delivery. Eligibility selection and the
        Pending->Sending ownership transition happen inside ONE explicit
        transaction (BEGIN IMMEDIATE), so the acquired claim can never
        be observed separately from the eligibility decision that
        produced it -- even by a concurrent reader on the serialized
        worker thread.

        Claiming stamps a delivery lease ("Claimed At") and marks the
        row "Sending", but does NOT spend an attempt. An attempt is
        only spent by a real (non-RetryAfter) delivery failure in
        _send_one, so a crash between claim and delivery can never eat
        into a notification's failure budget. Leftover "Sending"
        leases are re-opened without charge by
        _recover_stale_sending_user_notifications before the batch is
        selected.

        Single-process scope: the transaction guarantees atomicity
        within the one bot process. Multi-process claim coordination is
        out of scope by design (see the module docstring).
        """
        now = datetime.now().isoformat()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._recover_stale_sending_user_notifications(now)
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
                'AND (CAST(un."Attempts" AS INTEGER) < ? '
                '     OR un."Status" = "RateLimited") '
                'AND (un."Next Attempt At" IS NULL OR un."Next Attempt At" = "" '
                'OR un."Next Attempt At" <= ?) '
                'ORDER BY un.rowid LIMIT ?',
                (int(self.max_user_notification_attempts), now, int(limit)),
            )
            rows = [self._row_to_dict(cursor, row) for row in cursor.fetchall()]

            for row in rows:
                self._conn.execute(
                    'UPDATE user_notifications SET "Status" = "Sending", '
                    '"Claimed At" = ?, "Updated At" = ? '
                    'WHERE "Notification ID" = ? AND '
                    '("Status" = "Pending" OR "Status" = "Failed" '
                    ' OR "Status" = "RateLimited")',
                    (now, now, row["Notification ID"]),
                )
                row["Status"] = "Sending"
                row["Claimed At"] = now
                row["Updated At"] = now

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
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
        sets = ['"Status" = ?', '"Updated At" = ?', '"Claimed At" = ?']
        values = [status, datetime.now().isoformat(), ""]
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
