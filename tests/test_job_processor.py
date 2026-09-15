"""Business Node dedup-identity, atomicity and LLM-durability tests.

Restores the coverage the main branch had in test_job_processor.py
against the refactored Postgres-backed pipeline:
  - job_uuid determinism (the primitive underneath every dedup/at-least-
    once guarantee, pinned to the exact uuid5 value production uses so a
    namespace/format change can never silently orphan historical rows),
  - legacy-identity dedup compatibility (rows logged under the old
    source-based uuid scheme are still recognized as duplicates),
  - atomic create-if-absent under concurrency (exactly one durable row),
  - durable LLM-pending state: a failed classification persists a
    retry-not-before schedule, is NOT re-attempted before it is due by
    any caller, and is resumed by the classification retry sweep, and
  - the classification claim: exactly one of two concurrent callers may
    win a durably-pending job and reach the LLM provider.

All scenarios run through process_job() / the sweeps with the hermetic
MemRepository (Postgres adapter semantics) + isolated dedup, so nothing
here needs a live database.
"""
import asyncio

import pytest

import app.services.job_processor as jp
import app.services.state as state_module
from app.adapters.state.json import JsonStateStore
from app.services.state import StateManager

from tests._business_node_fakes import (
    MemRepository,
    RecordingPublisher,
    PassThroughShortener,
    allow_all,
    noop_resolver,
)

REJECT_TEXT = "Need someone to create SQL queries for a reporting system."
FLUTTER_TITLE = "Build a Flutter Mobile App with Laravel Backend Dashboard"
FLUTTER_DESCRIPTION = (
    "Need a cross-platform Flutter app with Laravel REST API backend "
    "and admin dashboard."
)


@pytest.fixture()
def isolated_dedup(tmp_path, monkeypatch):
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)
    manager = StateManager()
    manager.load()
    return JsonStateStore(manager)


@pytest.fixture()
def pipeline(isolated_dedup, monkeypatch):
    repo = MemRepository()
    publisher = RecordingPublisher()
    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "dedup", isolated_dedup)
    monkeypatch.setattr(jp, "stream_publisher", publisher)
    monkeypatch.setattr(jp, "resolver", noop_resolver)
    monkeypatch.setattr(jp, "guard_allow", allow_all)
    monkeypatch.setattr(jp, "url_shortener", PassThroughShortener())
    return repo, publisher


def _job(source, title=REJECT_TEXT, url="", budget=""):
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": source,
        "url": url,
        "budget": budget,
    }


def _run(job, job_id, identity_source):
    asyncio.run(
        jp.process_job(job=job, job_id=job_id, identity_source=identity_source)
    )


def _seed_legacy_row(repo, legacy_uuid, job_id, source, title):
    repo.seed(
        {
            "Job UUID": legacy_uuid,
            "Job ID": job_id,
            "Identity Source": source,
            "Source": source,
            "Title": title,
            "Description": "Historical description.",
            "Raw Message": title,
            "Company": "",
            "URL": "",
            "Decision": "reject",
            "Decision Reason": "insufficient_signal",
            "Categories": "",
            "Negative Categories": "",
            "Final Decision": "Rejected",
            "Notification Status": "Complete",
        }
    )


# ------------------------------------------------------------------
# job_uuid determinism
# ------------------------------------------------------------------


def test_same_source_and_job_id_always_produce_the_same_uuid():
    assert jp._make_job_uuid("-100222", "777777") == jp._make_job_uuid(
        "-100222", "777777"
    )


def test_different_job_id_produces_a_different_uuid():
    assert jp._make_job_uuid("-100222", "777777") != jp._make_job_uuid(
        "-100222", "777778"
    )


def test_different_identity_source_produces_a_different_uuid():
    assert jp._make_job_uuid("Some Channel Name", "777777") != jp._make_job_uuid(
        "-100222", "777777"
    )


def test_uuid_is_stable_across_process_runs():
    """uuid5 (not uuid4): deterministic across separate processes, so
    logger.has_job() recognizes a duplicate after a restart. The pinned
    value is the SAME one the main branch produced -- if the namespace
    or f"{source}:{job_id}" format ever changed, every already-logged
    job_uuid in production would stop matching."""
    assert (
        jp._make_job_uuid("-100222", "777777")
        == "b6dd8111-7f02-58cb-b88a-88d9a89b1465"
    )


