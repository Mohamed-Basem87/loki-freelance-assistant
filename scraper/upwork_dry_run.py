"""Standalone Upwork dry-run scraper.

This is a *test harness*, not a wired-in source. It intentionally lives
beside scraper.py and does not touch it, the app adapters, config, or
jobs_results.json. It mirrors the guardrails the LinkedIn/Wuzzuf scraper
already applies:

  * bounded discovery (per-page size, max pages, concurrency, pacing)
  * bounded detail work (max per run, its own concurrency and pacing)
  * HTTP 429/503 handling with exponential backoff + jitter, honouring
    Retry-After when the server sends it
  * explicit login-wall / Cloudflare-challenge detection
  * structured extraction only - no raw-page-as-description fallback
  * URL-level dedupe and a separate cross-run "seen" sidecar

What is different for Upwork, and why
-------------------------------------
Upwork's public search result cards contain the *entire* job description
(confirmed on 50/50 sampled cards; many were 1,500-8,000+ chars and ended
on a complete sentence). Unlike Wuzzuf, that text is a real structured
field of the result card, not a whole-page dump, so it is safe to use as
the description even when a detail fetch is unavailable.

Detail pages are *intermittently* accessible to guests. A normal guest
request to /jobs/... redirects to /freelance-jobs/apply/..., which in
live sampling returned HTTP 200 with JobPosting JSON-LD 4 times out of 5
and HTTP 403 on the fifth (and 403 for every probe of one older job).
The success case gives a real detail description, a structured
datePosted, and the client's country; the 403 case is recorded as
DetailBlocked and the full list-card description is kept. Because the
outcome is not reliable, detail fetching defaults OFF and is bounded by
--detail-max.

Upwork's `t` query parameter is a JOB TYPE filter, not a date-posted
filter (t=0 -> hourly, t=1 -> fixed price, omit -> both). The guest
search exposes no "date posted" facet at all, so the closest available
analogue to LinkedIn's 24h window is sort=recency plus the parsed
"Posted ..." label on each card. This module does not pretend otherwise.


Compliance note
---------------
Upwork's robots.txt disallows /nx/ and /jobs/, and Upwork's own llms.txt
tells automated clients to avoid dynamic search result pages and
login-gated pages. The guest search page is technically reachable, but
production use should go through Upwork's official API / partner feed or
be explicitly cleared by Upwork. This script keeps a dry run tiny (two
search pages + at most a couple of detail probes) and backs off on 429s.

Usage (from the repository root):

    python -m scraper.upwork_dry_run --keyword python --max-pages 2
    python -m scraper.upwork_dry_run --keyword python --max-pages 1 \
        --fetch-details --detail-max 1

Outputs, all under scraper/ by default:

    upwork_dry_run_results.json   same record shape as jobs_results.json
    upwork_dry_run_report.json    discovery/detail diagnostics
    upwork_dry_run_seen.json      cross-run seen keys (separate sidecar)
"""

import argparse
import asyncio
import html as html_module
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

# Allow both `python -m scraper.upwork_dry_run` and
# `python scraper/upwork_dry_run.py`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from curl_cffi.requests import AsyncSession as CurlCffiAsyncSession
from scrapling.parser import Selector


# ============================================================
# SHARED CONFIG / HELPERS
# ============================================================
#
# scraper.py imports scrapling.fetchers at module import time, and that
# pulls in Playwright. This dry run must be runnable in a plain
# requirements.txt environment (no browser), so it keeps local copies of
# the handful of pure helpers it needs instead of importing scraper.py.
# They are intentionally identical to the versions in scraper.py; if this
# ever gets wired in for real, move them into a shared module rather than
# letting the copies drift.

BACKOFF_429 = 4
HTTP_TIMEOUT = 20
MAX_429_RETRIES = 3
MAX_STORED_DESCRIPTION_CHARS = 200_000

# Mirrors scraper.py's default. None = ALL jobs.
SHARED_KEYWORD = None


def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def get_attr(node, name):
    try:
        if hasattr(node, "attrib"):
            value = node.attrib.get(name)

            if value:
                return value
    except Exception:
        pass

    try:
        if hasattr(node, "attrs"):
            value = node.attrs.get(name)

            if value:
                return value
    except Exception:
        pass

    return None


def strip_html_tags(value):

    if not value:
        return ""

    text = str(value)

    text = html_module.unescape(text)

    text = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    return clean_text(text)


def extract_jsonld_jobposting(response):

    try:
        script_nodes = response.css(
            "script[type='application/ld+json']"
        )
    except Exception:
        return None

    if not script_nodes:
        return None

    for node in script_nodes:

        try:
            raw = node.text
        except Exception:
            continue

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]

        expanded = []

        for item in candidates:

            if isinstance(item, dict) and "@graph" in item:
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)

        for item in expanded:

            if not isinstance(item, dict):
                continue

            item_type = item.get("@type", "")

            is_job = (
                "JobPosting" in item_type
                if isinstance(item_type, list)
                else item_type == "JobPosting"
            )

            if not is_job:
                continue

            result = {}

            if item.get("title"):
                result["title"] = clean_text(item["title"])

            if item.get("description"):
                result["description"] = strip_html_tags(
                    item["description"]
                )

            if item.get("datePosted"):
                result["datePosted"] = clean_text(
                    item["datePosted"]
                )

            org = item.get("hiringOrganization")

            if isinstance(org, dict) and org.get("name"):
                result["company"] = clean_text(org["name"])

            if result:
                return result

    return None


