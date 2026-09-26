"""Postgres persistence adapter (SQLAlchemy Core).

Replaces the old SQLite/DBLogger backend for this node. Scope is
narrower than the old backend: everything downstream of a guard
"notify" decision (subscriber fan-out, delivery, delivery retry) AND
all user/subscription-profile management (Telegram command surface,
categories/sources preferences) moved out to separate services -- this
repository only ever touches `jobs`, `categories` (the job-category
catalog), `notification_guard`, and the append-only `gemini`/`errors`
logs. See app.adapters.streams.redis_publisher for the Redis stream
this node publishes guard-approved jobs to.

Uses a real connection pool (SQLAlchemy's default QueuePool) and a
matching multi-worker thread pool, unlike the old SQLite backend's
single-writer-thread design -- Postgres supports genuine concurrent
writers, so there is no reason to serialize every call through one
thread the way the old SQLite backend had to. Check-then-act sequences
that used to rely on that app-level serialization for atomicity
(create_job_if_absent, claim_pending_classification) now rely on
Postgres's own constraints/row-locking instead -- see each method's
docstring for exactly how.
"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.ports import JobRepository
from app.infra.runtime_config import RUNTIME
from app.infra.heartbeat import liveness, STATE_ALIVE, STATE_DEAD

# snake_case keyword -> SQL column name, mirroring the old DBLogger's
# COLUMN_MAP so update_job()'s field-name contract is unchanged.
COLUMN_MAP = {
    "timestamp": "Timestamp",
    "job_uuid": "Job UUID",
    "job_id": "Job ID",
    "source": "Source",
    "identity_source": "Identity Source",
    "title": "Title",
    "description": "Description",
    "raw_message": "Raw Message",
    "company": "Company",
    "url": "URL",
    "short_url": "Short URL",
    "decision": "Decision",
    "decision_reason": "Decision Reason",
    "categories": "Categories",
    "negative_categories": "Negative Categories",
    "has_core_positive": "Has Core Positive",
    "has_core_negative": "Has Core Negative",
    "core_positive_hit_count": "Core Positive Hit Count",
    "supporting_positive_weight": "Supporting Positive Weight",
    "supporting_negative_weight": "Supporting Negative Weight",
    "hard_reject": "Hard Reject",
    "notify_directly": "Notify Directly",
    "needs_gemini": "Needs Gemini",
    "gemini_decision": "Gemini Decision",
    "notification_status": "Notification Status",
    "final_decision": "Final Decision",
    "category_id": "Category ID",
    "category_selection_method": "Category Selection Method",
    "filter_time_ms": "Filter Time (ms)",
    "classification_retry_not_before": "Classification Retry Not Before",
}

# Columns dropped from the fresh Postgres schema (see
# migrations/postgres/0001_init.sql) because nothing ever read them back
# on the SQLite backend: Filter Text, Category Candidates, Hard Reject
# Matches, Title Core Positive/Negative, Core/Supporting Positive/
# Negative Matches. Any filter_result key matching one of those is
# silently dropped rather than raising, so app.domain.filters can keep
# producing them without this adapter needing to change.
_DROPPED_FILTER_KEYS = {
    "category_candidates",
    "hard_reject_matches",
    "title_core_positive",
    "title_core_negative",
    "positive_core_matches",
    "positive_supporting_matches",
    "negative_core_matches",
    "negative_supporting_matches",
}

# A real connection pool, not single-threaded serialization: Postgres
# supports concurrent writers, so multiple worker threads may each hold
# their own pooled connection and run queries in parallel. Sized to
# match the SQLAlchemy engine's pool (see PostgresRepository.__init__).
# Check-then-act sequences that used to rely on app-level serialization
# (create_job_if_absent) now rely on Postgres's own constraints/locking
# instead -- see that method's docstring.
_POOL_SIZE = 10
_EXECUTOR = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="pg-repo")

# Every repository call is bounded by the same "database operation must
# return in this long, or the backend is treated as stuck" bound the old
# SQLite backend applied to its single writer thread (see the original
# app/services/logger.py: asyncio.wait_for over run_in_executor, then the
# executor is quarantined and all later calls fail closed). The Postgres
# adapter dropped that protection in the refactor: a hung sync call left
# the caller's await hanging forever while app.infra.heartbeat kept
# ticking, so the container reported healthy while ingestion stalled
# (audit finding: database timeout / quarantine dropped). Restored here,
# on top of the new server-side/http-level bounds below.
_DATABASE_TIMEOUT_SECONDS = RUNTIME.database_timeout_seconds

# libpq connect timeout used when establishing NEW pooled connections.
# Matches app.infra.healthcheck's own probe bound: a Postgres that does
# not answer the TCP/startup handshake within a couple of seconds is not
# reachable and must fail fast rather than hang a worker thread.
_CONNECT_TIMEOUT_SECONDS = 2

# Recycle pooled connections on an hourly cadence: belt-and-braces over
# pool_pre_ping for stale connections dropped by idle-timers/load
# balancers on long-lived deployments.
_POOL_RECYCLE_SECONDS = 1800


def _statement_timeout_option(database_timeout_seconds: int) -> str:
    """Server-side statement bound, in milliseconds, as a libpq `options`
    string. Guarantees Postgres itself aborts a statement that exceeds the
    app-level bound instead of leaving the executor thread running it
    forever (a caller-side timeout alone would cancel the await but not
    the thread)."""
    return f"-c statement_timeout={max(1, int(database_timeout_seconds * 1000))}"


class StuckExecutorError(RuntimeError):
    """Raised when a Postgres repository call did not return within
    _DATABASE_TIMEOUT_SECONDS. See PostgresRepository.run for why.

    The stuck worker thread cannot be forcibly killed (and may still be
    executing a statement server-side, or hold a checked-out pooled
    connection), so the repository is QUARANTINED rather than replaced:
    every subsequent run() call fails closed with this exception until
    the process restarts. A fresh executor/connection pool is deliberately
    NOT installed -- it could let a second worker touch the same rows as
    the still-stuck one. The failing call is loud (its caller's existing
    per-item exception handling surfaces it), the healthcheck sees the
    "db_worker" liveness go dead, and a restart is the only recovery."""


def _now():
    return datetime.now(timezone.utc)


def _as_uuid_or_none(value):
    """Coerce a value for a uuid-typed column. Callers sometimes pass
    legacy string identifiers (e.g. "source:internal_id" or a job URL)
    as the Job UUID; never let that crash the error-log insert itself,
    since the whole point of the errors table is to record failures."""
    if not value:
        return None
    if not isinstance(value, str):
        return value
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def _with_psycopg_driver(database_url: str) -> str:
    """Force the psycopg (v3) dialect; SQLAlchemy defaults a bare
    postgresql:// URL to psycopg2, which isn't one of this project's
    dependencies (see requirements.txt: psycopg[binary])."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


