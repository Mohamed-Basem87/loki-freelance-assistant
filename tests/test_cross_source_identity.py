"""
Cross-subsystem identity/dedup boundary tests.

The audit's own assessment was that most of the 198 existing tests are
good *subsystem* tests, and the remaining weaknesses sit at subsystem
boundaries. This file targets exactly one boundary: whether the same
underlying job, arriving through different call shapes (a second
Telegram message, a second FreeHub poll, or -- the one nothing
exercised before -- the SAME project surfacing through Telegram and
FreeHub) is actually recognized as a duplicate end-to-end through
app.job_processor.process_job(), not just at the _make_job_uuid()
unit level.

Assumption, documented per the task instructions: FreeHub's
`project_link` field is assumed to use the same `<platform-domain>
/project/<numeric-id>` shape used by Mostaql-sourced Telegram posts
(evidenced in tests/test_parser.py, e.g.
"https://mostaql.com/project/12345"). This is a reasonable assumption
because "mostaql" is itself one of FreeHub's own configured upstream
sources (app.freehub.SOURCES), so a FreeHub project polled from
Mostaql plausibly carries a project_link pointing at the same
mostaql.com/project/<id> page a human would also see posted to
Telegram. The repository does not contain a captured real FreeHub API
response to confirm this beyond doubt, so this is written against the
intended/supported URL shape (the one _extract_project_id() actually
recognizes) rather than an invented format that would trivially bypass
it.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

import app.state as state_module
from app.job_processor import _make_job_uuid, process_job
from app.logger import logger


REJECT_TEXT = "Need someone to create SQL queries for a reporting system."


@pytest.fixture()
def isolated_database():
    tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")
    original_path = logger.path

    logger.path = Path(tmp_dir) / "test_logs.db"
    logger.initialize()

    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


@pytest.fixture()
def isolated_state_file(tmp_path, monkeypatch):
    """Point app.state.STATE_FILE at a throwaway path, matching
    tests/test_state.py and tests/test_freehub.py exactly, and reset
    the shared `state` singleton's in-memory data so a previous test's
    cross-source claims can't leak in."""
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)
    state_module.state.data = {}
    return fake_state_file


def _telegram_job(url="", title=REJECT_TEXT):
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": "Test Telegram Channel",
        "url": url,
        "budget": "",
    }


def _freehub_job(url="", title=REJECT_TEXT, platform="mostaql"):
    # Mirrors app.freehub_worker.freehub_worker()'s job dict shape.
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": platform,
        "url": url,
        "budget": "",
    }


# ------------------------------------------------------------------
# Same Telegram job twice.
# ------------------------------------------------------------------


def test_same_telegram_message_processed_twice_is_deduplicated(
    isolated_database, isolated_state_file
):
    log = isolated_database

    job = _telegram_job()

    asyncio.run(process_job(job=job, job_id="55501", identity_source="-100701"))
    asyncio.run(process_job(job=job, job_id="55501", identity_source="-100701"))

    assert log.count_jobs() == 1

    job_uuid = _make_job_uuid("-100701", "55501")
    assert log.get_job(job_uuid) is not None


# ------------------------------------------------------------------
# Same FreeHub job twice.
# ------------------------------------------------------------------


def test_same_freehub_project_polled_twice_is_deduplicated(
    isolated_database, isolated_state_file
):
    log = isolated_database

    job = _freehub_job(url="https://mostaql.com/project/40001")

    # freehub_worker() always passes identity_source="_poll_source"
    # (here "mostaql"), independent of job["source"].
    asyncio.run(process_job(job=job, job_id="uid-40001", identity_source="mostaql"))
    asyncio.run(process_job(job=job, job_id="uid-40001", identity_source="mostaql"))

    assert log.count_jobs() == 1

    job_uuid = _make_job_uuid("mostaql", "uid-40001")
    assert log.get_job(job_uuid) is not None


# ------------------------------------------------------------------
# Same underlying project from Telegram + FreeHub.
# ------------------------------------------------------------------


def test_same_project_from_telegram_and_freehub_cross_source_dedup(
    isolated_database, isolated_state_file
):
    """
    A project posted to a monitored Telegram channel AND later polled
    from FreeHub (or vice versa) has two different job_uuids (different
    identity_source/job_id pairs -- Telegram uses the channel id +
    message id, FreeHub uses "mostaql" + the FreeHub uid). They can
    never collide through _make_job_uuid(). Cross-source dedup instead
    relies entirely on _extract_project_id() pulling the same numeric
    ID out of both jobs' `url`, and app.state.StateManager atomically
    claiming that ID for whichever one is processed first (see
    process_job()'s `state.async_claim_cross_source_project` call).

    This is the one boundary nothing in the existing suite exercised
    end-to-end: test_state.py tests claim_cross_source_project() in
    isolation, and test_job_processor.py's dedup tests never give two
    *different* identities the same project URL.
    """
    log = isolated_database

    project_url = "https://mostaql.com/project/778899"

    telegram_job = _telegram_job(url=project_url, title="Need a Python scraper built")
    freehub_job = _freehub_job(url=project_url, title="Need a Python scraper built")

    asyncio.run(
        process_job(
            job=telegram_job,
            job_id="66601",
            identity_source="-100702",
        )
    )
    asyncio.run(
        process_job(
            job=freehub_job,
            job_id="uid-778899",
            identity_source="mostaql",
        )
    )

    # Two distinct rows exist (different identities), but only the
    # first is Accepted-track; the second must be recognized as the
    # same underlying project and rejected as a duplicate.
    assert log.count_jobs() == 2

    telegram_uuid = _make_job_uuid("-100702", "66601")
    freehub_uuid = _make_job_uuid("mostaql", "uid-778899")

    telegram_row = log.get_job(telegram_uuid)
    freehub_row = log.get_job(freehub_uuid)

    assert telegram_row is not None
    assert freehub_row is not None

    assert freehub_row["Final Decision"] == "Rejected"
    assert freehub_row["Decision Reason"] == "Duplicate project from another source"

    # The first (Telegram) job must NOT have been affected by the
    # later duplicate -- it went through the normal classifier path.
    assert telegram_row["Decision Reason"] != "Duplicate project from another source"


def test_same_project_reversed_order_freehub_then_telegram(
    isolated_database, isolated_state_file
):
    """Same as above with the arrival order reversed, to confirm the
    claim isn't accidentally order- or source-dependent."""
    log = isolated_database

    project_url = "https://mostaql.com/project/990011"

    freehub_job = _freehub_job(url=project_url, title="Need a Python scraper built")
    telegram_job = _telegram_job(url=project_url, title="Need a Python scraper built")

    asyncio.run(
        process_job(
            job=freehub_job,
            job_id="uid-990011",
            identity_source="mostaql",
        )
    )
    asyncio.run(
        process_job(
            job=telegram_job,
            job_id="77701",
            identity_source="-100703",
        )
    )

    assert log.count_jobs() == 2

    telegram_uuid = _make_job_uuid("-100703", "77701")
    telegram_row = log.get_job(telegram_uuid)

    assert telegram_row is not None
    assert telegram_row["Final Decision"] == "Rejected"
    assert telegram_row["Decision Reason"] == "Duplicate project from another source"
