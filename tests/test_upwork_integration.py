"""Integrated Upwork scraper tests (scraper/scraper.py).

These exercise the Upwork path that is part of the unified scraper
pipeline. They cover the pieces the integration actually changed:

  * search URL building (global, no location filter; per_page clamped)
  * search-card extraction (Upwork identity, stable JobID, description)
  * detail parsing (JSON-LD wins, "Upwork" is never the client Company,
    datePosted + client country captured)
  * blocked/failed detail handling: keep the full card description
  * to_output_schema keeps those described jobs and still drops the
    genuinely empty ones

scraper.py eagerly imports scrapling.fetchers, which needs Playwright.
When Playwright is absent (local dev boxes), a minimal FetcherSession
stub is installed *for this test process only* so the pure extraction /
output logic can still be tested. Production imports are untouched.
"""

import asyncio
import json
import sys
import types

import pytest

try:  # pragma: no cover - depends on the local environment

    from scrapling.fetchers import FetcherSession  # noqa: F401

except Exception:  # playwright not installed

    _fetchers_stub = types.ModuleType("scrapling.fetchers")

    class FetcherSession:  # noqa: D401 - minimal test double
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    _fetchers_stub.FetcherSession = FetcherSession
    sys.modules["scrapling.fetchers"] = _fetchers_stub

from scrapling.parser import Selector

from scraper import scraper as sc


class FakeResponse:

    def __init__(self, html, status=200, url="https://www.upwork.com/"):
        self._html = html
        self.status = status
        self.url = url

    @property
    def text(self):
        return self._html

    @property
    def body(self):
        return self._html.encode("utf-8")

    def css(self, query):
        return Selector(self._html).css(query)


FIXED_CARD = """
<article class="job-tile" data-ev-job-uid="2120881892029947726">
  <small data-test="job-pubilshed-date"><span>Posted 39 minutes ago</span></small>
  <h2 class="job-tile-title">
    <a data-test="job-tile-title-link"
       href="/jobs/Foo-Bar_~022120881892029947726/?referrer_url_path=/nx/search/jobs/">
      <span>Foo <span class="highlight">Python</span> Bar</span>
    </a>
  </h2>
  <ul class="job-tile-info-list">
    <li data-test="job-type-label"><strong>Fixed price</strong></li>
    <li data-test="experience-level"><strong>Intermediate</strong></li>
    <li data-test="is-fixed-price"><strong>Est. budget:</strong> <strong>$1,500.00</strong></li>
  </ul>
  <div class="air3-line-clamp-wrapper">
    <div class="air3-line-clamp"><p class="body-medium">Build the thing.</p></div>
  </div>
  <button class="air3-token" data-test="token"><span>Python</span></button>
</article>
"""


DETAIL_HTML = (
    '<script type="application/ld+json">'
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Detail Title",
            "description": "<p>Detail description.</p>",
            "datePosted": "2026-11-12T14:51:39.738Z",
            # Confirmed live: Upwork always names itself here.
            "hiringOrganization": {"name": "Upwork"},
        }
    )
    + "</script>"
    '<ul class="ac-items">'
    '<li data-qa="client-location">'
    "<strong>United States</strong>"
    "<div><span>9:08 AM</span></div>"
    "</li></ul>"
)


def _card_job(**overrides):
    job = {
        "Platform": "Upwork",
        "Title": "Foo Python Bar",
        "Company": "N/A",
        "Location": "Anywhere",
        "Link": "https://www.upwork.com/jobs/Foo-Bar_~022120881892029947726",
        "JobID": "022120881892029947726",
        "Description": "Build the thing.",
        "DescriptionSource": "search_card",
    }
    job.update(overrides)
    return job


# ================================================================
# Search URL / card extraction
# ================================================================

def test_search_url_is_global_and_clamps_page_size():
    url = sc.build_upwork_search_url(
        keyword="python", page=2, per_page=999
    )

    # The user asked for all jobs: no location/country filter.
    assert "location" not in url.lower()
    assert "country" not in url.lower()

    assert "q=python" in url
    assert "sort=recency" in url
    assert "per_page=50" in url
    assert "page=2" in url
    # No t= -> hourly and fixed-price both included.
    assert "&t=" not in url


def test_extract_card_produces_upwork_records():
    response = FakeResponse(
        f"<html><body>{FIXED_CARD}</body></html>"
    )

    jobs = sc.extract_upwork_jobs(response)

    assert len(jobs) == 1
    job = jobs[0]

    assert job["Platform"] == "Upwork"
    assert job["Title"] == "Foo Python Bar"
    assert job["Company"] == "N/A"
    assert job["JobID"] == "022120881892029947726"
    assert job["Description"] == "Build the thing."
    assert job["DescriptionSource"] == "search_card"
    assert "referrer_url_path" not in job["Link"]


# ================================================================
# Detail parsing
# ================================================================

def test_parse_detail_ignores_upwork_hiring_org_and_captures_client_data():
    job = _card_job()

    result = sc.parse_upwork_detail(job, FakeResponse(DETAIL_HTML))

    assert result["Description"] == "Detail description."
    assert result["DescriptionSource"] == "detail_jsonld"
    assert result["Title"] == "Detail Title"
    # "Upwork" is never the client.
    assert result["Company"] == "N/A"
    assert result["DatePosted"] == "2026-11-12T14:51:39.738Z"
    assert result["ClientLocation"] == "United States"
    assert "DetailBlocked" not in result
    assert "DetailError" not in result


def test_detail_worker_routes_upwork_to_the_upwork_parser(monkeypatch):
    async def fake_fetch_upwork(session, url, semaphore, pacing=0.0):
        return FakeResponse(DETAIL_HTML), None

    monkeypatch.setattr(sc, "fetch_upwork", fake_fetch_upwork)

    job = _card_job()

    result = asyncio.run(
        sc.detail_worker(None, job, None)
    )

    assert result["DescriptionSource"] == "detail_jsonld"
    assert result["DatePosted"] == "2026-11-12T14:51:39.738Z"


# ================================================================
# Blocked detail: keep the card description
# ================================================================

def test_blocked_detail_keeps_card_description_without_detail_error():
    job = _card_job()

    result = sc.handle_upwork_detail_failure(
        job, "HTTP 403 (blocked/login-walled)"
    )

    assert result["DetailBlocked"] is True
    assert result["Description"] == "Build the thing."
    assert result["DescriptionSource"] == "search_card"
    assert "DetailError" not in result
    assert "DetailNote" in result


def test_output_keeps_described_blocked_job_but_drops_empty_failures():
    blocked_with_card = _card_job()
    sc.handle_upwork_detail_failure(
        blocked_with_card, "HTTP 403 (blocked/login-walled)"
    )

    blocked_without_card = _card_job(Description="", DescriptionSource="")
    sc.handle_upwork_detail_failure(
        blocked_without_card, "HTTP 403 (blocked/login-walled)"
    )

    errored_without_card = _card_job(Description="", DescriptionSource="")
    errored_without_card["DetailError"] = "HTTP 500"

    output = sc.to_output_schema(
        [blocked_with_card, blocked_without_card, errored_without_card]
    )

    assert len(output) == 1
    assert output[0]["source"] == "Upwork"
    assert output[0]["description"] == "Build the thing."
