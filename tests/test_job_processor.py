"""
app.job_processor tests -- the dedup identity primitive underneath
every "same source + same job identity -> same UUID -> duplicate
rejected" guarantee described in the audit's State/Recovery/Dedup
analysis, plus the legacy-identity dedup compatibility fix (job_uuid
scheme changed from job["source"] directly to a stable
identity_source; historical rows logged under the old scheme must
still be recognized as duplicates). No app.config dependency issues
here beyond what's already covered by conftest.py (importing
app.job_processor pulls in app.notifier/app.channel_notifier ->
app.config).

The isolated_workbook fixture and REJECT_TEXT fixture text mirror
test_pipeline.py exactly -- all process_job() calls below use text the
classifier rejects outright, so no test here ever calls Gemini/Groq or
sends a real Telegram message.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.job_processor import _make_job_uuid, process_job
from app.logger import logger


REJECT_TEXT = "Need someone to create SQL queries for a reporting system."


@pytest.fixture()
def isolated_workbook():
    tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")
    original_path = logger.path

    logger.path = Path(tmp_dir) / "test_logs.xlsx"
    logger.initialize()

    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


def _jobs_sheet(log):
    return log.workbook["Jobs"]


def _notifications_sheet(log):
    return log.workbook["Notifications"]


def _build_job(source, title=REJECT_TEXT, url="", budget=""):
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": source,
        "url": url,
        "budget": budget,
    }


def _seed_legacy_row(log, legacy_uuid, job_id, source, title):
    """
    Simulate an existing production Jobs-sheet row logged under the
    OLD identity scheme, i.e. before identity_source existed and
    job_uuid was derived from job["source"] directly.
    """
    log.create_job(
        job_uuid=legacy_uuid,
        job_id=job_id,
        source=source,
        title=title,
        description="Historical description.",
        raw_message=title,
        filter_text=title,
        company="",
        url="",
        filter_result={
            "decision": "reject",
            "reason": "insufficient_signal",
            "categories": [],
        },
        filter_time_ms=0,
        save=True,
    )


def test_same_source_and_job_id_always_produce_the_same_uuid():
    first = _make_job_uuid("-100222", "777777")
    second = _make_job_uuid("-100222", "777777")

    assert first == second


def test_different_job_id_produces_a_different_uuid():
    a = _make_job_uuid("-100222", "777777")
    b = _make_job_uuid("-100222", "777778")

    assert a != b


def test_different_identity_source_produces_a_different_uuid():
    """
    This is the exact mechanism Fix 6/Fix 7 rely on: switching
    identity_source (e.g. from a Telegram channel title to its stable
    chat_id, or from FreeHub's live "platform" field to the fixed
    poll-source) changes the derived UUID. That's expected and safe
    for a one-time scheme migration (see message_processor.py's and
    freehub_worker.py's docstrings on why this doesn't cause ongoing
    duplicate notifications), but it does mean this function itself
    has no notion of "these two sources are secretly the same
    channel" -- that equivalence has to be established by the caller
    choosing a stable identity_source in the first place.
    """
    by_title = _make_job_uuid("Some Channel Name", "777777")
    by_chat_id = _make_job_uuid("-100222", "777777")

    assert by_title != by_chat_id


def test_uuid_is_stable_across_process_runs():
    """
    _make_job_uuid must be deterministic across separate Python
    processes/imports (uuid5, not uuid4) -- this is what lets
    logger.has_job() recognize a duplicate after a restart, a
    state.json reset, or a FreeHub seen-cache reset. Hardcoding the
    expected value pins this down: if _JOB_UUID_NAMESPACE or the
    f"{source}:{job_id}" format ever changed, this test documents that
    every already-logged job_uuid in production would stop matching.
    """
    assert (
        _make_job_uuid("-100222", "777777")
        == "b6dd8111-7f02-58cb-b88a-88d9a89b1465"
    )


# ------------------------------------------------------------------
# Legacy-identity dedup compatibility (final round).
#
# Before identity_source existed, job_uuid was always derived from
# job["source"] directly -- a Telegram channel's *title*, or a
# FreeHub project's live "platform" field. Every job logged prior to
# that change has a job_uuid computed that way. If database/
# state.json is ever deleted/corrupted/reset, recovery can walk back
# into that historical territory; without a legacy lookup,
# logger.has_job(new_uuid) wouldn't find the existing legacy row, and
# the same historical job could be reprocessed and re-notified.
# ------------------------------------------------------------------


def test_telegram_legacy_uuid_is_recognized_as_a_duplicate(isolated_workbook):
    """
    Test 1 (required). Reproduces the actual production-risk scenario:
    a job already logged under the OLD identity scheme (job["source"]
    = channel title) must be recognized as a duplicate when the same
    message is reprocessed under the NEW scheme (chat_id-based
    identity_source) -- e.g. after a state.json reset causes Telegram
    recovery to re-walk into history. Must fail if the legacy lookup
    in process_job() is removed (job_uuid alone would not match).
    """
    log = isolated_workbook

    channel_title = "My Freelance Channel"
    chat_id = "-100999888"
    message_id = "555444"

    legacy_uuid = _make_job_uuid(channel_title, message_id)
    _seed_legacy_row(log, legacy_uuid, message_id, channel_title, "Old Title")

    rows_before = _jobs_sheet(log).max_row
    notifications_before = _notifications_sheet(log).max_row

    # Reprocessed under the new chat_id-based identity_source -- same
    # channel/message, exactly what a post-state-reset recovery walk
    # would produce.
    job = _build_job(source=channel_title)
    asyncio.run(process_job(job=job, job_id=message_id, identity_source=chat_id))

    assert _jobs_sheet(log).max_row == rows_before, (
        "A job already logged under the legacy (title-based) UUID "
        "must be recognized as a duplicate, not reprocessed and "
        "logged again under the new UUID."
    )
    assert _notifications_sheet(log).max_row == notifications_before, (
        "No second notification may occur for a job recognized via "
        "the legacy identity lookup."
    )

    new_uuid = _make_job_uuid(chat_id, message_id)
    assert not log.has_job(new_uuid), (
        "The job must not ALSO get logged under the new canonical "
        "UUID -- it should be recognized as the same job via the "
        "legacy lookup and skipped entirely, not double-logged."
    )


def test_freehub_legacy_uuid_is_recognized_as_a_duplicate(isolated_workbook):
    """
    Test 2 (required). Same scenario as above, for FreeHub: a job
    logged under the OLD identity scheme (job["source"] = the live
    "platform" field) must be recognized as a duplicate when the same
    project is reprocessed under the NEW scheme (the fixed poll-source
    -- "kafiil"/"freelancer" -- based identity_source).
    """
    log = isolated_workbook

    live_platform_field = "SomeMarketplaceName"
    poll_source = "kafiil"
    project_id = "freehub-project-42"

    legacy_uuid = _make_job_uuid(live_platform_field, project_id)
    _seed_legacy_row(
        log, legacy_uuid, project_id, live_platform_field, "Old FreeHub Title"
    )

    rows_before = _jobs_sheet(log).max_row
    notifications_before = _notifications_sheet(log).max_row

    job = _build_job(source=live_platform_field)
    asyncio.run(
        process_job(job=job, job_id=project_id, identity_source=poll_source)
    )

    assert _jobs_sheet(log).max_row == rows_before, (
        "A FreeHub job already logged under the legacy "
        "(platform-field-based) UUID must be recognized as a "
        "duplicate, not reprocessed and logged again under the new "
        "poll-source-based UUID."
    )
    assert _notifications_sheet(log).max_row == notifications_before, (
        "No second notification may occur for a FreeHub job "
        "recognized via the legacy identity lookup."
    )

    new_uuid = _make_job_uuid(poll_source, project_id)
    assert not log.has_job(new_uuid)


def test_genuinely_new_job_is_processed_and_logged_under_the_canonical_uuid(
    isolated_workbook,
):
    """
    Test 3 (required). A job with no existing row under either the
    canonical or legacy UUID is processed normally and logged under
    the NEW canonical UUID -- the legacy lookup must never become the
    identity newly-processed jobs are stored under.
    """
    log = isolated_workbook

    channel_title = "Brand New Channel"
    chat_id = "-100111222"
    message_id = "999000"

    canonical_uuid = _make_job_uuid(chat_id, message_id)
    legacy_uuid = _make_job_uuid(channel_title, message_id)

    assert not log.has_job(canonical_uuid)
    assert not log.has_job(legacy_uuid)

    job = _build_job(source=channel_title)
    asyncio.run(process_job(job=job, job_id=message_id, identity_source=chat_id))

    assert log.has_job(canonical_uuid), (
        "A genuinely new job must be logged under the new canonical "
        "(identity_source-based) UUID."
    )
    assert not log.has_job(legacy_uuid), (
        "A genuinely new job must NOT be logged under the legacy "
        "UUID scheme -- the legacy identity is a dedup lookup only, "
        "never a storage key for new jobs."
    )


def test_unrelated_jobs_with_similar_titles_are_not_falsely_deduplicated(
    isolated_workbook,
):
    """
    Test 4 (required). Two different jobs that could plausibly look
    similar (same channel/source, near-identical title text) but with
    different message ids must both be logged -- the legacy-identity
    compatibility lookup must stay an exact, deterministic UUID
    comparison and never become a fuzzy title/description match.
    """
    log = isolated_workbook

    channel_title = "Shared Channel Name"
    chat_id = "-100333444"

    job_a = _build_job(source=channel_title, title=REJECT_TEXT)
    job_b = _build_job(source=channel_title, title=REJECT_TEXT + " (follow-up)")

    asyncio.run(
        process_job(job=job_a, job_id="111", identity_source=chat_id)
    )
    rows_after_first = _jobs_sheet(log).max_row

    asyncio.run(
        process_job(job=job_b, job_id="222", identity_source=chat_id)
    )
    rows_after_second = _jobs_sheet(log).max_row

    assert rows_after_second == rows_after_first + 1, (
        "Two different jobs (different job_id, same source/similar "
        "title) must both be logged -- legacy-identity compatibility "
        "must not become a fuzzy match that conflates unrelated jobs."
    )

    assert log.has_job(_make_job_uuid(chat_id, "111"))
    assert log.has_job(_make_job_uuid(chat_id, "222"))


def test_unrelated_jobs_with_different_sources_are_not_falsely_deduplicated(
    isolated_workbook,
):
    """
    Test 4 (required), second case: same job_id coincidentally reused
    across two genuinely different channels/sources must still be
    treated as two different jobs -- confirms the legacy lookup keys
    on the full (source, job_id) pair, not job_id alone.
    """
    log = isolated_workbook

    job_a = _build_job(source="Channel One")
    job_b = _build_job(source="Channel Two")

    asyncio.run(
        process_job(job=job_a, job_id="777", identity_source="-100555")
    )
    rows_after_first = _jobs_sheet(log).max_row

    asyncio.run(
        process_job(job=job_b, job_id="777", identity_source="-100666")
    )
    rows_after_second = _jobs_sheet(log).max_row

    assert rows_after_second == rows_after_first + 1, (
        "Two different channels/sources reusing the same job_id must "
        "both be logged as distinct jobs."
    )

def test_concurrent_duplicate_processing_creates_only_one_row(isolated_workbook):
    """
    The logger's atomic create-if-absent operation must close the
    check-then-act race: two concurrent process_job() calls for the
    same canonical identity may both reach the logger, but exactly
    one may create the durable Jobs row.
    """
    log = isolated_workbook

    job = _build_job(
        source="Concurrent Channel",
        title=REJECT_TEXT,
    )

    async def run_both():
        await asyncio.gather(
            process_job(
                job=job,
                job_id="concurrent-1",
                identity_source="-100777",
            ),
            process_job(
                job=job,
                job_id="concurrent-1",
                identity_source="-100777",
            ),
        )

    asyncio.run(run_both())

    canonical_uuid = _make_job_uuid("-100777", "concurrent-1")
    assert log.has_job(canonical_uuid)
    assert _jobs_sheet(log).max_row == 2


def test_pending_notification_is_resumed_without_reprocessing(
    isolated_workbook, monkeypatch
):
    """
    A durable Pending notification state must be resumable after a
    restart. This simulates the crash window after the job row was
    persisted but before notification delivery completed.
    """
    log = isolated_workbook

    job = {
        "title": "Power BI Dashboard Needed",
        "description": "Need a Power BI dashboard built from sales data.",
        "raw_text": (
            "Power BI Dashboard Needed\n\n"
            "Need a Power BI dashboard built from sales data."
        ),
        "source": "Test Channel",
        "url": "https://example.invalid/job",
        "budget": "$100",
    }

    from app.job_processor import _make_job_uuid

    job_uuid = _make_job_uuid("-100888", "pending-1")

    from app.filters import keyword_filter

    result = keyword_filter(
        f"{job['title']}\n{job['description']}",
        title=job["title"],
    )
    assert result["notify_directly"] is True

    log.create_job(
        job_uuid=job_uuid,
        job_id="pending-1",
        source=job["source"],
        title=job["title"],
        description=job["description"],
        raw_message=job["raw_text"],
        filter_text=f"{job['title']}\n{job['description']}",
        company="",
        url=job["url"],
        filter_result=result,
        filter_time_ms=0,
        save=True,
    )
    log.update_job(
        job_uuid,
        final_decision="Accepted",
        decision_reason=result["reason"],
        notification_status="Pending",
        save=True,
    )

    sends = {"private": 0, "channel": 0}

    async def fake_private(**kwargs):
        sends["private"] += 1
        return True

    async def fake_channel(**kwargs):
        sends["channel"] += 1
        return True

    monkeypatch.setattr(
        "app.job_processor.send_notification",
        fake_private,
    )
    monkeypatch.setattr(
        "app.job_processor.send_channel_notification",
        fake_channel,
    )

    asyncio.run(
        process_job(
            job=job,
            job_id="pending-1",
            identity_source="-100888",
        )
    )

    assert sends == {"private": 1, "channel": 1}
    row = log.get_job(job_uuid)
    assert row["Notification Status"] == "Complete"
