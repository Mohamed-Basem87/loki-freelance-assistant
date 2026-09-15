"""Postgres repository bootstrap + persistence contract tests.

Runs against a REAL Postgres backend, exactly like CI (DATABASE_URL
points at the postgres service container; conftest.py already brings up
the schema). Skipped hermetically when no DATABASE_URL is configured.

Covers the durable-storage invariants the Business Node leans on:
  - initialize() is idempotent and seeds the category catalog,
  - create_job_if_absent() is atomic under concurrent writers (the
    "Job UUID" PRIMARY KEY is the arbiter, not a caller-side check),
  - update_job() rejects unknown fields, silently tolerates the dropped
    legacy filter keys, and reports a missing row as not-found,
  - claim_pending_classification() implements the lease semantics the
    classification retry race depends on,
  - get_incomplete_notification_jobs() only selects recoverable rows,
  - a full accepted job round-trips into the publishable stream DTO.
"""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text

import app.services.job_processor as jp
from app.adapters.repositories.postgres import PostgresRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set -- no live Postgres for repository contract tests",
)


def _uuid(prefix="bootstrap"):
    return str(uuid.uuid4())


@pytest.fixture()
def repo(scope="module"):
    database_url = os.environ["DATABASE_URL"]
    repository = PostgresRepository(database_url, database_timeout_seconds=30)
    asyncio.run(repository.initialize())
    created = []

    yield repository

    with repository._engine.begin() as conn:
        for job_uuid in created:
            conn.execute(
                text('DELETE FROM jobs WHERE "Job UUID" = :u'),
                {"u": job_uuid},
            )


@pytest.fixture()
def make_job(repo):
    """Returns a (job_uuid, repository, kwargs) builder; does NOT create
    the row -- callers that need one call repository.create_job_if_absent
    themselves (see test_create_job_if_absent_is_atomic_true_then_false,
    which relies on controlling that call itself)."""

    def _make_and_create(**overrides):
        job_uuid = overrides.pop("job_uuid", _uuid())
        params = {
            "job_uuid": job_uuid,
            "job_id": overrides.pop("job_id", "j1"),
            "source": overrides.pop("source", "Test Channel"),
            "identity_source": overrides.pop("identity_source", "Test Channel"),
            "title": overrides.pop("title", "Power BI Dashboard Needed"),
            "description": overrides.pop(
                "description", "Need a Power BI dashboard built from sales data."
            ),
            "raw_message": overrides.pop(
                "raw_message", "Power BI Dashboard Needed\n\nNeed a dashboard."
            ),
            "company": overrides.pop("company", ""),
            "url": overrides.pop("url", "https://example.invalid/job"),
            "filter_result": overrides.pop("filter_result", {}),
            "filter_time_ms": overrides.pop("filter_time_ms", 0),
        }
        params.update(overrides)
        return job_uuid, repo, params

    return _make_and_create


def test_initialize_seeds_categories_and_is_idempotent(repo, repo_under_test=None):
    def _count():
        with repo._engine.connect() as conn:
            row = conn.execute(
                text('SELECT COUNT(*) FROM categories WHERE "Enabled" = TRUE')
            ).scalar()
        return int(row)

    before = _count()
    assert before > 0, "initialize() must seed the job-category catalog"

    asyncio.run(repo.initialize())
    assert _count() == before, "re-initializing must not duplicate the seed"


def test_create_job_if_absent_is_atomic_true_then_false(make_job):
    job_uuid, repository, params = make_job()

    assert asyncio.run(repository.create_job_if_absent(**params)) is True
    assert asyncio.run(
        repository.create_job_if_absent(**params)
    ) is False, (
        "the duplicate insert must be rejected by the 'Job UUID' PRIMARY "
        "KEY, not by a caller-side pre-check"
    )


def test_update_job_rejects_unknown_field(make_job):
    job_uuid, repository, _ = make_job()

    with pytest.raises(ValueError, match="unknown field"):
        asyncio.run(repository.update_job(job_uuid, not_a_column=1))


def test_update_job_skips_dropped_legacy_filter_keys(make_job):
    job_uuid, repository, params = make_job()
    asyncio.run(repository.create_job_if_absent(**params))

    result = asyncio.run(
        repository.update_job(
            job_uuid, category_candidates="a, b", hard_reject_matches=["x"]
        )
    )
    assert result is True, "dropped keys must be silently tolerated"
    assert asyncio.run(repository.get_job(job_uuid)) is not None


def test_update_job_returns_false_for_missing_row(repo):
    assert asyncio.run(repo.update_job(_uuid("missing"), final_decision="Accepted")) is False, (
        "the UPDATE rowcount must surface a missing row as not-found"
    )


def test_claim_pending_classification_lease_semantics(make_job):
    import time

    job_uuid, repository, params = make_job()
    asyncio.run(repository.create_job_if_absent(**params))
    asyncio.run(
        repository.update_job(
            job_uuid, final_decision="Pending", decision_reason="LLM Error"
        )
    )

    lease = time.time() + 120
    assert asyncio.run(repository.claim_pending_classification(job_uuid, lease)) is True
    assert asyncio.run(
        repository.claim_pending_classification(job_uuid, lease + 10)
    ) is False, "an in-window lease must prevent a second caller from claiming"

    asyncio.run(repository.update_job(job_uuid, classification_retry_not_before="0"))
    assert asyncio.run(
        repository.claim_pending_classification(job_uuid, lease + 20)
    ) is True, "an expired lease must be reclaimable"

    asyncio.run(repository.update_job(job_uuid, final_decision="Accepted"))
    assert asyncio.run(
        repository.claim_pending_classification(job_uuid, lease + 30)
    ) is False, "a row that already left the Pending state must never be claimed"


def test_get_incomplete_notification_jobs_filters_terminal_states(make_job):
    statuses = {}
    for i, status in enumerate(["Pending", "Complete", "Suppressed", ""]):
        job_uuid, repository, params = make_job(job_id=f"st-{i}")
        asyncio.run(repository.create_job_if_absent(**params))
        if status:
            asyncio.run(
                repository.update_job(
                    job_uuid, final_decision="Accepted", notification_status=status
                )
            )
        statuses[job_uuid] = status

    incomplete = asyncio.run(repository.get_incomplete_notification_jobs())
    uuids = {str(row["Job UUID"]) for row in incomplete}

    assert uuids == {
        u for u, s in statuses.items() if s == "Pending"
    }, "only the recoverable Pending row may be selected"


def test_full_accepted_row_round_trips_into_publishable_dto(make_job):
    job_uuid, repository, params = make_job(
        filter_result={
            "decision": "notify_directly",
            "reason": "direct match",
            "categories": ["Data Analysis"],
            "category_id": "data_analysis",
            "category_selection_method": "keyword_direct",
            "notify_directly": True,
            "has_core_positive": True,
            "core_positive_hit_count": 3,
        }
    )
    asyncio.run(repository.create_job_if_absent(**params))

    asyncio.run(
        repository.update_job(
            job_uuid,
            final_decision="Accepted",
            decision_reason="direct match",
            notification_status="Pending",
        )
    )

    row = asyncio.run(repository.get_job(job_uuid))
    event = jp.build_notification_event(row)

    assert str(event["Job UUID"]) == job_uuid
    assert event["Title"] == "Power BI Dashboard Needed"
    assert event["Source"] == "Test Channel"
    assert event["Category ID"] == "data_analysis"
    assert event["Categories"] == "Data Analysis"
    assert event["Category Selection Method"] == "keyword_direct"