def canonical_url(url):

    if not url:
        return ""

    try:

        parsed = urlparse(url)

        if not parsed.scheme:
            return ""

        if not parsed.netloc:
            return ""

        return (
            f"{parsed.scheme}://"
            f"{parsed.netloc}"
            f"{parsed.path.rstrip('/')}"
        )

    except Exception:

        return ""


def absolute_url(base, href):

    if not href:
        return ""

    return urljoin(base, href)


def deduplicate_jobs(jobs):

    unique = []

    seen = set()

    for job in jobs:

        platform = job.get("Platform", "")

        link = canonical_url(
            job.get("Link", "")
        )

        if not link:
            continue

        key = (platform, link)

        if key in seen:
            continue

        seen.add(key)

        job["Link"] = link

        unique.append(job)

    return unique


def load_seen_keys(filename):

    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as file:

            return set(json.load(file))

    except (FileNotFoundError, json.JSONDecodeError):

        return set()


def save_seen_keys(keys, filename):

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            sorted(keys),
            file,
            ensure_ascii=False,
            indent=2
        )


def report_new_jobs(jobs, seen_keys):

    current_keys = {
        f"{job.get('Platform', '')}|{job.get('Link', '')}"
        for job in jobs
        if job.get("Link")
    }

    new_keys = current_keys - seen_keys

    print(
        f"[Cross-run tracking] "
        f"New vs. previous polls: "
        f"{len(new_keys)}/{len(current_keys)}"
    )

    return len(new_keys), seen_keys | current_keys


def build_output_record(job):
    """Same output shape as scraper.py's build_output_record()."""

    title = clean_text(job.get("Title", ""))

    description = strip_html_tags(
        job.get("Description", "")
    )[:MAX_STORED_DESCRIPTION_CHARS]

    company = clean_text(job.get("Company", ""))

    if title and description:
        raw_text = f"{title}\n\n{description}"
    else:
        raw_text = title or description

    return {
        "title": title,
        "description": description,
        "raw_text": raw_text,
        "source": job.get("Platform", ""),
        "url": job.get("Link", ""),
        "company": company,
    }


# ============================================================
# CONFIG - Upwork dry run
# ============================================================

UPWORK_BASE = "https://www.upwork.com"
UPWORK_SEARCH_ENDPOINT = f"{UPWORK_BASE}/nx/search/jobs/"

# Confirmed: per_page=50 returns 50 cards. per_page=100 silently falls
# back to 10, so 50 is the usable guest maximum.
UPWORK_PER_PAGE = 50

# Keep the dry run small. None = every page the result count implies.
MAX_UPWORK_PAGES = 2

# None = all jobs. This intentionally follows the shared scraper config
# so a dry run can mirror the real keyword, but can be overridden with
# --keyword.
UPWORK_KEYWORD = SHARED_KEYWORD

# Guest search has no meaningful country filter, so Upwork's default is
# a remote/global result set. The card itself does not expose a client
# location publicly.
UPWORK_LOCATION = "Remote"

# Newest first. Confirmed to be honoured by the guest search endpoint.
UPWORK_SORT_BY = "recency"

# IMPORTANT: Upwork's `t` query param is the JOB TYPE filter, not a
# date-posted filter. Confirmed against the live guest search:
#   t=0 -> 1,115 hourly jobs
#   t=1 ->   597 fixed-price jobs
#   omit -> 1,712 total (exactly 1,115 + 597)
# The old LinkedIn-style assumption that t=1 meant "last 24h" was wrong
# and silently restricted the dry run to fixed-price work. The guest
# search also exposes no date-posted facet; sort=recency is the only
# temporal control, and the parsed "Posted ..." labels are reported so
# staleness can be measured client-side.
UPWORK_JOB_TYPE = None  # None = all; "hourly" = t=0; "fixed" = t=1

# Discovery is deliberately gentle. Search pages are cheap but Upwork is
# Cloudflare-fronted, so keep both concurrency and pacing conservative.
UPWORK_SEARCH_CONCURRENCY = 2
UPWORK_SEARCH_PACING = 1.0

# Detail requests are the expensive/blocked path (guest detail = 403).
UPWORK_DETAIL_CONCURRENCY = 1
UPWORK_DETAIL_PACING = 1.5
UPWORK_DETAIL_MAX_PER_RUN = 2

# Default OFF: detail pages are login-walled for guests. Enable with
# --fetch-details to verify the blocked-detection path.
UPWORK_FETCH_DETAILS = False

# Clamp server-provided Retry-After so a bad header cannot park a request
# for an hour.
UPWORK_MAX_BACKOFF_SECONDS = 120

UPWORK_OUTPUT_FILE = Path(__file__).with_name("upwork_dry_run_results.json")
UPWORK_REPORT_FILE = Path(__file__).with_name("upwork_dry_run_report.json")
UPWORK_SEEN_FILE = Path(__file__).with_name("upwork_dry_run_seen.json")


