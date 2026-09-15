"""Shared hermetic fakes for the Business Node integration tests.

These stand in for the real Postgres backend, Redis stream, guard and
shortener so `process_job()` / retry-sweep / recovery tests run fast
and deterministically without a database -- the same approach the rest
of the suite uses (e.g. the `_FakeRepo` in test_guard_error_retry_
integration.py). The one deliberate difference is that every repository
call dispatches through a `_roll` seam that yields to the event loop
once, mimicking the real PostgresRepository.run()->worker-thread
round-trip. That makes concurrent callers (two process_job() calls,
a live process_job() versus retry_incomplete_notifications()) actually
interleave like they do against the real backend instead of silently
serializing because the fake body completes without ever suspending.

Field semantics mirror app.adapters.repositories.postgres exactly:
create_job_if_absent flattens `filter_result` onto the header-case job
schema, update_job() translates snake_case keywords via COLUMN_MAP and
normalizes values for the typed Postgres columns, and claim_pending_
classification() honors the "Final Decision"/lease conditional the
SQL adapter enforces with a real UPDATE.
"""
import asyncio
from datetime import datetime, timezone

from app.adapters.repositories.postgres import (
    COLUMN_MAP,
    _DROPPED_FILTER_KEYS,
    _as_int_or_none,
    _normalize_update_value,
)


def _row_base(uuid, **overrides):
    row = {
        "Job UUID": uuid,
        "Job ID": "",
        "Identity Source": "",
        "Source": "",
        "Title": "",
        "Description": "",
        "Raw Message": "",
        "Company": "",
        "URL": "",
        "Short URL": "",
        "Decision": "",
        "Decision Reason": "",
        "Categories": "",
        "Negative Categories": "",
        "Has Core Positive": False,
        "Has Core Negative": False,
        "Core Positive Hit Count": 0,
        "Supporting Positive Weight": 0,
        "Supporting Negative Weight": 0,
        "Hard Reject": False,
        "Notify Directly": False,
        "Needs Gemini": False,
        "Gemini Decision": "",
        "Notification Status": "",
        "Final Decision": "",
        "Category ID": None,
        "Category Selection Method": "",
        "Filter Time (ms)": 0,
        "Classification Retry Not Before": None,
    }
    row.update(overrides)
    return row