# ------------------------------------------------------------------
# Legacy-identity dedup compatibility
# ------------------------------------------------------------------


def test_telegram_legacy_uuid_is_recognized_as_a_duplicate(pipeline):
    """A job logged under the OLD scheme (job["source"] = channel title)
    must be recognized as a duplicate when the same message is
    reprocessed under the NEW scheme (chat_id identity_source)."""
    repo, _ = pipeline

    channel_title = "My Freelance Channel"
    chat_id = "-100999888"
    message_id = "555444"

    legacy_uuid = jp._make_job_uuid(channel_title, message_id)
    _seed_legacy_row(repo, legacy_uuid, message_id, channel_title, "Old Title")

    _run(_job(source=channel_title), message_id, chat_id)

    assert set(repo.rows) == {legacy_uuid}, (
        "a job seen via the legacy lookup must not be reprocessed or "
        "duplicated under the new canonical uuid"
    )


def test_freehub_legacy_uuid_is_recognized_as_a_duplicate(pipeline):
    """Same scenario for FreeHub: a job logged under the OLD scheme (job
    ["source"] = live platform field) must be recognized as a duplicate
    reprocessed under the NEW scheme (the fixed poll source)."""
    repo, _ = pipeline

    live_platform_field = "SomeMarketplaceName"
    poll_source = "kafiil"
    project_id = "freehub-project-42"

    legacy_uuid = jp._make_job_uuid(live_platform_field, project_id)
    _seed_legacy_row(repo, legacy_uuid, project_id, live_platform_field, "Old FreeHub Title")

    _run(_job(source=live_platform_field), project_id, poll_source)

    assert set(repo.rows) == {legacy_uuid}


def test_genuinely_new_job_is_processed_and_logged_under_the_canonical_uuid(
    pipeline,
):
    """A job with no existing row under either uuid is processed normally
    and stored under the NEW canonical uuid -- the legacy lookup is a
    dedup check only, never a storage key."""
    repo, _ = pipeline

    channel_title = "Brand New Channel"
    chat_id = "-100111222"
    message_id = "999000"

    canonical_uuid = jp._make_job_uuid(chat_id, message_id)
    legacy_uuid = jp._make_job_uuid(channel_title, message_id)

    _run(_job(source=channel_title), message_id, chat_id)

    assert canonical_uuid in repo.rows
    assert legacy_uuid not in repo.rows


def test_unrelated_jobs_with_similar_titles_are_not_falsely_deduplicated(
    pipeline,
):
    repo, _ = pipeline
    channel_title = "Shared Channel Name"
    chat_id = "-100333444"

    _run(_job(source=channel_title, title=REJECT_TEXT), "111", chat_id)
    _run(_job(source=channel_title, title=REJECT_TEXT + " (follow-up)"), "222", chat_id)

    assert len(repo.rows) == 2
    assert jp._make_job_uuid(chat_id, "111") in repo.rows
    assert jp._make_job_uuid(chat_id, "222") in repo.rows


def test_unrelated_jobs_with_different_sources_are_not_falsely_deduplicated(
    pipeline,
):
    """The same job_id reused across genuinely different channels must
    stay two different jobs -- the identity is the FULL (source, job_id)
    pair, never job_id alone."""
    repo, _ = pipeline

    _run(_job(source="Channel One"), "777", "-100555")
    _run(_job(source="Channel Two"), "777", "-100666")

    assert len(repo.rows) == 2


# ------------------------------------------------------------------
# Atomic create-if-absent under concurrency
# ------------------------------------------------------------------


def test_concurrent_duplicate_processing_creates_only_one_row(pipeline):
    repo, _ = pipeline
    job = _job(source="Concurrent Channel")

    async def run_both():
        await asyncio.gather(
            jp.process_job(job=job, job_id="concurrent-1", identity_source="-100777"),
            jp.process_job(job=job, job_id="concurrent-1", identity_source="-100777"),
        )

    asyncio.run(run_both())

    canonical_uuid = jp._make_job_uuid("-100777", "concurrent-1")
    assert canonical_uuid in repo.rows
    assert len(repo.rows) == 1
    assert repo.rows[canonical_uuid]["Final Decision"] == "Rejected"


# ------------------------------------------------------------------
# Durable LLM-pending state (retry-not-before) + claim race
# ------------------------------------------------------------------