# Markers that indicate a bot challenge / block page rather than real
# content. Kept specific: the Upwork search page legitimately contains
# "Log in" and "Sign up" in its navigation.
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl",
    "enable javascript and cookies",
    "verify you are human",
    "checking your browser",
    "unusual traffic",
    "access denied",
    "attention required",
    # NOTE: the bare word "captcha" is intentionally absent. Upwork's
    # normal search page embeds googleRecaptchaEnterpriseSiteKey in its
    # Nuxt config, so matching that word marked every real page as a
    # challenge. The phrases above do not appear on a normal page.
)

# Detail-specific login-wall markers. Only applied to detail responses,
# never to search pages (whose nav always mentions logging in).
DETAIL_BLOCK_MARKERS = (
    "sign in to view",
    "log in to view",
    "join upwork to see",
    "sign up to view",
    "login required",
    "authwall",
    "this job is no longer available",
    "this job has been closed",
)


# ============================================================
# GENERIC HELPERS
# ============================================================

def _first_text(nodes):
    if not nodes:
        return ""
    return clean_text(nodes[0].get_all_text())


def _looks_challenged(text):
    lower = (text or "").lower()
    return any(marker in lower for marker in CHALLENGE_MARKERS)


def _parse_retry_after(value):
    """Return a sane number of seconds from a Retry-After header, or None."""

    if value is None:
        return None

    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None

    if seconds < 0:
        return None

    return min(seconds, UPWORK_MAX_BACKOFF_SECONDS)


# ============================================================
# RESPONSE WRAPPER
# ============================================================
#
# Same approach as scraper.py's _LinkedInResponse: give curl_cffi's
# response the small surface the extraction helpers need. Text is decoded
# explicitly as UTF-8 because curl_cffi/requests otherwise falls back to
# ISO-8859-1 and turns Upwork's en-dashes/bullets into mojibake.

class _UpworkResponse:

    __slots__ = (
        "status",
        "url",
        "text",
        "body",
        "headers",
        "_selector",
    )

    def __init__(self, raw):

        self.status = raw.status_code
        self.url = str(raw.url)
        self.body = raw.content or b""
        self.headers = raw.headers
        self._selector = None

        try:
            self.text = self.body.decode("utf-8", "replace")
        except Exception:
            try:
                self.text = raw.text or ""
            except Exception:
                self.text = ""

    def css(self, query):

        if self._selector is None:

            self._selector = Selector(
                content=self.text or "",
                url=self.url,
            )

        return self._selector.css(query)

    @property
    def looks_challenged(self):
        return _looks_challenged(self.text)


# ============================================================
# RATE-LIMITED HTTP
# ============================================================

async def fetch_upwork(
    session,
    url,
    semaphore,
    pacing=0.0,
):
    """Fetch one Upwork URL with the same guardrails as the other sources.

    Returns (response, error). A 429/503 is retried with the shared
    exponential backoff + jitter (and Retry-After when present). A
    Cloudflare challenge, 401, 403 or 999 is terminal for this request
    and reported as a blocked/login-walled failure.
    """

    async with semaphore:

        if pacing:

            await asyncio.sleep(pacing)

        retry_429 = 0

        while True:

            try:

                raw = await session.get(
                    url,
                    impersonate="chrome",
                    timeout=HTTP_TIMEOUT,
                    allow_redirects=True,
                )

            except Exception as exc:

                return (
                    None,
                    f"{type(exc).__name__}: {exc}",
                )

            response = _UpworkResponse(raw)

            if response.status == 200 and not response.looks_challenged:

                return response, None

            if response.status == 200 and response.looks_challenged:

                return (
                    None,
                    "HTTP 200 but Cloudflare/bot-challenge page",
                )

            if response.status in (429, 503):

                if retry_429 >= MAX_429_RETRIES:

                    return (
                        None,
                        f"HTTP {response.status} after "
                        f"{MAX_429_RETRIES} retries",
                    )

                retry_429 += 1

                wait_time = _parse_retry_after(
                    response.headers.get("Retry-After")
                    if response.headers
                    else None
                )

                if wait_time is None:

                    wait_time = (
                        BACKOFF_429
                        * (2 ** (retry_429 - 1))
                        + random.uniform(0, 1)
                    )

                wait_time = min(
                    wait_time,
                    UPWORK_MAX_BACKOFF_SECONDS,
                )

                print(
                    f"[Upwork] {response.status} → waiting "
                    f"{wait_time:.1f}s "
                    f"(retry {retry_429}/{MAX_429_RETRIES})"
                )

                await asyncio.sleep(wait_time)

                continue

            if response.status in (401, 403, 999):

                return (
                    None,
                    f"HTTP {response.status} (blocked/login-walled)",
                )

            return (
                None,
                f"HTTP {response.status}",
            )


# ============================================================
# URL BUILDING
# ============================================================

