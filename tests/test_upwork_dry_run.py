"""Offline regression tests for the standalone Upwork dry run.

These do not touch the network and do not import scraper.py (which
eagerly imports scrapling.fetchers -> Playwright). They cover the parts
that were verified against live Upwork markup:

  * 50-card pages and the t=0/t=1 job-type mapping
  * fixed-price vs hourly card shapes (hourly puts the rate range in the
    job-type label, not a separate budget node)
  * "jobs found" / rel=next pagination hints
  * 429 backoff and 403 blocked detection
  * detail parsing with no raw-page-as-description fallback

Run with: pytest tests/test_upwork_dry_run.py -v
"""

import asyncio
import json

from scrapling.parser import Selector

from scraper import upwork_dry_run as up


class FakeResponse:

    def __init__(self, html, status=200, url="https://www.upwork.com/nx/search/jobs/"):

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


class FakeRaw:

    def __init__(self, status_code, content=b"", url="https://www.upwork.com/", headers=None):

        self.status_code = status_code
        self.content = content
        self.url = url
        self.headers = headers or {}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeSession:

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


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
  <button class="air3-token" data-test="token"><span>React</span></button>
</article>
"""

HOURLY_CARD = """
<article class="job-tile" data-ev-job-uid="2120880945184806394">
  <small data-test="job-pubilshed-date"><span>Posted 2 hours ago</span></small>
  <h2 class="job-tile-title">
    <a data-test="job-tile-title-link"
       href="/jobs/Hourly-Gig_~022120880945184806394/">
      <span>Hourly Gig</span>
    </a>
  </h2>
  <ul class="job-tile-info-list">
    <li data-test="job-type-label"><strong>Hourly: $20.00 - $40.00</strong></li>
    <li data-test="experience-level"><strong>Expert</strong></li>
    <li>Est. time: 1 to 3 months, Less than 30 hrs/week</li>
  </ul>
  <div class="air3-line-clamp-wrapper">
    <div class="air3-line-clamp"><p class="body-medium">Hourly work.</p></div>
  </div>
