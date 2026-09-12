"""
End-to-end pipeline tests: Telegram event -> parse -> classify -> log.

Importing app.message_processor pulls in the full app.job_processor ->
app.notifier/app.channel_notifier -> app.telegram_bot -> app.config
chain, so (like test_llm_manager.py etc.) this relies on
tests/conftest.py's environment defaults to import without a real
.env. All fixtures below are deliberately built from text that the
classifier rejects outright (no core/enough supporting positive
evidence to reach notify_directly or needs_gemini), so no test here
ever calls Gemini/Groq or sends a real Telegram message -- these stay
genuine offline unit/integration tests of the orchestration layer,
not something that happens to also depend on live credentials
succeeding.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.logger import logger
from app.message_processor import process_message


from app.categories.data_analysis.profile import PROFILE


REJECT_TEXT = "Need someone to create SQL queries for a reporting system."

# Regression fixture for Fix 1/Fix 5 (see
# test_reason_collapse_is_preserved_through_the_pipeline below):
# matched=True (one low-weight "report" supporting-positive hit) but
# decision=reject with reason="insufficient_signal" -- this is exactly
# the shape of result that used to get its reason silently overwritten
# with the generic "Below Gemini Threshold" in job_processor.py.
INSUFFICIENT_SIGNAL_TEXT = (
    "I need someone to write a short report about my company history."
)


class FakeButton:
    url = None


class FakeMessage:
    buttons = []


class FakeChat:
    def __init__(self, title):
        self.title = title


class FakeEvent:
    def __init__(self, event_id, chat_id, chat_title, text):
        self.id = event_id
        self.chat_id = chat_id
        self.chat = FakeChat(chat_title)
        self.message = FakeMessage()
        self.raw_text = text


@pytest.fixture()
def isolated_workbook():
    """
    Point the shared logger singleton at an isolated, temporary
    database file for the duration of a test, instead of the real
    loki_freelance_bot.db -- and restore/close it afterwards.
    Every test in this file that touches the pipeline needs this,
    since app.job_processor logs through the module-level `logger`
    singleton.
    """
    tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")
    original_path = logger.path

    logger.path = Path(tmp_dir) / "test_logs.db"
    logger.initialize()

    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


def test_process_message_logs_exactly_one_job_with_a_decision(isolated_workbook):
    log = isolated_workbook
    jobs_before = log.count_jobs()

    event = FakeEvent(888888, -100111, "Pipeline Test", REJECT_TEXT)
    asyncio.run(process_message(event))

    jobs_after = log.count_jobs()

    assert jobs_after == jobs_before + 1, "Expected exactly one new job to be logged."

    job = log.get_last_job()
    assert job["Job UUID"] is not None, "Job UUID was not written."
    assert job["Title"], "Job title is empty."
    assert job["Decision"] is not None, "Decision was not calculated."


def test_reprocessing_the_same_message_is_deduplicated(isolated_workbook):
    log = isolated_workbook

    event = FakeEvent(888888, -100111, "Pipeline Test", REJECT_TEXT)
    asyncio.run(process_message(event))

    jobs_after_first = log.count_jobs()

    # job_uuid is derived deterministically from (identity_source,
    # job_id) rather than a fresh uuid4() per call, so logger.has_job()
    # recognizes the repeat and process_job() skips it.
    asyncio.run(process_message(event))

    jobs_after_repeat = log.count_jobs()

    assert jobs_after_repeat == jobs_after_first, (
        "Reprocessing the same message must not create a duplicate row "
        "(job_uuid dedup regression)."
    )


def test_channel_title_change_does_not_change_job_identity(isolated_workbook):
    """
    Fix 6 regression test: job identity must come from the channel's
    stable chat_id, not its display title. Two events with the SAME
    chat_id and SAME message id, but a DIFFERENT channel title
    (simulating a channel rename between the two), must still resolve
    to the same job_uuid -- i.e. the second one is recognized as a
    duplicate of the first, not logged as a brand new job.
    """
    log = isolated_workbook

    original_event = FakeEvent(
        777777, -100222, "Original Channel Name", REJECT_TEXT
    )
    asyncio.run(process_message(original_event))

    jobs_after_original = log.count_jobs()

    renamed_event = FakeEvent(
        # Same chat_id (-100222) and same message id (777777) as
        # above -- only the title differs, exactly like a channel
        # rename would look from Loki's point of view.
        777777,
        -100222,
        "Totally Renamed Channel",
        REJECT_TEXT,
    )
    asyncio.run(process_message(renamed_event))

    jobs_after_renamed = log.count_jobs()

    assert jobs_after_renamed == jobs_after_original, (
        "A channel title change must not change job identity -- the "
        "second event (same chat_id + message id) should have been "
        "recognized as a duplicate of the first."
    )


def test_reason_collapse_is_preserved_through_the_pipeline(isolated_workbook):
    """
    Orchestration-boundary regression test for Fix 1. This must fail
    against the pre-fix job_processor.py (which logged the generic
    "Below Gemini Threshold" for any matched=True reject) and pass
    against the fixed version.

    Testing app.filters.keyword_filter() alone is not enough here --
    the bug lived specifically at the job_processor.py boundary where
    the classifier's own `reason` was discarded, so this asserts on
    the actual logged Jobs-sheet row, the same place the audit's
    tuning workflow reads from.
    """
    log = isolated_workbook

    # Sanity check the fixture actually exercises the bug's precondition
    # (matched=True, decision=reject, via the fallthrough branch --
    # not hard_reject/notify_directly/needs_gemini).
    from app.filters import keyword_filter

    classifier_result = keyword_filter(
        INSUFFICIENT_SIGNAL_TEXT, title=INSUFFICIENT_SIGNAL_TEXT,
        profile=PROFILE,
    )
    assert classifier_result["matched"] is True
    assert classifier_result["decision"] == "reject"
    assert classifier_result["hard_reject"] is False
    assert classifier_result["notify_directly"] is False
    assert classifier_result["needs_gemini"] is False
    assert classifier_result["reason"] == "insufficient_signal"

    event = FakeEvent(999999, -100333, "Reason Test Channel", INSUFFICIENT_SIGNAL_TEXT)
    asyncio.run(process_message(event))

    job = log.get_last_job()

    # "Decision" holds the classifier's raw decision string and is
    # written once by create_job(); "Final Decision" holds the
    # human-readable Accepted/Rejected label job_processor.py computes
    # and writes via the later update_job() call -- see app/logger.py's
    # COLUMN_MAP. "Decision Reason" is written by create_job() first
    # (the classifier's raw `reason`) and then OVERWRITTEN by that same
    # update_job() call -- this is exactly where the reason-collapse bug
    # lived.
    logged_final_decision = job["Final Decision"]
    logged_reason = job["Decision Reason"]

    assert logged_final_decision == "Rejected"
    assert logged_reason == "insufficient_signal", (
        f"Expected the classifier's own reason to survive into the "
        f"Jobs sheet, got {logged_reason!r} instead -- this is the "
        f"'Below Gemini Threshold' reason-collapse bug."
    )


REJECT_TEXT_WITH_URL = REJECT_TEXT + " https://example.com/project/4242"


def test_url_is_shortened_after_row_creation_and_row_is_updated(isolated_workbook, monkeypatch):
    """The row is created FIRST with the ORIGINAL long URL; shortening is
    a best-effort follow-up that rewrites the stored URL. The shortener
    must be called with the original url (not something already mutated),
    and when shorten() runs the row must already exist -- that durable-row-
    first ordering is what makes the feature crash-safe (a process death
    between the create and the shorten leaves the job intact with the long
    URL instead of silently losing it)."""
    from app import job_processor

    calls = []

    class _FakeShortener:
        async def shorten(self, url):
            calls.append(url)
            job = log.get_last_job()
            assert job is not None, (
                "the job row must already exist before shortening runs"
            )
            assert job["URL"] == url, (
                "the row must still hold the original long URL when "
                "shortening runs"
            )
            return "http://short.test/abc"

    monkeypatch.setattr(job_processor, "url_shortener", _FakeShortener())

    log = isolated_workbook
    event = FakeEvent(700001, -100444, "Shortener Test", REJECT_TEXT_WITH_URL)
    asyncio.run(process_message(event))

    assert calls == ["https://example.com/project/4242"]

    job = log.get_last_job()
    assert job["URL"] == "http://short.test/abc"


def test_duplicate_url_signal_no_longer_drops_the_job(isolated_workbook, monkeypatch):
    """A 'already shortened' signal from the shortener is no longer
    treated as a duplicate job. The row is created from job_uuid dedup
    (identity_source + job_id) BEFORE the shortener is consulted, so a
    duplicate-status response just leaves the row holding its original
    long URL and the job proceeds normally -- it must not be silently
    dropped, and nothing may escape to the caller."""
    from app import job_processor
    from app.url_shortener import UrlAlreadyExistsError

    class _DuplicateShortener:
        async def shorten(self, url):
            raise UrlAlreadyExistsError(url)

    monkeypatch.setattr(job_processor, "url_shortener", _DuplicateShortener())

    log = isolated_workbook
    jobs_before = log.count_jobs()

    event = FakeEvent(700002, -100444, "Shortener Test", REJECT_TEXT_WITH_URL)
    asyncio.run(process_message(event))  # must not raise

    assert log.count_jobs() == jobs_before + 1, (
        "A duplicate-URL signal from the shortener must not drop the job: "
        "its row was already created from job_uuid dedup and must survive."
    )
    job = log.get_last_job()
    assert job["URL"] == "https://example.com/project/4242", (
        "The row must keep its original URL when the shortener reports "
        "the URL as already shortened."
    )


def test_fail_closed_shortener_error_is_reported_failed_but_keeps_the_row(
    isolated_workbook, monkeypatch
):
    """A genuine (non-duplicate) shortener failure in fail_closed mode
    still raises UrlShorteningError so the source worker skips
    mark_seen()/watermark advancement and retries the job (see
    app/source_worker.py). But because the durable row is created FIRST,
    that retry is now crash-safe: it resumes the existing row (which keeps
    the original URL) instead of re-shortening the same URL into a
    permanent duplicate-drop. process_message() catches the error, logs it,
    and reports failure via its return value (see app/message_processor.py).
    The job row must still exist."""
    from app import job_processor
    from app.url_shortener import UrlShorteningError

    class _FailClosedShortener:
        async def shorten(self, url):
            raise UrlShorteningError("upstream unavailable")

    monkeypatch.setattr(job_processor, "url_shortener", _FailClosedShortener())

    log = isolated_workbook
    jobs_before = log.count_jobs()

    event = FakeEvent(700003, -100444, "Shortener Test", REJECT_TEXT_WITH_URL)
    succeeded = asyncio.run(process_message(event))

    assert succeeded is False, (
        "A fail_closed shortener error must not be reported as a "
        "successfully processed message."
    )
    assert log.count_jobs() == jobs_before + 1, (
        "The row must exist even when fail_closed raises -- a crash or a "
        "retry must resume it, never lose it."
    )
    job = log.get_last_job()
    assert job["URL"] == "https://example.com/project/4242"