def _flutter_job():
    return {
        "title": FLUTTER_TITLE,
        "description": FLUTTER_DESCRIPTION,
        "raw_text": f"{FLUTTER_TITLE}\n{FLUTTER_DESCRIPTION}",
        "source": "test",
        "url": "https://example.invalid/llm-retry",
        "budget": "",
    }


def _arbitration_success(filter_text, candidates, **kwargs):
    return {
        "selected_category": candidates[0]["id"],
        "reason": "recovered",
        "confidence": 0.9,
    }


def test_llm_error_is_durable_and_retryable(pipeline, monkeypatch):
    repo, _ = pipeline
    job = _flutter_job()

    def _down(*args, **kwargs):
        raise RuntimeError("provider outage")

    monkeypatch.setattr(jp, "arbitrate_category", _down)

    with pytest.raises(jp.ClassificationPendingError):
        asyncio.run(jp.process_job(job, "llm-retry", "test"))

    row = repo.rows[jp._make_job_uuid("test", "llm-retry")]
    assert row["Identity Source"] == "test"
    assert row["Final Decision"] == "Pending"
    assert row["Decision Reason"] == "LLM Error"
    assert row["Classification Retry Not Before"], (
        "a failed classification must persist a retry-not-before schedule"
    )

    # A caller re-surfacing the job before its backoff window elapses
    # must not trigger a fresh LLM call at all.
    calls = {"count": 0}

    def _counting(*args, **kwargs):
        calls["count"] += 1
        return _arbitration_success(*args, **kwargs)

    monkeypatch.setattr(jp, "arbitrate_category", _counting)
    with pytest.raises(jp.ClassificationPendingError):
        asyncio.run(jp.process_job(job, "llm-retry", "test"))
    assert calls["count"] == 0, (
        "reprocessing before the retry-not-before schedule must not call the provider"
    )

    # The dedicated sweep must also honor the not-yet-due schedule.
    assert asyncio.run(jp.retry_incomplete_classifications()) == 0
    assert calls["count"] == 0

    # Once the schedule has passed, both paths succeed again.
    asyncio.run(
        repo.update_job(
            jp._make_job_uuid("test", "llm-retry"),
            classification_retry_not_before="0",
            save=True,
        )
    )
    assert asyncio.run(jp.retry_incomplete_classifications()) == 1
    recovered = repo.rows[jp._make_job_uuid("test", "llm-retry")]
    assert recovered["Final Decision"] == "Accepted"


def test_two_concurrent_callers_cannot_both_classify_the_same_pending_job(
    pipeline, monkeypatch
):
    """FreeHub rediscovery and the classification retry sweep are two
    independent callers that can both observe the same durably-pending
    job past its backoff window at once. Exactly one may win the
    claim_pending_classification() lease and call the provider."""
    repo, _ = pipeline
    job = _flutter_job()
    job["url"] = "https://example.invalid/llm-race"

    # Seed a durably-pending job whose backoff window has elapsed.
    def _down(*args, **kwargs):
        raise RuntimeError("provider outage")

    monkeypatch.setattr(jp, "arbitrate_category", _down)
    with pytest.raises(jp.ClassificationPendingError):
        asyncio.run(jp.process_job(job, "llm-race", "test"))
    job_uuid = jp._make_job_uuid("test", "llm-race")
    asyncio.run(
        repo.update_job(job_uuid, classification_retry_not_before="0", save=True)
    )

    calls = {"count": 0}

    def _counting(*args, **kwargs):
        calls["count"] += 1
        return _arbitration_success(*args, **kwargs)

    monkeypatch.setattr(jp, "arbitrate_category", _counting)

    async def _race():
        return await asyncio.gather(
            jp.process_job(job, "llm-race", "test"),
            jp.process_job(job, "llm-race", "test"),
            return_exceptions=True,
        )

    results = asyncio.run(_race())
    pending_errors = [
        r for r in results if isinstance(r, jp.ClassificationPendingError)
    ]
    assert calls["count"] == 1, (
        "exactly one concurrent caller must win the classification claim "
        "and call the LLM provider"
    )
    assert len(pending_errors) == 1, (
        "the losing caller must back off rather than also classify"
    )
    assert repo.rows[job_uuid]["Final Decision"] == "Accepted"