</article>
"""


def _search_page(cards, total="597", next_href="/nx/search/jobs/?q=python&page=2"):
    return (
        "<html><head>"
        f'<link rel="next" href="{next_href}">'
        "</head><body>"
        f"{cards}"
        f"<div>{total} jobs found</div>"
        "</body></html>"
    )


# ================================================================
# URL building / limits
# ================================================================

def test_search_url_defaults_to_all_job_types():
    url = up.build_upwork_search_url(keyword="python", page=1, per_page=50)

    assert "q=python" in url
    assert "sort=recency" in url
    assert "per_page=50" in url
    # t is a job-type filter; omitting it is what keeps hourly + fixed.
    assert "&t=" not in url
    assert "page=1" not in url


def test_search_url_job_type_mapping_and_page_size_clamp():
    hourly = up.build_upwork_search_url(job_type="hourly", per_page=100)
    fixed = up.build_upwork_search_url(job_type="fixed", per_page=10)

    assert "t=0" in hourly
    assert "per_page=50" in hourly  # clamped
    assert "t=1" in fixed
    assert "per_page=10" in fixed


# ================================================================
# Card extraction (fixed + hourly)
# ================================================================

def test_extract_fixed_price_card():
    response = FakeResponse(_search_page(FIXED_CARD))
    jobs = up.extract_upwork_jobs(response)

    assert len(jobs) == 1
    job = jobs[0]

    assert job["Platform"] == "Upwork"
    assert job["Title"] == "Foo Python Bar"
    assert job["Company"] == "N/A"
    assert job["Location"] == up.UPWORK_LOCATION
    assert job["Posted"] == "Posted 39 minutes ago"
    assert job["JobType"] == "Fixed price"
    assert job["ExperienceLevel"] == "Intermediate"
    assert job["Budget"] == "Est. budget: $1,500.00"
    assert job["Description"] == "Build the thing."
    assert job["Skills"] == ["Python", "React"]
    assert job["JobID"] == "022120881892029947726"
    assert job["CardUID"] == "2120881892029947726"
    # Tracking query is stripped.
    assert job["Link"].endswith("_~022120881892029947726")
    assert "referrer_url_path" not in job["Link"]


def test_extract_hourly_card_normalises_type_and_budget():
    response = FakeResponse(_search_page(HOURLY_CARD))
    job = up.extract_upwork_jobs(response)[0]

    assert job["JobType"] == "Hourly"
    assert job["Budget"] == "$20.00 - $40.00/hr"
    assert job["Duration"] == "Est. time: 1 to 3 months, Less than 30 hrs/week"
    assert job["Description"] == "Hourly work."


def test_extract_total_and_next_page():
    response = FakeResponse(_search_page(FIXED_CARD))

    assert up.extract_upwork_total(response) == 597
    assert up.extract_upwork_next_url(response) == (
        "https://www.upwork.com/nx/search/jobs/?q=python&page=2"
    )


# ================================================================
# Rate limit / blocked handling
# ================================================================

def test_fetch_retries_429_then_succeeds():
    session = FakeSession(
        [
            FakeRaw(429, headers={"Retry-After": "0"}),
            FakeRaw(200, content=b"<html>ok</html>"),
        ]
    )

    response, error = asyncio.run(
        up.fetch_upwork(session, "https://www.upwork.com/x", asyncio.Semaphore(1))
    )

    assert error is None
    assert response is not None
    assert response.status == 200
    assert session.calls == 2


def test_fetch_403_is_reported_as_blocked():
    session = FakeSession([FakeRaw(403, content=b"<html>nope</html>")])

    response, error = asyncio.run(
        up.fetch_upwork(session, "https://www.upwork.com/x", asyncio.Semaphore(1))
    )

    assert response is None
    assert "blocked" in error.lower()


def test_detail_403_keeps_card_description_without_detail_error():
    job = {"Description": "Full card description.", "Link": "https://upwork.invalid/x"}

    up.handle_upwork_detail_failure(job, "HTTP 403 (blocked/login-walled)")

    assert job["DetailBlocked"] is True
    assert job["Description"] == "Full card description."
    assert job["DescriptionSource"] == "search_card"
    assert "DetailError" not in job
    assert "DetailNote" in job


# ================================================================
# Detail extraction: structured only, no raw-page fallback
# ================================================================

def test_parse_detail_uses_jsonld_when_present():
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Detail Title",
        "description": "<p>Detail description.</p>",
        "hiringOrganization": {"name": "Acme"},
    }
    html = (
        '<script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script><html><body>nav noise</body></html>"
    )

    job = {"Description": "Card description."}

    result = up.parse_upwork_detail(job, FakeResponse(html, status=200))

    assert result["Description"] == "Detail description."
    assert result["DescriptionSource"] == "detail_jsonld"
    assert result["Title"] == "Detail Title"
    assert result["Company"] == "Acme"


def test_parse_detail_ignores_upwork_as_hiring_org_and_captures_client_data():
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Detail Title",
        "description": "<p>Detail description.</p>",
        "datePosted": "2026-11-12T14:51:39.738Z",
        # Confirmed live: Upwork always names itself here, not the client.
        "hiringOrganization": {"name": "Upwork"},
    }
    html = (
        '<script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script>"
        "<ul class=\"ac-items\">"
        "<li data-qa=\"client-location\">"
        "<strong>United States</strong>"
        "<div><span>9:08 AM</span></div>"
        "</li></ul>"
    )

    job = {"Description": "Card description.", "Company": "N/A"}

    result = up.parse_upwork_detail(job, FakeResponse(html, status=200))

    assert result["Company"] == "N/A"
    assert result["DatePosted"] == "2026-11-12T14:51:39.738Z"
    assert result["ClientLocation"] == "United States"


def test_parse_detail_never_uses_the_raw_page_as_description():
    noisy_page = "<html><body>" + ("<div>filler</div>" * 5000) + "</body></html>"

    job = {"Description": "Full card description."}

    result = up.parse_upwork_detail(job, FakeResponse(noisy_page, status=200))

    assert result["Description"] == "Full card description."
    assert result["DescriptionSource"] == "search_card"
    assert "DetailError" not in result
    assert "DetailNote" in result
    assert "filler" not in result["Description"]
