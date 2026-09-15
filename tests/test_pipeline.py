"""Business Node URL-shortening pipeline tests.

Restores the pipeline-level ordering guarantees the main branch covered
in test_pipeline.py against the refactored flow: the durable job row is
created FIRST with the original URL, shortening runs only for accepted
jobs afterwards into the separate "Short URL" column (crash safety: the
shortener's independent memory can never orphan a job), fail_closed
shortener errors propagate (so the source worker leaves the job unseen)
while the durable row stays recoverable, and rejected jobs never call
the shortener at all.
"""
import asyncio

import pytest

import app.services.job_processor as jp
import app.services.state as state_module
from app.adapters.state.json import JsonStateStore
from app.infra.url_shortener import UrlShorteningError
from app.services.state import StateManager

from tests._business_node_fakes import (
    MemRepository,
    PassThroughShortener,
    RecordingPublisher,
    allow_all,
    noop_resolver,
)

DIRECT_TITLE = "Power BI Dashboard Needed"
DIRECT_DESCRIPTION = "Need a Power BI dashboard built from sales data."
REJECT_TEXT = "Need someone to create SQL queries for a reporting system."


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
    return repo, publisher


def _direct_job(url="https://example.invalid/project/42000"):
    return {
        "title": DIRECT_TITLE,
        "description": DIRECT_DESCRIPTION,
        "raw_text": f"{DIRECT_TITLE}\n\n{DIRECT_DESCRIPTION}",
        "source": "Test Channel",
        "url": url,
        "budget": "",
    }


class RecordingShortener:
    def __init__(self, results=None, fail_times=0):
        self.calls = []
        self._results = list(results or [])
        self._fail_left = fail_times

    async def shorten(self, job_id, url):
        self.calls.append((job_id, url))
        if self._fail_left:
            self._fail_left -= 1
            raise UrlShorteningError("shortener down")
        if self._results:
            return self._results.pop(0)
        return f"https://s.example.invalid/{job_id[:8]}"


def test_url_is_shortened_after_row_creation_and_row_is_updated(
    pipeline, monkeypatch
):
    repo, publisher = pipeline
    original_url = "https://example.invalid/project/42000"

    shortener = RecordingShortener()
    monkeypatch.setattr(jp, "url_shortener", shortener)

    job_uuid = jp._make_job_uuid("-100801", "shorten-1")
    asyncio.run(jp.process_job(_direct_job(original_url), "shorten-1", "-100801"))

    row = repo.rows[job_uuid]
    assert row["URL"] == original_url, (
        "the original URL must never be overwritten -- it is the "
        "durable, pre-shortener value"
    )
    short_url = row["Short URL"]
    assert short_url.startswith("https://s.example.invalid/"), (
        "the shortened link must be written to its own 'Short URL' column"
    )
    assert shortener.calls == [(job_uuid, original_url)], (
        "the shortener must be keyed by the canonical job_uuid"
    )
    assert row["Notification Status"] == "Complete"

    # The published stream event carries the short link for the DM
    # renderer, sourced from the durable row.
    assert len(publisher.events) == 1
    assert publisher.events[0]["Short URL"] == short_url
    assert publisher.events[0]["URL"] == original_url


def test_shortener_fail_closed_keeps_row_and_recovers_on_reprocess(
    pipeline, monkeypatch
):
    """A fail_closed shortener error deliberately propagates out of
    process_job() (the source worker leaves the job unseen), but the
    durable row is already committed with the original URL and the
    notification workflow Pending -- so reprocessing the same message
    resumes the notification instead of losing the job."""
    repo, publisher = pipeline
    job = _direct_job()

    shortener = RecordingShortener(fail_times=1)
    monkeypatch.setattr(jp, "url_shortener", shortener)

    job_uuid = jp._make_job_uuid("-100802", "shorten-fail-1")
    with pytest.raises(UrlShorteningError):
        asyncio.run(jp.process_job(job, "shorten-fail-1", "-100802"))

    row = repo.rows[job_uuid]
    assert row["Final Decision"] == "Accepted", "the decision is durable before shortening"
    assert row["Notification Status"] == "Pending", (
        "the workflow is durably Pending so recovery can resume it"
    )
    assert row["Short URL"] == "", "a failed shortener must not write a partial short link"
    assert publisher.events == [], "no notification must be published while the row is unresolved"

    # The source retries the same message on its next poll: the already
    # Pending workflow is resumed (shortening is not redone -- there is
    # nothing to gain) and the job reaches the stream.
    asyncio.run(jp.process_job(job, "shorten-fail-1", "-100802"))

    assert repo.rows[job_uuid]["Notification Status"] == "Complete"
    assert len(publisher.events) == 1
    assert len(shortener.calls) == 1, (
        "a resumed workflow must not re-invoke the shortener"
    )


def test_fail_open_shortener_falls_back_to_original_url(pipeline, monkeypatch):
    """fail_open returns the original URL; no Short URL is written (the
    renderer falls back) and the pipeline completes normally."""
    repo, publisher = pipeline

    # fail_open returns the ORIGINAL URL (no shortening happened), so the
    # no-op guard in process_job skips the "Short URL" write entirely.
    monkeypatch.setattr(jp, "url_shortener", PassThroughShortener())

    job_uuid = jp._make_job_uuid("-100803", "shorten-open-1")
    asyncio.run(jp.process_job(_direct_job(), "shorten-open-1", "-100803"))

    row = repo.rows[job_uuid]
    assert row["Short URL"] == ""
    assert row["Notification Status"] == "Complete"
    assert len(publisher.events) == 1
    assert publisher.events[0]["Short URL"] == ""


def test_rejected_job_never_calls_the_shortener(pipeline, monkeypatch):
    repo, publisher = pipeline
    shortener = RecordingShortener()
    monkeypatch.setattr(jp, "url_shortener", shortener)

    job = {
        "title": REJECT_TEXT,
        "description": "",
        "raw_text": REJECT_TEXT,
        "source": "Test Channel",
        "url": "https://example.invalid/rejected",
        "budget": "",
    }
    job_uuid = jp._make_job_uuid("-100804", "rejected-1")
    asyncio.run(jp.process_job(job, "rejected-1", "-100804"))

    assert shortener.calls == [], (
        "rejected jobs must never call the shortener -- no orphan short "
        "links or wasted upstream calls for jobs that will never deliver"
    )
    assert repo.rows[job_uuid]["Final Decision"] == "Rejected"
    assert publisher.events == []