class MemRepository:
    """In-memory JobRepository with the Postgres adapter's semantics."""

    def __init__(self, yields=True):
        self.rows = {}
        self.decisions = {}
        self.errors = []
        self._yields = yields

    async def _roll(self):
        if self._yields:
            await asyncio.sleep(0)

    # ------------------------------------------------------------------
    # JobRepository surface
    # ------------------------------------------------------------------

    async def initialize(self, *a, **kw):
        await self._roll()
        return None

    async def save(self, *a, **kw):
        await self._roll()
        return None

    async def has_job(self, job_uuid):
        await self._roll()
        return job_uuid in self.rows

    async def get_job(self, job_uuid):
        await self._roll()
        row = self.rows.get(job_uuid)
        return dict(row) if row is not None else None

    async def create_job_if_absent(self, **kwargs):
        await self._roll()
        job_uuid = kwargs["job_uuid"]
        if job_uuid in self.rows:
            return False

        filter_result = kwargs.pop("filter_result", None) or {}
        row = _row_base(
            job_uuid,
            **{
                "Job ID": kwargs.get("job_id", ""),
                "Identity Source": kwargs.get("identity_source", ""),
                "Source": kwargs.get("source", ""),
                "Title": kwargs.get("title", ""),
                "Description": kwargs.get("description", ""),
                "Raw Message": kwargs.get("raw_message", ""),
                "Company": kwargs.get("company", ""),
                "URL": kwargs.get("url", ""),
                "Decision": filter_result.get("decision", ""),
                "Decision Reason": filter_result.get("reason", ""),
                "Categories": ", ".join(filter_result.get("categories", []) or []),
                "Negative Categories": ", ".join(
                    filter_result.get("negative_categories", []) or []
                ),
                "Has Core Positive": bool(filter_result.get("has_core_positive", False)),
                "Has Core Negative": bool(filter_result.get("has_core_negative", False)),
                "Core Positive Hit Count": _as_int_or_none(
                    filter_result.get("core_positive_hit_count", 0)
                ),
                "Supporting Positive Weight": filter_result.get(
                    "supporting_positive_weight", 0
                ),
                "Supporting Negative Weight": filter_result.get(
                    "supporting_negative_weight", 0
                ),
                "Hard Reject": bool(filter_result.get("hard_reject", False)),
                "Notify Directly": bool(filter_result.get("notify_directly", False)),
                "Needs Gemini": bool(filter_result.get("needs_gemini", False)),
                "Category ID": filter_result.get("category_id") or None,
                "Category Selection Method": filter_result.get(
                    "category_selection_method", ""
                ),
                "Filter Time (ms)": _as_int_or_none(kwargs.get("filter_time_ms")),
            },
        )
        self.rows[job_uuid] = row
        return True

    async def update_job(self, job_uuid, save=True, **fields):
        await self._roll()
        row = self.rows.get(job_uuid)
        if row is None:
            return False
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
            row[COLUMN_MAP[key]] = _normalize_update_value(key, value)
        return True

    async def claim_pending_classification(self, job_uuid, lease_until):
        await self._roll()
        row = self.rows.get(job_uuid)
        if row is None:
            return False
        if row.get("Final Decision") != "Pending":
            return False
        not_before = row.get("Classification Retry Not Before")
        if not_before is not None:
            not_before = _normalize_timestamp(not_before)
            if not_before is not None and not_before > datetime.now(timezone.utc):
                return False
        row["Classification Retry Not Before"] = datetime.fromtimestamp(
            float(lease_until), tz=timezone.utc
        )
        return True

    async def get_incomplete_classification_jobs(self):
        await self._roll()
        now = datetime.now(timezone.utc)
        rows = []
        for row in self.rows.values():
            if row.get("Final Decision") != "Pending":
                continue
            if row.get("Decision Reason") != "LLM Error":
                continue
            not_before = row.get("Classification Retry Not Before")
            if not_before is not None:
                not_before = _normalize_timestamp(not_before)
                if not_before is not None and not_before > now:
                    continue
            rows.append(dict(row))
        return rows

    async def get_incomplete_notification_jobs(self):
        await self._roll()
        return [
            dict(row)
            for row in self.rows.values()
            if (row.get("Notification Status") or "")
            not in ("", "Complete", "Suppressed")
        ]

    async def get_latest_guard_decision(self, job_uuid):
        await self._roll()
        return self.decisions.get(job_uuid)

    async def get_latest_guard_decision_with_category(self, job_uuid):
        await self._roll()
        return self.decisions.get(job_uuid), None

    async def log_notification_guard(self, *a, **kw):
        await self._roll()
        return None

    async def log_gemini(self, *a, **kw):
        await self._roll()
        return None

    async def log_error(self, module, error, job_uuid="", save=True):
        await self._roll()
        self.errors.append((module, str(error), job_uuid))
        return None

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def seed(self, row):
        self.rows[row["Job UUID"]] = dict(row)


def _normalize_timestamp(value):
    from app.adapters.repositories.postgres import _normalize_timestamp as _nt

    return _nt(value)


class RecordingPublisher:
    """JobStreamPublisher that records every event it is asked to publish.

    `block_on` (an asyncio.Event) makes publish() wait, so a test can
    hold the notification workflow open mid-publish -- the per-job lock
    held by the live caller keeps a concurrent retry sweep from
    re-publishing while the first publish is still in flight.
    """

    def __init__(self, block_on=None):
        self.events = []
        self.block_on = block_on
        self.started = asyncio.Event() if block_on is not None else None

    async def publish(self, event):
        if self.block_on is not None:
            self.started.set()
            await self.block_on.wait()
        self.events.append(dict(event))


async def noop_resolver(job_uuid, row, category_id):
    return category_id


async def allow_all(payload):
    return True


class PassThroughShortener:
    """No-op url_shortener: returns the original URL unchanged, so no
    Short URL is ever written."""

    async def shorten(self, job_id, url):
        return url


class FakeRedisClient:
    """Records XADD calls exactly the way redis.asyncio's client would."""

    def __init__(self, raise_on_xadd=None):
        self.xadds = []
        self.closed = False
        self.raise_on_xadd = raise_on_xadd

    async def xadd(self, stream, fields, maxlen=None, approximate=False):
        if self.raise_on_xadd is not None:
            raise self.raise_on_xadd
        self.xadds.append((stream, dict(fields), maxlen, approximate))

    async def aclose(self):
        self.closed = True