# Postgres enforces types the legacy SQLite backend tolerated silently.
# The old columns were loosely-typed (SQLite stored a 123.45 timer float,
# an epoch-as-string "1754322000.0", or an empty-string category with no
# complaint), while this schema declares INTEGER / TIMESTAMPTZ / FK. The
# production values below arrive in the exact legacy shapes, so they are
# normalized at the adapter boundary once, where the whole pipeline can
# rely on it, instead of at each call site.
_INT_BACKFILL_COLUMNS = {
    "filter_time_ms",
    "core_positive_hit_count",
    "response_time_ms",
    "prompt_tokens",
    "completion_tokens",
}


def _as_int_or_none(value):
    """Coerce to int for Postgres INTEGER columns. Tolerantly returns
    None for absent/garbage input so a display/audit column can never
    crash the job pipeline (float 123.45 -> 123, '' -> None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_timestamp(value):
    """Coerce to a tz-aware datetime for Postgres TIMESTAMPTZ columns.

    Accepts a datetime, a real number, a numeric epoch string (what
    app.services.job_processor historically stored), or an ISO string.
    None and unparseable input return None so a retry/backoff column
    can never crash the pipeline.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return _from_epoch(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return _from_epoch(stripped)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalize_update_value(key, value):
    """Normalize one update_job() field against the schema it lands in."""
    if key == "category_id" and value == "":
        # "no category" must be NULL, never a foreign-key-violating ''.
        return None
    if key in _INT_BACKFILL_COLUMNS:
        return _as_int_or_none(value)
    if key == "classification_retry_not_before":
        return _normalize_timestamp(value)
    return value


def _load_migration_runner():
    """Import scripts/migrate_postgres.py by file path.

    The repository that owns schema bootstrap reuses the exact runner
    operators/docs reference, without gambling that ``scripts`` is on
    sys.path in every run mode (stale PYTHONPATH, pytest without rootdir
    insertion, etc.). Loading by file location keeps one implementation
    regardless of how the process was launched.
    """
    import importlib.util

    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts" / "migrate_postgres.py"
    )
    spec = importlib.util.spec_from_file_location(
        "app_migrate_postgres", str(script_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PostgresRepository(JobRepository):
    id = "postgres"

    def __init__(
        self,
        database_url: str,
        database_timeout_seconds: int | None = None,
    ):
        self._database_url = database_url
        # Bounded detection with REAL bounds, not a fake caller-side
        # cancel: the connect timeout caps socket/startup handshakes, the
        # server-side statement_timeout makes Postgres itself abort a
        # statement that overruns (so the worker thread actually
        # terminates), pool_timeout caps how long a call waits for a free
        # pooled connection, and run() additionally bounds the whole call
        # with asyncio.wait_for -- see run() for the quarantine semantics.
        self._database_timeout_seconds = (
            _DATABASE_TIMEOUT_SECONDS
            if database_timeout_seconds is None
            else database_timeout_seconds
        )
        self._quarantined = False
        self._engine = create_engine(
            _with_psycopg_driver(database_url),
            pool_pre_ping=True,
            pool_size=_POOL_SIZE,
            max_overflow=0,
            pool_timeout=self._database_timeout_seconds,
            pool_recycle=_POOL_RECYCLE_SECONDS,
            connect_args={
                "connect_timeout": _CONNECT_TIMEOUT_SECONDS,
                "options": _statement_timeout_option(self._database_timeout_seconds),
            },
        )

    async def run(self, func, *args, **kwargs):
        """Dispatch one blocking repository call to the worker pool with a
        hard wall-clock bound (mirrors app.services.state.StateManager.run).

        On timeout the repository is QUARANTINED, exactly like the old
        SQLite backend and the state backend: the stuck thread cannot be
        killed (it may still hold a pooled connection / in-flight server
        statement), so the repository fails closed with StuckExecutorError
        on this and every later call, the "db_worker" liveness state goes
        dead for the healthcheck, and restart (a fresh executor/pool) is
        the only recovery. A hung DB call therefore surfaces loudly
        instead of silently stalling the source workers while the
        heartbeat keeps beating.
        """
        if self._quarantined:
            raise StuckExecutorError(
                "Postgres repository is quarantined after a timed-out "
                "operation; restart the process before reusing the "
                "database."
            )

        loop = asyncio.get_running_loop()

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs)),
                timeout=self._database_timeout_seconds,
            )
        except asyncio.TimeoutError:
            # wait_for cancels the Future, not the running thread (the
            # statement may still be executing server-side once
            # statement_timeout lets the thread unwind). Quarantine
            # rather than replacing the pool: a second writer could
            # otherwise touch the same rows concurrently with the
            # still-stuck call. Restart is the only safe recovery.
            self._quarantined = True
            func_name = getattr(func, "__name__", repr(func))
            print(
                f"[DB] {func_name} timed out after "
                f"{self._database_timeout_seconds}s. The worker thread "
                "cannot be killed safely, so the Postgres repository is "
                "quarantined until process restart."
            )
            # Same reporting seam as app.services.state.StateManager.run:
            # once quarantined every future call re-raises at the guard
            # above before reaching the success beat below, so this dead
            # state can never be silently overwritten back to "alive".
            liveness.beat("db_worker", STATE_DEAD)
            raise StuckExecutorError(
                f"{func_name} did not complete within "
                f"{self._database_timeout_seconds}s; database backend "
                "quarantined."
            ) from None
        liveness.beat("db_worker", STATE_ALIVE)
        return result

    def dispose(self):
        """Release pooled connections at shutdown. Deliberately NO-OPs when
        the repository is quarantined: a stuck worker thread may still
        hold a checked-out connection and an in-flight statement, so
        disposing the pool under it is unsafe -- mirror the old SQLite
        backend's deliberate leak-on-quarantine (process exit reclaims
        everything instead of shutdown deadlocking on a stuck thread)."""
        if self._quarantined:
            return
        self._engine.dispose()

    # Thin async dispatch surface -- every JobRepository method call runs
    # its blocking SQLAlchemy body (the _sync_* methods below) on the
    # single dedicated worker thread, mirroring the old SQLiteRepository/
    # DBLogger.run() pattern so check-then-act sequences stay atomic
    # within this process.
    def initialize(self, *a, **kw): return self.run(self._sync_initialize, *a, **kw)
    def save(self, *a, **kw): return self.run(self._sync_save, *a, **kw)
    def has_job(self, *a, **kw): return self.run(self._sync_has_job, *a, **kw)
    def get_job(self, *a, **kw): return self.run(self._sync_get_job, *a, **kw)
    def create_job_if_absent(self, *a, **kw): return self.run(self._sync_create_job_if_absent, *a, **kw)
    def update_job(self, *a, **kw): return self.run(self._sync_update_job, *a, **kw)
    def claim_pending_classification(self, *a, **kw): return self.run(self._sync_claim_pending_classification, *a, **kw)
    def get_incomplete_classification_jobs(self, *a, **kw): return self.run(self._sync_get_incomplete_classification_jobs, *a, **kw)
    def get_incomplete_notification_jobs(self, *a, **kw): return self.run(self._sync_get_incomplete_notification_jobs, *a, **kw)
    def log_gemini(self, *a, **kw): return self.run(self._sync_log_gemini, *a, **kw)
    def log_error(self, *a, **kw): return self.run(self._sync_log_error, *a, **kw)
    def get_latest_guard_decision(self, *a, **kw): return self.run(self._sync_get_latest_guard_decision, *a, **kw)
    def get_latest_guard_decision_with_category(self, *a, **kw): return self.run(self._sync_get_latest_guard_decision_with_category, *a, **kw)
    def log_notification_guard(self, *a, **kw): return self.run(self._sync_log_notification_guard, *a, **kw)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _sync_initialize(self):
        """Apply pending versioned migrations, then seed `categories`.

        Migrations run first (idempotent -- schema_migrations records
        each applied file), restoring the on-boot schema maintenance the
        old SQLite DBLogger did. Schema must exist before even the
        category seed: Postgres raises UndefinedTable otherwise, which
        is exactly what a fresh production database would hit today if
        scripts/migrate_postgres.py was never run by hand.

        Jobs and user_notifications both carry a FOREIGN KEY on Category
        ID, so seeding must run before anything else writes a job.
        """
        self._run_migrations()

        from app.domain.categories.registry import enabled_categories

        with self._engine.begin() as conn:
            for profile in enabled_categories():
                conn.execute(
                    text(
                        'INSERT INTO categories ("Category ID", "Name", "Description", "Enabled", "Created At") '
                        'VALUES (:id, :name, :description, TRUE, :now) '
                        'ON CONFLICT ("Category ID") DO NOTHING'
                    ),
                    {
                        "id": profile.id, "name": profile.name,
                        "description": getattr(profile, "description", "") or "",
                        "now": _now(),
                    },
                )

    def _run_migrations(self):
        """Apply pending versioned migrations (see
        scripts/migrate_postgres.py) before the repository touches any
        schema. Idempotent, so it is safe on every boot."""
        runner = _load_migration_runner()
        runner.run(self._database_url)

    def _sync_save(self):
        """No-op: every method below commits immediately. Kept for
        JobRepository API compatibility with call sites that still pass
        save=True/False."""
        return None

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _sync_has_job(self, job_uuid) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                text('SELECT 1 FROM jobs WHERE "Job UUID" = :job_uuid'),
                {"job_uuid": job_uuid},
            ).fetchone()
        return row is not None

    def _sync_get_job(self, job_uuid):
        with self._engine.connect() as conn:
            row = conn.execute(
                text('SELECT * FROM jobs WHERE "Job UUID" = :job_uuid'),
                {"job_uuid": job_uuid},
            ).mappings().fetchone()
        return dict(row) if row is not None else None

    def _sync_create_job_if_absent(self, *, legacy_job_uuid=None, save=False, **kwargs):
        """Insert-or-report-duplicate, safe under real concurrent writers.

        With a real connection pool, two threads can both pass a
        pre-check SELECT before either commits -- unlike the old SQLite
        backend, where one dedicated worker thread made that race
        impossible by construction. Correctness here instead comes from
        the "Job UUID" PRIMARY KEY: the second concurrent insert for the
        same job_uuid raises IntegrityError, which is caught and
        reported as `False` (already exists), exactly like the old
        check-then-act return value -- just enforced by Postgres itself
        instead of app-level serialization.

        `legacy_job_uuid` is accepted for call-site compatibility with
        job_processor.py but is otherwise unused -- it only mattered for
        the SQLite backend's pre-stable-identity compatibility rows,
        which a fresh Postgres database has none of.
        """
        job_uuid = kwargs["job_uuid"]
        filter_result = kwargs.pop("filter_result", None) or {}

        params = {
            "Timestamp": _now(),
            "Job UUID": job_uuid,
            "Job ID": kwargs.get("job_id", ""),
            "Source": kwargs.get("source", ""),
            "Identity Source": kwargs.get("identity_source", ""),
            "Title": kwargs.get("title", ""),
            "Description": kwargs.get("description", ""),
            "Raw Message": kwargs.get("raw_message", ""),
            "Company": kwargs.get("company", ""),
            "URL": kwargs.get("url", ""),
            "Short URL": "",
            "Decision": filter_result.get("decision", ""),
            "Decision Reason": filter_result.get("reason", ""),
            "Categories": ", ".join(filter_result.get("categories", []) or []),
            "Negative Categories": ", ".join(filter_result.get("negative_categories", []) or []),
            "Has Core Positive": bool(filter_result.get("has_core_positive", False)),
            "Has Core Negative": bool(filter_result.get("has_core_negative", False)),
            "Core Positive Hit Count": _as_int_or_none(filter_result.get("core_positive_hit_count", 0)),
            "Supporting Positive Weight": filter_result.get("supporting_positive_weight", 0),
            "Supporting Negative Weight": filter_result.get("supporting_negative_weight", 0),
            "Hard Reject": bool(filter_result.get("hard_reject", False)),
            "Notify Directly": bool(filter_result.get("notify_directly", False)),
            "Needs Gemini": bool(filter_result.get("needs_gemini", False)),
            "Gemini Decision": "",
            "Notification Status": "",
            "Final Decision": "",
            "Category ID": filter_result.get("category_id") or None,
            "Category Selection Method": filter_result.get("category_selection_method", ""),
            "Filter Time (ms)": _as_int_or_none(kwargs.get("filter_time_ms")),
            "Classification Retry Not Before": None,
        }
        columns = ", ".join(f'"{k}"' for k in params)
        placeholders = ", ".join(f":{_bind(k)}" for k in params)
        bind_params = {_bind(k): v for k, v in params.items()}

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f'INSERT INTO jobs ({columns}) VALUES ({placeholders})'),
                    bind_params,
                )
        except IntegrityError as exc:
            # Only the "Job UUID" PRIMARY KEY collision means "already
            # exists" (the concurrent-insert race this method exists to
            # handle). Any other integrity violation (a bad FK on
            # Category ID, a NOT NULL violation, etc.) is a real data
            # error and must not be silently reported as a duplicate.
            if "jobs_pkey" not in str(exc.orig):
                raise
            return False
        return True

    def _sync_update_job(self, job_uuid, save=True, **fields):
        sets = []
        params = {"job_uuid": job_uuid}
        for key, value in fields.items():
            if key in _DROPPED_FILTER_KEYS:
                continue
            if key not in COLUMN_MAP:
                raise ValueError(
                    f"update_job: unknown field {key!r} for job {job_uuid!r}. "
                    f"Known fields: {sorted(COLUMN_MAP)}"
                )
            if isinstance(value, list):
                value = ", ".join(value)
            value = _normalize_update_value(key, value)
            bind = _bind(COLUMN_MAP[key])
            sets.append(f'"{COLUMN_MAP[key]}" = :{bind}')
            params[bind] = value

        if not sets:
            return True

        with self._engine.begin() as conn:
            result = conn.execute(
                text(f'UPDATE jobs SET {", ".join(sets)} WHERE "Job UUID" = :job_uuid'),
                params,
            )
            return result.rowcount > 0

    def _sync_claim_pending_classification(self, job_uuid, lease_until):
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    'UPDATE jobs SET "Classification Retry Not Before" = :lease '
                    'WHERE "Job UUID" = :job_uuid AND "Final Decision" = \'Pending\' '
                    'AND ("Classification Retry Not Before" IS NULL '
                    'OR "Classification Retry Not Before" <= :now)'
                ),
                {"lease": _from_epoch(lease_until), "job_uuid": job_uuid, "now": _now()},
            )
            return result.rowcount == 1

    def _sync_get_incomplete_classification_jobs(self):
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    'SELECT * FROM jobs WHERE "Final Decision" = \'Pending\' '
                    'AND ("Decision Reason" = \'LLM Error\' '
                    'OR "Decision Reason" LIKE \'Guard Fallback%\') '
                    'AND ("Classification Retry Not Before" IS NULL '
                    'OR "Classification Retry Not Before" <= :now)'
                ),
                {"now": _now()},
            ).mappings().fetchall()
        return [dict(row) for row in rows]

    def _sync_get_incomplete_notification_jobs(self):
        """Jobs whose post-classification workflow (guard decision +
        stream publish) is durably marked started but not yet
        "Complete"/"Suppressed". Reused, unchanged in meaning, as the
        crash-safety resweep: a job only reaches "Complete" after
        JobStreamPublisher.publish() succeeds (see job_processor.py), so
        a row stuck here after a crash between DB-commit and XADD is
        exactly what this resweep picks back up and republishes."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    'SELECT * FROM jobs WHERE "Notification Status" IS NOT NULL '
                    'AND "Notification Status" != \'\' '
                    'AND "Notification Status" != \'Complete\' '
                    'AND "Notification Status" != \'Suppressed\''
                )
            ).mappings().fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Append-only logs
    # ------------------------------------------------------------------

    def _sync_log_gemini(self, job_uuid, decision_before, reason_before, prompt_tokens,
                    completion_tokens, response_time_ms, decision, confidence,
                    provider="", save=True):
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    'INSERT INTO gemini '
                    '("Timestamp", "Job UUID", "Decision Before", "Reason Before", '
                    '"Prompt Tokens", "Completion Tokens", "Response Time (ms)", '
                    '"Decision", "Confidence", "Provider") '
                    'VALUES (:ts, :job_uuid, :decision_before, :reason_before, '
                    ':prompt_tokens, :completion_tokens, :response_time_ms, '
                    ':decision, :confidence, :provider)'
                ),
                {
                    "ts": _now(), "job_uuid": job_uuid,
                    "decision_before": decision_before, "reason_before": reason_before,
                    "prompt_tokens": _as_int_or_none(prompt_tokens),
                    "completion_tokens": _as_int_or_none(completion_tokens),
                    "response_time_ms": _as_int_or_none(response_time_ms),
                    "decision": decision, "confidence": str(confidence),
                    "provider": provider,
                },
            )

    def _sync_log_error(self, module, error, job_uuid="", save=True):
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    'INSERT INTO errors ("Timestamp", "Job UUID", "Module", "Error") '
                    'VALUES (:ts, :job_uuid, :module, :error)'
                ),
                {"ts": _now(), "job_uuid": _as_uuid_or_none(job_uuid), "module": module, "error": str(error)},
            )

    def _sync_get_latest_guard_decision(self, job_uuid):
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    'SELECT "Guard Decision" FROM notification_guard '
                    'WHERE "Job UUID" = :job_uuid ORDER BY id DESC LIMIT 1'
                ),
                {"job_uuid": job_uuid},
            ).fetchone()
        return row[0] if row else None

    def _sync_get_latest_guard_decision_with_category(self, job_uuid):
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    'SELECT "Guard Decision", "Guard Category" FROM notification_guard '
                    'WHERE "Job UUID" = :job_uuid ORDER BY id DESC LIMIT 1'
                ),
                {"job_uuid": job_uuid},
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _sync_log_notification_guard(self, job_uuid, source, title, original_decision,
                                guard_decision, provider, model, response_time_ms,
                                error="", guard_category="", save=True):
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    'INSERT INTO notification_guard '
                    '("Timestamp", "Job UUID", "Source", "Title", "Original Decision", '
                    '"Guard Decision", "Provider", "Model", "Response Time (ms)", '
                    '"Error", "Guard Category") '
                    'VALUES (:ts, :job_uuid, :source, :title, :original_decision, '
                    ':guard_decision, :provider, :model, :response_time_ms, '
                    ':error, :guard_category)'
                ),
                {
                    "ts": _now(), "job_uuid": job_uuid, "source": source, "title": title,
                    "original_decision": original_decision, "guard_decision": guard_decision,
                    "provider": provider, "model": model,
                    "response_time_ms": _as_int_or_none(response_time_ms),
                    "error": error, "guard_category": guard_category,
                },
            )



def _bind(column_name: str) -> str:
    """SQLAlchemy bind-parameter names can't contain spaces/parens."""
    return "b_" + "".join(ch if ch.isalnum() else "_" for ch in column_name)


def _from_epoch(epoch_seconds) -> datetime:
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
