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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import asyncio

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.ports import JobRepository

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


def _now():
    return datetime.now(timezone.utc)


def _with_psycopg_driver(database_url: str) -> str:
    """Force the psycopg (v3) dialect; SQLAlchemy defaults a bare
    postgresql:// URL to psycopg2, which isn't one of this project's
    dependencies (see requirements.txt: psycopg[binary])."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    return database_url


class PostgresRepository(JobRepository):
    id = "postgres"

    def __init__(self, database_url: str):
        self._engine = create_engine(
            _with_psycopg_driver(database_url),
            pool_pre_ping=True,
            pool_size=_POOL_SIZE,
            max_overflow=0,
        )

    async def run(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs))

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
        """Schema is owned by scripts/migrate_postgres.py, not this
        adapter. Seeds `categories` from the enabled category profiles on
        every startup (mirroring the old DBLogger.initialize()) -- jobs
        and user_notifications both carry a FOREIGN KEY on Category ID,
        so this must run before anything else writes a job."""
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
            "Core Positive Hit Count": filter_result.get("core_positive_hit_count", 0),
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
            "Filter Time (ms)": kwargs.get("filter_time_ms"),
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
                    'AND "Decision Reason" = \'LLM Error\' '
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
                    "prompt_tokens": prompt_tokens or None,
                    "completion_tokens": completion_tokens or None,
                    "response_time_ms": response_time_ms or None,
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
                {"ts": _now(), "job_uuid": job_uuid or None, "module": module, "error": str(error)},
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
                    "response_time_ms": response_time_ms or None,
                    "error": error, "guard_category": guard_category,
                },
            )



def _bind(column_name: str) -> str:
    """SQLAlchemy bind-parameter names can't contain spaces/parens."""
    return "b_" + "".join(ch if ch.isalnum() else "_" for ch in column_name)


def _from_epoch(epoch_seconds) -> datetime:
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