def build_upwork_search_url(
    keyword=None,
    page=1,
    per_page=UPWORK_PER_PAGE,
    job_type=UPWORK_JOB_TYPE,
):
    """Build a guest search URL.

    Upwork's page size is capped at 50 even though the query accepts
    larger numbers (larger values silently fall back to 10).

    job_type: None = all, "hourly" -> t=0, "fixed" -> t=1.
    """

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = UPWORK_PER_PAGE

    per_page = max(
        1,
        min(per_page, UPWORK_PER_PAGE),
    )

    params = []

    if keyword:

        params.append(
            f"q={quote(str(keyword))}"
        )

    params.append(
        f"sort={UPWORK_SORT_BY}"
    )

    job_type = (
        str(job_type).strip().lower()
        if job_type
        else ""
    )

    if job_type == "hourly":

        params.append("t=0")

    elif job_type == "fixed":

        params.append("t=1")

    params.append(
        f"per_page={per_page}"
    )

    if page > 1:

        params.append(
            f"page={int(page)}"
        )

    return (
        f"{UPWORK_SEARCH_ENDPOINT}"
        f"?{'&'.join(params)}"
    )


def extract_upwork_job_id(url):
    """Extract the URL id from ..._~022120881892029947726.

    Kept with its leading zero(es) so it can rebuild the canonical detail
    URL if a future accepted path needs it. The card's data-ev-job-uid is
    a different tracking id, so it is not used as JobID.
    """

    if not url:
        return ""

    match = re.search(
        r"~(\d+)",
        url,
    )

    if match:
        return match.group(1)

    return ""


def upwork_detail_url(job_id):
    return f"{UPWORK_BASE}/jobs/~{job_id}"


# ============================================================
# SEARCH EXTRACTION
# ============================================================

