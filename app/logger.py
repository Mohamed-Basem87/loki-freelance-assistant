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
)


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

        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        # Autocommit: every statement is its own transaction. Explicit
        # BEGIN IMMEDIATE/COMMIT is still used where atomicity across
        # multiple statements matters (create_job_if_absent).
        self._conn.isolation_level = None

        for ddl in _CREATE_TABLES:
            self._conn.execute(ddl)

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