def extract_upwork_total(response):

    try:
        text = response.text or ""
    except Exception:
        return None

    match = re.search(
        r"([\d,]+)\s+jobs?\s+found",
        text,
        re.I,
    )

    if match:

        try:
            return int(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            pass

    return None


def extract_upwork_next_url(response):

    nodes = response.css(
        "link[rel='next']"
    ) or response.css(
        "a[rel='next']"
    )

    if not nodes:
        return ""

    href = get_attr(nodes[0], "href")

    if not href:
        return ""

    return absolute_url(
        UPWORK_BASE,
        href,
    )


def extract_upwork_jobs(response):
    """Extract one page of job cards.

    The card description is a structured field of the result card, not a
    raw page dump. It is stored in Description so the dry run can produce
    the same output shape as the existing scraper. CardText remains a
    diagnostic only.
    """

    cards = response.css(
        "article.job-tile"
    )

    jobs = []

    for card in cards:

        # ----------------------------------------------------
        # TITLE + LINK
        # ----------------------------------------------------

        title_nodes = (
            card.css(
                "[data-test='job-tile-title-link']"
            )
            or card.css(
                "h2.job-tile-title a"
            )
            or card.css(
                "h2 a[href*='/jobs/']"
            )
        )

        title = _first_text(title_nodes)

        if not title:
            continue

        link = ""

        if title_nodes:

            href = get_attr(
                title_nodes[0],
                "href",
            )

            if href:

                link = canonical_url(
                    absolute_url(
                        UPWORK_BASE,
                        href,
                    )
                )

        if not link:

            for node in card.css(
                "a[href*='/jobs/']"
            ):

                href = get_attr(node, "href")

                if href:

                    link = canonical_url(
                        absolute_url(
                            UPWORK_BASE,
                            href,
                        )
                    )

                    break

        if not link:
            continue

        # ----------------------------------------------------
        # COMPANY / LOCATION
        # ----------------------------------------------------
        # Guest search does not expose the client name or country
        # publicly. Keep explicit placeholders instead of fabricating
        # values; the report notes this limitation.

        company = _first_text(
            card.css(
                "[data-test='client-name'], "
                "[data-test='client-company']"
            )
        ) or "N/A"

        location = _first_text(
            card.css(
                "[data-test='client-location'], "
                "[data-test='job-location']"
            )
        ) or UPWORK_LOCATION

        # ----------------------------------------------------
        # POSTED / TYPE / EXPERIENCE / BUDGET
        # ----------------------------------------------------

        posted = _first_text(
            card.css(
                "[data-test='job-pubilshed-date'], "
                "[data-test='job-published-date']"
            )
        ) or "N/A"

        job_type_raw = _first_text(
            card.css(
                "[data-test='job-type-label']"
            )
        )

        experience = _first_text(
            card.css(
                "[data-test='experience-level']"
            )
        )

        # Hourly cards put the rate range inside the job-type label
        # ("Hourly: $12.00 - $28.00") and have no separate budget node.
        # Fixed-price cards keep "Fixed price" there and put the budget
        # in [data-test='is-fixed-price']. Normalise both so JobType is
        # just "Hourly"/"Fixed price" and Budget is the actual money.
        job_type = job_type_raw

        hourly_budget = ""

        lowered_type = job_type_raw.lower()

        if lowered_type.startswith("hourly"):

            job_type = "Hourly"

            _, _, hourly_budget = job_type_raw.partition(":")

            hourly_budget = clean_text(hourly_budget)

        elif lowered_type.startswith("fixed"):

            job_type = "Fixed price"

        budget = (
            _first_text(
                card.css(
                    "[data-test='is-fixed-price']"
                )
            )
            or hourly_budget
        )

        if budget and job_type == "Hourly" and "/hr" not in budget.lower():

            budget = f"{budget}/hr"

        # The third info item on hourly cards is "Est. time: ...", not
        # a budget. Keep it separately for diagnostics.
        duration = ""

        for item in card.css(
            "ul.job-tile-info-list li"
        ):

            text = clean_text(
                item.get_all_text()
            )

            if text.lower().startswith("est. time"):

                duration = text

                break

        # ----------------------------------------------------
        # DESCRIPTION (structured card field)
        # ----------------------------------------------------

        description_nodes = (
            card.css(
                "div.air3-line-clamp p.body-medium"
            )
            or card.css(
                "p.body-medium"
            )
        )

        description = strip_html_tags(
            _first_text(description_nodes)
        )

        # ----------------------------------------------------
        # SKILLS
        # ----------------------------------------------------

        skills = []

        seen_skills = set()

        for node in card.css(
            "button.air3-token, [data-test='token']"
        ):

            skill = clean_text(
                node.get_all_text()
            )

            if skill and skill not in seen_skills:

                seen_skills.add(skill)

                skills.append(skill)

        # ----------------------------------------------------
        # CARD TEXT (diagnostic only - never a description)
        # ----------------------------------------------------

        card_text = clean_text(
            card.get_all_text()
        )

        jobs.append({

            "Platform":
                "Upwork",

            "Title":
                title,

            "Company":
                company,

            "Location":
                location,

            "Posted":
                posted,

            "Link":
                link,

            "JobID":
                extract_upwork_job_id(link),

            "CardUID":
                get_attr(card, "data-ev-job-uid") or "",

            "JobType":
                job_type,

            "ExperienceLevel":
                experience,

            "Budget":
                budget,

            "Duration":
                duration,

            "Skills":
                skills,

            "Description":
                description,

            "DescriptionSource":
                "search_card" if description else "",

            "CardText":
                card_text,
        })

    return jobs


# ============================================================
# SEARCH DISCOVERY
# ============================================================

async def fetch_upwork_search_page(
    session,
    semaphore,
    page,
    keyword,
    per_page,
    job_type,
):

    url = build_upwork_search_url(
        keyword=keyword,
        page=page,
        per_page=per_page,
        job_type=job_type,
    )

    response, error = await fetch_upwork(
        session,
        url,
        semaphore,
        pacing=UPWORK_SEARCH_PACING,
    )

    if response is None:

        return {
            "page": page,
            "url": url,
            "status": None,
            "error": error,
            "jobs": [],
            "total": None,
            "next": "",
        }

    return {
        "page": page,
        "url": response.url,
        "status": response.status,
        "error": None,
        "jobs": extract_upwork_jobs(response),
        "total": extract_upwork_total(response),
        "next": extract_upwork_next_url(response),
    }


async def scrape_upwork(
    session,
    keyword=None,
    max_pages=MAX_UPWORK_PAGES,
    per_page=UPWORK_PER_PAGE,
    job_type=UPWORK_JOB_TYPE,
):
    """Discover jobs, bounded by max_pages and the search guardrails."""

    print("\n" + "=" * 70)
    print("UPWORK SEARCH (DRY RUN)")
    print("=" * 70)

    all_jobs = []

    seen_urls = set()

    seen_signatures = set()

    page_results = []

    semaphore = asyncio.Semaphore(
        UPWORK_SEARCH_CONCURRENCY
    )

    first = await fetch_upwork_search_page(
        session,
        semaphore,
        page=1,
        keyword=keyword,
        per_page=per_page,
        job_type=job_type,
    )

    page_results.append(first)

    print(
        f"[Upwork] Page 1 | "
        f"jobs={len(first['jobs'])} | "
        f"status={first['status']}"
    )

    if first["error"]:

        print(
            f"[Upwork] Page 1 failed: "
            f"{first['error']}"
        )

    total = first["total"]

    if total:

        print(
            f"[Upwork] Reported total: "
            f"{total}"
        )

    print(
        "[Upwork] Note: guest search has no date-posted filter; "
        "sort=recency is the only temporal control."
    )

    # --------------------------------------------------------
    # PAGE COUNT
    # --------------------------------------------------------

    if total:

        pages_planned = ceil(
            total / max(1, int(per_page))
        )

    else:

        pages_planned = max_pages or 1

    if max_pages is not None:

        pages_planned = min(
            pages_planned,
            max_pages,
        )

    if not first["next"] and not total:

        pages_planned = 1

    pages_planned = max(1, pages_planned)

    print(
        f"[Upwork] Pages planned: "
        f"{pages_planned}"
    )

    if first["next"] and total:

        implied = ceil(
            total / max(1, int(per_page))
        )

        if pages_planned < implied:

            print(
                f"[Upwork] Capped at max_pages="
                f"{pages_planned} of {implied} implied pages."
            )

    # --------------------------------------------------------
    # REMAINING PAGES IN PARALLEL (small, gated pool)
    # --------------------------------------------------------

    if pages_planned > 1:

        tasks = [
            asyncio.create_task(
                fetch_upwork_search_page(
                    session,
                    semaphore,
                    page=page,
                    keyword=keyword,
                    per_page=per_page,
                    job_type=job_type,
                )
            )
            for page in range(2, pages_planned + 1)
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        for result in results:

            if isinstance(result, Exception):

                print(
                    f"[Upwork] Page worker failed: "
                    f"{result}"
                )

                continue

            page_results.append(result)

    # --------------------------------------------------------
    # AGGREGATE (preserve page order, dedupe by link)
    # --------------------------------------------------------

    for result in page_results:

        page = result["page"]

        jobs = result["jobs"]

        if result["error"]:

            print(
                f"[Upwork] Page {page} failed: "
                f"{result['error']}"
            )

            continue

        signature = tuple(
            sorted(
                job["Link"]
                for job in jobs
                if job.get("Link")
            )
        )

        if signature and signature in seen_signatures:

            print(
                f"[Upwork] Page {page} repeated; "
                "skipping."
            )

            continue

        seen_signatures.add(signature)

        new_count = 0

        for job in jobs:

            link = job["Link"]

            if link in seen_urls:
                continue

            seen_urls.add(link)

            all_jobs.append(job)

            new_count += 1

        if page != 1:

            print(
                f"[Upwork] Page {page} | "
                f"jobs={len(jobs)} | "
                f"new={new_count} | "
                f"status={result['status']}"
            )

    print(
        f"\n[Upwork] FINAL UNIQUE: "
        f"{len(all_jobs)}"
    )

    search_meta = {
        "keyword": keyword,
        "job_type": job_type,
        "per_page": int(per_page),
        "total_reported": total,
        "pages_planned": pages_planned,
        "pages_fetched": len(page_results),
        "pages": [
            {
                "page": result["page"],
                "status": result["status"],
                "jobs": len(result["jobs"]),
                "total": result["total"],
                "has_next": bool(result["next"]),
                "error": result["error"],
            }
            for result in page_results
        ],
    }

    return all_jobs, search_meta


# ============================================================
# DETAIL FETCH + EXTRACTION
# ============================================================

def _keep_card_description(job, note):

    if job.get("Description"):

        job["DescriptionSource"] = "search_card"

        job["DetailNote"] = note

    else:

        job["DetailError"] = note


def handle_upwork_detail_failure(job, error):

    job["DetailStatus"] = None

    job["DetailFetcher"] = "HTTP"

    lowered = (error or "").lower()

    if (
        "403" in lowered
        or "401" in lowered
        or "999" in lowered
        or "blocked" in lowered
        or "login" in lowered
        or "challenge" in lowered
    ):

        job["DetailBlocked"] = True

        _keep_card_description(
            job,
            f"Detail fetch blocked ({error}); "
            "keeping the full list-card description.",
        )

    else:

        _keep_card_description(
            job,
            f"Detail fetch failed ({error}); "
            "keeping the full list-card description.",
        )

    return job


def parse_upwork_detail(job, response):
    """Structured detail extraction only.

    Tier 1: JobPosting JSON-LD.
    Tier 2: known Upwork description containers.
    No tier 3: the raw page is never used as a description.
    """

    try:

        description = ""

        source = ""

        structured = extract_jsonld_jobposting(response)

        if structured and structured.get("description"):

            description = structured["description"]

            source = "detail_jsonld"

            if structured.get("title"):
                job["Title"] = structured["title"]

            # Upwork's JobPosting always names Upwork itself as the
            # hiringOrganization, not the real client. Never copy that
            # into Company; leave the list-card placeholder (N/A) alone.
            company = structured.get("company") or ""

            if company and company.strip().lower() != "upwork":
                job["Company"] = company

            if structured.get("datePosted"):
                job["DatePosted"] = structured["datePosted"]

        else:

            nodes = response.css(
                "[data-test='job-description'], "
                ".job-description, "
                "section[class*='job-description']"
            )

            if nodes:

                description = strip_html_tags(
                    nodes[0].get_all_text()
                )

                if description:
                    source = "detail_dom"

        # Client location is only exposed on the detail page (the search
        # card does not reveal it). Keep it as its own diagnostic rather
        # than overwriting the card's placeholder Location.
        location_nodes = (
            response.css(
                "[data-qa='client-location'] strong"
            )
            or response.css(
                "[data-qa='client-location']"
            )
        )

        if location_nodes:

            client_location = _first_text(location_nodes)

            # The bare <li> also contains the client's local time
            # ("United States 9:08 AM"); drop that trailing clock.
            client_location = re.sub(
                r"\s+\d{1,2}:\d{2}\s*(AM|PM)\s*$",
                "",
                client_location,
                flags=re.IGNORECASE,
            ).strip()

            if client_location:
                job["ClientLocation"] = client_location

        job["DetailStatus"] = response.status

        job["DetailFetcher"] = "HTTP"

        if description:

            job["Description"] = description

            job["DescriptionSource"] = source

            job.pop("DetailBlocked", None)
            job.pop("DetailError", None)
            job.pop("DetailNote", None)

            return job

        lower = (response.text or "").lower()

        blocked = any(
            marker in lower
            for marker in DETAIL_BLOCK_MARKERS
        )

        if blocked:

            job["DetailBlocked"] = True

            _keep_card_description(
                job,
                "Detail page is login-walled/blocked for guests; "
                "keeping the full list-card description.",
            )

        else:

            _keep_card_description(
                job,
                "Detail extraction found no structured description; "
                "keeping the full list-card description.",
            )

        return job

    except Exception as exc:

        _keep_card_description(
            job,
            f"Detail parse error ({type(exc).__name__}: {exc}); "
            "keeping the full list-card description.",
        )

        return job


async def scrape_upwork_details(
    session,
    jobs,
    max_jobs=UPWORK_DETAIL_MAX_PER_RUN,
):
    """Detail-crawl at most max_jobs, newest first (jobs are recency-sorted)."""

    if not jobs:
        return jobs

    selected = jobs

    if max_jobs is not None and len(jobs) > max_jobs:

        selected = jobs[:max_jobs]

        print(
            f"\n[Upwork] Capping detail crawl to newest "
            f"{max_jobs} of {len(jobs)} jobs."
        )

    if not selected:
        return jobs

    semaphore = asyncio.Semaphore(
        UPWORK_DETAIL_CONCURRENCY
    )

    async def worker(job):

        response, error = await fetch_upwork(
            session,
            job["Link"],
            semaphore,
            pacing=UPWORK_DETAIL_PACING,
        )

        if response is None:

            return handle_upwork_detail_failure(
                job,
                error,
            )

        return parse_upwork_detail(
            job,
            response,
        )

    print("\n" + "=" * 70)
    print("UPWORK DETAIL CRAWL (DRY RUN)")
    print("=" * 70)

    tasks = [
        asyncio.create_task(worker(job))
        for job in selected
    ]

    completed = 0

    for future in asyncio.as_completed(tasks):

        result = await future

        completed += 1

        status = result.get("DetailStatus")

        blocked = bool(result.get("DetailBlocked"))

        print(
            f"[Upwork Detail] {completed}/{len(selected)} "
            f"| status={status} "
            f"| blocked={blocked}"
        )

    return jobs


# ============================================================
# OUTPUT / REPORT
# ============================================================

def build_upwork_output(jobs):
    """Emit the same record shape as jobs_results.json, skipping no-description jobs."""

    records = []

    skipped = 0

    for job in jobs:

        if not clean_text(job.get("Description", "")):

            skipped += 1

            continue

        records.append(
            build_output_record(job)
        )

    if skipped:

        print(
            f"[Output] Skipping {skipped} job(s) with no "
            "clean description this run."
        )

    return records


def build_upwork_report(
    jobs,
    search_meta,
    output_records,
    started_at,
    elapsed,
):
    detail_attempted = sum(
        1
        for job in jobs
        if job.get("DetailStatus") is not None
        or job.get("DetailError")
        or job.get("DetailBlocked")
        or job.get("DetailNote")
    )

    detailed_jobs = []

    for job in jobs:

        detailed_jobs.append({
            "url": job.get("Link"),
            "title": job.get("Title"),
            "company": job.get("Company"),
            "posted": job.get("Posted"),
            "date_posted_iso": job.get("DatePosted"),
            "client_location": job.get("ClientLocation"),
            "job_type": job.get("JobType"),
            "experience_level": job.get("ExperienceLevel"),
            "budget": job.get("Budget"),
            "duration": job.get("Duration"),
            "skills": job.get("Skills", []),
            "job_id": job.get("JobID"),
            "card_uid": job.get("CardUID"),
            "description_chars": len(job.get("Description", "")),
            "description_source": job.get("DescriptionSource", ""),
            "detail_status": job.get("DetailStatus"),
            "detail_blocked": bool(job.get("DetailBlocked")),
            "detail_error": job.get("DetailError"),
            "detail_note": job.get("DetailNote"),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "platform": "Upwork",
        "dry_run": True,
        "compliance_note": (
            "Upwork robots.txt disallows /nx/ and /jobs/, and Upwork's "
            "llms.txt asks automated clients to avoid dynamic search "
            "result and login-gated pages. This dry run is a small "
            "feasibility probe; production use should use Upwork's "
            "official API/partner feed or be cleared with Upwork."
        ),
        "config": {
            "search_endpoint": UPWORK_SEARCH_ENDPOINT,
            "keyword": search_meta.get("keyword"),
            "location": UPWORK_LOCATION,
            "sort": UPWORK_SORT_BY,
            "job_type": search_meta.get("job_type"),
            "date_posted_filter": (
                "not exposed by Upwork guest search; sort=recency only"
            ),
            "per_page": search_meta.get("per_page"),
            "max_pages": MAX_UPWORK_PAGES,
            "search_concurrency": UPWORK_SEARCH_CONCURRENCY,
            "search_pacing": UPWORK_SEARCH_PACING,
            "detail_concurrency": UPWORK_DETAIL_CONCURRENCY,
            "detail_pacing": UPWORK_DETAIL_PACING,
            "detail_max_per_run": UPWORK_DETAIL_MAX_PER_RUN,
        },
        "search": search_meta,
        "counts": {
            "discovered": len(jobs),
            "with_description": sum(
                1
                for job in jobs
                if job.get("Description")
            ),
            "output_records": len(output_records),
            "detail_attempted": detail_attempted,
            "detail_blocked": sum(
                1
                for job in jobs
                if job.get("DetailBlocked")
            ),
            "detail_errors": sum(
                1
                for job in jobs
                if job.get("DetailError")
            ),
            "date_posted_iso_known": sum(
                1
                for job in jobs
                if job.get("DatePosted")
            ),
            "client_location_known": sum(
                1
                for job in jobs
                if job.get("ClientLocation")
            ),
        },
        "jobs": detailed_jobs,
    }


def write_json(path, payload):

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Saved {path}")


# ============================================================
# MAIN
# ============================================================

def parse_args(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Standalone Upwork dry run. Does not write jobs_results.json "
            "and is not wired into the app."
        )
    )

    parser.add_argument(
        "--keyword",
        default=UPWORK_KEYWORD,
        help=(
            "Search keyword. Omit/empty to mirror the shared scraper's "
            "ALL JOBS mode."
        ),
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_UPWORK_PAGES,
        help="Maximum search pages (default: %(default)s).",
    )

    parser.add_argument(
        "--job-type",
        choices=("any", "hourly", "fixed"),
        default="any",
        help=(
            "Upwork job-type filter. 'any' omits t; 'hourly' is t=0 and "
            "'fixed' is t=1 (default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--per-page",
        type=int,
        default=UPWORK_PER_PAGE,
        help=(
            "Cards per search page, clamped to 50 "
            "(default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--fetch-details",
        action="store_true",
        default=UPWORK_FETCH_DETAILS,
        help=(
            "Also probe detail pages. Guest detail is login-walled "
            "(expected 403), so this is off by default."
        ),
    )

    parser.add_argument(
        "--detail-max",
        type=int,
        default=UPWORK_DETAIL_MAX_PER_RUN,
        help=(
            "Detail jobs per run when --fetch-details is set "
            "(default: %(default)s)."
        ),
    )

    parser.add_argument(
        "--out",
        default=str(UPWORK_OUTPUT_FILE),
        help="Results JSON path.",
    )

    parser.add_argument(
        "--report",
        default=str(UPWORK_REPORT_FILE),
        help="Diagnostics JSON path.",
    )

    parser.add_argument(
        "--seen-state",
        default=str(UPWORK_SEEN_FILE),
        help="Separate cross-run seen sidecar path.",
    )

    parser.add_argument(
        "--no-seen",
        action="store_true",
        help="Do not read/write the cross-run seen sidecar.",
    )

    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Print the summary but write no files.",
    )

    args = parser.parse_args(argv)

    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be >= 1")

    if args.detail_max < 0:
        parser.error("--detail-max must be >= 0")

    if args.keyword is not None:
        args.keyword = clean_text(args.keyword) or None

    return args


async def run(args):

    started_wall = datetime.now(timezone.utc)

    started = time.perf_counter()

    async with CurlCffiAsyncSession() as session:

        discovered, search_meta = await scrape_upwork(
            session,
            keyword=args.keyword,
            max_pages=args.max_pages,
            per_page=args.per_page,
            job_type=args.job_type,
        )

        # Same global URL-level dedupe as the existing pipeline.
        discovered = deduplicate_jobs(
            discovered
        )

        print(
            f"[Upwork] Unique discovered: "
            f"{len(discovered)}"
        )

        # Cross-run diagnostic, kept in its own sidecar so it cannot
        # affect the real LinkedIn/Wuzzuf seen state.
        new_count = None

        if not args.no_seen:

            seen_keys = load_seen_keys(
                str(args.seen_state)
            )

            new_count, updated = report_new_jobs(
                discovered,
                seen_keys,
            )

            save_seen_keys(
                updated,
                str(args.seen_state),
            )

        if args.fetch_details and args.detail_max > 0:

            await scrape_upwork_details(
                session,
                discovered,
                max_jobs=args.detail_max,
            )

        else:

            print(
                "\n[Upwork] Detail crawl skipped "
                "(guest detail pages are login-walled; "
                "use --fetch-details to probe)."
            )

        output_records = build_upwork_output(
            discovered
        )

        elapsed = time.perf_counter() - started

        report = build_upwork_report(
            discovered,
            search_meta,
            output_records,
            started_wall,
            elapsed,
        )

        if new_count is not None:
            report["counts"]["new_vs_previous_runs"] = new_count

        print("\n" + "=" * 70)
        print("UPWORK DRY-RUN SUMMARY")
        print("=" * 70)

        print(f"Discovered:       {len(discovered)}")
        print(
            f"With description: "
            f"{report['counts']['with_description']}"
        )
        print(f"Output records:   {len(output_records)}")
        print(
            f"Detail attempted: "
            f"{report['counts']['detail_attempted']}"
        )
        print(
            f"Detail blocked:   "
            f"{report['counts']['detail_blocked']}"
        )
        print(
            f"Detail errors:    "
            f"{report['counts']['detail_errors']}"
        )

        if new_count is not None:
            print(f"New vs. prior:    {new_count}")

        print(f"Elapsed:          {elapsed:.1f}s")

        if not args.no_save:

            write_json(
                args.out,
                output_records,
            )

            write_json(
                args.report,
                report,
            )

    return 0 if output_records else 1


def main(argv=None):

    args = parse_args(argv)

    try:

        return asyncio.run(run(args))

    except KeyboardInterrupt:

        print("\nInterrupted.")

        return 130


if __name__ == "__main__":

    raise SystemExit(main())
