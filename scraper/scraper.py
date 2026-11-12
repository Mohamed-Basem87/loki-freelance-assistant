import argparse
import asyncio
import contextlib
import html as html_module
import json
import os
import random
import re
import time
from math import ceil
from urllib.parse import quote, urljoin, urlparse


from scrapling.fetchers import FetcherSession
from scrapling.parser import Selector
from curl_cffi.requests import AsyncSession as CurlCffiAsyncSession


# ============================================================
# CONFIG
# ============================================================

LOCATION = "Egypt"

# None = ALL jobs
# Example:
# KEYWORD = "AI Engineer"
KEYWORD = None


# ------------------------------------------------------------
# WUZZUF
# ------------------------------------------------------------

# Wuzzuf currently exposes 15 jobs per result page.
WUZZUF_PER_PAGE = 15

# None = all available Wuzzuf result pages.
# Keep 3 while testing.
MAX_WUZZUF_PAGES = 3

WUZZUF_SEARCH_CONCURRENCY = 8
WUZZUF_DETAIL_CONCURRENCY = 20


# ------------------------------------------------------------
# LINKEDIN
# ------------------------------------------------------------

# The logged-out jobs-guest endpoint returns pages of jobs.
LINKEDIN_PER_PAGE = 10

# None = continue until an empty/repeated page.
# Keep 3 while testing.
MAX_LINKEDIN_PAGES = 3

# Sort newest-first ("DD" = date descending; "R" = relevance, LinkedIn's
# default for a query with no sort/date params, which is why guest search
# pages 1-3 previously never changed between polls).
LINKEDIN_SORT_BY = "DD"

# Restrict guest search to postings from the last 24h. Confirmed working
# values for LinkedIn's guest f_TPR filter: r3600 (1h), r86400 (24h),
# r604800 (week), r2592000 (month) - "r<seconds>". 24h comfortably covers
# a 7-minute poll cadence while still narrowing away from the stale
# "top jobs" set.
LINKEDIN_TIME_POSTED_RANGE = "r86400"

# We only need to detail-fetch the newest handful each poll, not every
# job discovered across MAX_LINKEDIN_PAGES. Jobs are already newest-first
# once LINKEDIN_SORT_BY=DD is applied, so a simple head-slice keeps the
# most recent postings and cuts detail-request volume (and 429 pressure)
# without touching MAX_LINKEDIN_PAGES / discovery breadth.
LINKEDIN_DETAIL_MAX_PER_RUN = 15

LINKEDIN_SEARCH_CONCURRENCY = 3
LINKEDIN_DETAIL_CONCURRENCY = 2


# ------------------------------------------------------------
# UPWORK
# ------------------------------------------------------------
#
# COMPLIANCE: Upwork's robots.txt disallows /nx/ and /jobs/, and its
# llms.txt asks automated clients to avoid dynamic search and
# login-gated pages. Guest scraping is therefore a production risk;
# prefer Upwork's official API/partner feed where available.

UPWORK_BASE = "https://www.upwork.com"
UPWORK_SEARCH_ENDPOINT = f"{UPWORK_BASE}/nx/search/jobs/"

# Guest search returns 50 cards per page. per_page=100 silently falls
# back to 10, so 50 is the usable maximum.
UPWORK_PER_PAGE = 50

# None = every page the reported total implies.
# Keep 3 while testing: with no keyword, Upwork reports 35k+ jobs
# (700+ pages), so an uncapped run is not realistic.
MAX_UPWORK_PAGES = 3

# Upwork's `t` query param is a JOB TYPE filter, not a date-posted
# filter: t=0 -> hourly, t=1 -> fixed price, omit -> both. Keep None
# so discovery covers every job type. The guest search exposes no
# date-posted facet at all; sort=recency is the only temporal control.
UPWORK_JOB_TYPE = None

UPWORK_SORT_BY = "recency"

# Card descriptions are already the complete job description, so only
# the newest handful need the expensive detail fetch (which also gives
# a structured datePosted and the client's country).
UPWORK_DETAIL_MAX_PER_RUN = 15

UPWORK_SEARCH_CONCURRENCY = 2
UPWORK_DETAIL_CONCURRENCY = 2
UPWORK_SEARCH_PACING = 1.0
UPWORK_DETAIL_PACING = 1.5

# Clamp a server-provided Retry-After so a bad header cannot park a
# request for an hour.
UPWORK_MAX_BACKOFF_SECONDS = 120

# Guest search returns no client name or location, so these are
# explicit placeholders rather than fabricated values. Detail pages
# expose the client's country, recorded separately as ClientLocation.
UPWORK_COMPANY_PLACEHOLDER = "N/A"
UPWORK_LOCATION_PLACEHOLDER = "Anywhere"


# ------------------------------------------------------------
# RUN OPTIONS / SOURCE SELECTION
# ------------------------------------------------------------
# The app schedules the scraper per source group (e.g. a 60s Upwork-only
# run alongside a 300s LinkedIn+Wuzzuf run), so the same script has to
# support scraping a subset of platforms. Defaults preserve the original
# all-sources, full-page, 15-detail behaviour for a manual `python
# scraper.py` run.

SOURCE_PLATFORMS = {
    "wuzzuf": "Wuzzuf",
    "linkedin": "LinkedIn",
    "upwork": "Upwork",
}
ALL_SOURCES = tuple(SOURCE_PLATFORMS)

DEFAULT_RUN_OPTIONS = {
    "sources": ALL_SOURCES,
    "max_upwork_pages": MAX_UPWORK_PAGES,
    "upwork_detail_budget": UPWORK_DETAIL_MAX_PER_RUN,
    "upwork_snapshot_limit": UPWORK_DETAIL_MAX_PER_RUN,
}

RUN_OPTIONS = dict(DEFAULT_RUN_OPTIONS)

# Upwork detail pages are the expensive/block-prone half of the scrape:
# success is intermittent (~4/5 HTTP 200 in live sampling, the rest 403).
# Remembering a successful enrichment per URL means a 1-minute poll only
# ever fetches details for genuinely new postings instead of re-fetching
# the same newest-15 every cycle.
UPWORK_DETAIL_CACHE_FILE = "upwork_detail_cache.json"
UPWORK_CACHE_FIELDS = (
    "Company",
    "DatePosted",
    "ClientLocation",
    "DetailStatus",
    "DetailFetcher",
)
# The card always provides the description, so caching it would only bloat
# the file (a new top-15 URL can be enriched every minute). ~2,000 entries
# is many hours of turnover -- far longer than a URL stays in the newest-15
# window -- while keeping the cache small enough to load on every run.
UPWORK_CACHE_MAX_ENTRIES = 2000
UPWORK_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

# Consecutive blocked/failed Upwork details inside one run after which the
# remaining detail fetches are abandoned and enrichment is parked for a
# while. Discovery keeps running, and cards already carry the full job
# description, so a cooldown costs freshness of the DatePosted/client
# country enrichment only -- never a job.
UPWORK_BLOCK_STREAK_LIMIT = 4
UPWORK_BLOCK_COOLDOWN_SECONDS = 10 * 60


def _normalize_source_list(value):
    """Parse `--sources` into a validated tuple of source ids."""
    if value is None:
        return ALL_SOURCES
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in ("", "all"):
            return ALL_SOURCES
        requested = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        requested = [str(part).strip().lower() for part in value if str(part).strip()]
    unknown = sorted(set(requested) - set(ALL_SOURCES))
    if unknown:
        raise ValueError(
            f"unknown source(s): {', '.join(unknown)} "
            f"(expected one or more of: {', '.join(ALL_SOURCES)})"
        )
    if not requested:
        return ALL_SOURCES
    # Keep a stable order regardless of CLI ordering.
    return tuple(name for name in ALL_SOURCES if name in requested)


def _selected_sources():
    return tuple(RUN_OPTIONS.get("sources") or ALL_SOURCES)


def _atomic_write_json(filename, data):
    """Write JSON via a temp file + os.replace so readers never see a
    half-written snapshot (the app polls this file concurrently)."""
    directory = os.path.dirname(os.path.abspath(filename))
    tmp_path = os.path.join(
        directory,
        f".{os.path.basename(filename)}.{os.getpid()}.tmp",
    )
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, filename)


@contextlib.contextmanager
def _snapshot_lock(filename, timeout=10.0):
    """Cross-process lock around the snapshot read-merge-write.

    The Upwork-only 60s run and the LinkedIn+Wuzzuf 300s run are separate
    subprocesses that both update jobs_results.json. Each writer has valid
    data for its own sources, but an interleaved read-modify-write could
    resurrect a stale slice of the other source. A lock file (O_EXCL, the
    same mechanism on Windows and Linux) removes that window. Lock
    acquisition is best-effort: on timeout we log and proceed, because a
    stuck lock must never stop job ingestion.
    """
    lock_path = f"{filename}.lock"
    acquired = False
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 60:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                print(
                    f"[WARN] snapshot lock {lock_path} timed out after "
                    f"{timeout:.1f}s; writing anyway."
                )
                break
            time.sleep(0.05)

    try:
        yield
    finally:
        if acquired:
            try:
                os.unlink(lock_path)
            except OSError:
                pass


def _load_snapshot_records(filename):
    try:
        with open(filename, "r", encoding="utf-8") as handle:
            records = json.load(handle)
    except (OSError, ValueError):
        return []
    return records if isinstance(records, list) else []


def _merge_snapshot(filename, records):
    """Replace this run's platforms in the on-disk snapshot, keeping every
    record from the sources this run did not scrape.

    A partial run must not erase the other platforms' snapshot: the app's
    FileJobSource adapters each filter the same file, and a restart between
    an Upwork-only write and the next full run would otherwise leave them
    with an empty snapshot.
    """
    scraped_platforms = {
        SOURCE_PLATFORMS[name]
        for name in _selected_sources()
        if name in SOURCE_PLATFORMS
    }
    with _snapshot_lock(filename):
        existing = _load_snapshot_records(filename)
        kept = [
            record
            for record in existing
            if isinstance(record, dict)
            and str(record.get("source") or "").strip() not in scraped_platforms
        ]
        merged = list(records) + kept
        _atomic_write_json(filename, merged)
    return len(kept)


def load_upwork_detail_cache(filename=UPWORK_DETAIL_CACHE_FILE):
    """Load the per-URL Upwork detail cache, tolerating absence/corruption."""
    entries, cooldown_until = None, 0
    try:
        with open(filename, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            entries = raw.get("entries")
            cooldown_until = raw.get("cooldown_until") or 0
    except (OSError, ValueError):
        entries, cooldown_until = None, 0

    now = time.time()
    pruned = {}
    for url, entry in (entries or {}).items():
        if not isinstance(entry, dict):
            continue
        fetched_at = entry.get("fetched_at") or 0
        if not entry.get("DetailStatus"):
            # Card-only entries written by older builds (or a partial
            # write) never had a detail fetch; drop them so the job is
            # enriched on a later run instead of being served forever.
            continue
        if now - fetched_at > UPWORK_CACHE_MAX_AGE_SECONDS:
            continue
        pruned[url] = entry

    if len(pruned) > UPWORK_CACHE_MAX_ENTRIES:
        newest = sorted(
            pruned.items(),
            key=lambda item: item[1].get("fetched_at") or 0,
            reverse=True,
        )
        pruned = dict(newest[:UPWORK_CACHE_MAX_ENTRIES])

    return {
        "entries": pruned,
        "cooldown_until": float(cooldown_until or 0),
    }


def save_upwork_detail_cache(cache, filename=UPWORK_DETAIL_CACHE_FILE):
    _atomic_write_json(
        filename,
        {
            "entries": cache.get("entries") or {},
            "cooldown_until": cache.get("cooldown_until") or 0,
        },
    )


def _apply_cached_upwork_detail(job, cache):
    """Overlay a cached detail enrichment onto a search-card job.

    Returns True when the job was fully served from cache (no detail
    request needed).
    """
    entry = (cache.get("entries") or {}).get(job.get("Link") or "")
    if not entry:
        return False
    for field in UPWORK_CACHE_FIELDS:
        value = entry.get(field)
        if value:
            job[field] = value
    job.pop("DetailBlocked", None)
    job.pop("DetailError", None)
    job.pop("DetailNote", None)
    return True


def _remember_upwork_detail(job, cache):
    """Persist a successful detail enrichment for future runs.

    Only jobs that actually went through a detail fetch may be cached.
    Search-card jobs carry a usable description but no DatePosted/client
    country; caching those would make every later run serve them from
    cache and never enrich them. ``DetailStatus`` is set by both detail
    parsers and by the failure handler, so its presence is the marker of
    an attempted fetch; blocked/errored fetches are never cached.
    """
    if job.get("DetailBlocked") or job.get("DetailError"):
        return False
    if not job.get("DetailStatus"):
        return False
    url = job.get("Link") or ""
    if not url:
        return False
    entry = {
        field: job[field]
        for field in UPWORK_CACHE_FIELDS
        if job.get(field)
    }
    entry["fetched_at"] = time.time()
    cache.setdefault("entries", {})[url] = entry
    return True


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

HTTP_TIMEOUT = 20
HTTP_RETRIES = 2
HTTP_RETRY_DELAY = 1


# ------------------------------------------------------------
# RATE LIMIT HANDLING
# ------------------------------------------------------------

MAX_429_RETRIES = 3
BACKOFF_429 = 4

# Extra fixed delay (seconds) between successive LinkedIn detail requests
# inside the concurrency-gated pool, on top of the semaphore. The
# semaphore only limits how many requests are in flight at once; it does
# not pace *how fast* the pool cycles through the queue, which is what
# actually drives requests-per-minute against LinkedIn's guest API.
LINKEDIN_DETAIL_PACING = 1.5


# ------------------------------------------------------------
# OUTPUT
# ------------------------------------------------------------

OUTPUT_FILE = "jobs_results.json"


# ============================================================
# GENERIC HELPERS
# ============================================================

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

    # Unescape HTML entities FIRST, before any tag-stripping. Some
    # sources (confirmed: LinkedIn's own JSON-LD JobPosting.description
    # field) double-encode - the tags themselves are entity-escaped
    # ("&lt;ul&gt;&lt;li&gt;..." rather than "<ul><li>..."). Stripping
    # tags before unescaping finds nothing to strip (there's no literal
    # "<" yet), and then unescaping afterward resurrects those entities
    # into real, now-unstripped tags - so raw "<ul><li><strong>" markup
    # was reaching stored descriptions. Unescaping first guarantees any
    # entity-encoded tags become real tags in time to be stripped below.
    text = html_module.unescape(text)

    # Remove script/style/noscript blocks WHOLESALE (tag + inner
    # content) before generic tag-stripping below. Otherwise, on a
    # whole-page fallback, the tag-only strip below turns
    # "<style>.foo{color:red}</style>" into ".foo{color:red}" -
    # CSS/JS text leaks straight into the stored description instead
    # of being removed. This showed up in production as CSS rules
    # appearing inside a job's description text.
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
    """
    Look for a schema.org JobPosting embedded as JSON-LD
    (<script type="application/ld+json">). Job boards embed this for
    Google for Jobs, and it gives a clean title/description/company
    with none of the surrounding nav/footer/ad text that dumping the
    whole page carries.

    Returns a dict with any of "title", "description", "company"
    that were found, or None if no JobPosting JSON-LD is present.
    """

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

        candidates = (
            data if isinstance(data, list) else [data]
        )

        # Some sites nest everything under "@graph".
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

            # Upwork's JobPosting exposes the real posting timestamp;
            # LinkedIn/Wuzzuf simply won't have this key.
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


def extract_wuzzuf_embedded_state(response):
    """
    Wuzzuf job pages do NOT embed JSON-LD (confirmed: 0/45 sampled
    pages had a JobPosting script). The DOM itself is also unusable
    for a text-node fallback selector - the server-rendered
    `<div class="css-n7fcne">` job-description container's text IS
    present, but only reliably so on some page variants; the sturdier
    source across page variants is the client-side React state blob
    Wuzzuf inlines for hydration:

        <script> (function(){
            var Wuzzuf = window.Wuzzuf = window.Wuzzuf || {};
            ...
            Wuzzuf.initialStoreState = { ... big JSON ... };
            Wuzzuf.serverRenderedURL = "/jobs/p/<slug>";
            ...
        })(); </script>

    `initialStoreState.entities.job.collection` is a dict of ALL jobs
    referenced on the page (the viewed job PLUS its "Similar Jobs"
    sidebar entries), keyed by job UUID. To identify which entry is
    the job actually being viewed, we use (in order):

      1. `initialStoreState.jobPage.similarJobs` - its only top-level
         key IS the viewed job's own ID (confirmed: the value under
         that key holds the *other* jobs' ids as
         `similar.ids`/`featured.ids`, i.e. the sidebar). This needs
         no URL parsing at all and is robust to page variants (e.g.
         confidential/hidden-title postings) where the visible URL
         or the job's "uri" attribute may not line up with
         `serverRenderedURL` the way a normal listing does.
      2. Fallback: match `serverRenderedURL` against each candidate
         job's own "uri" attribute (works for the common case, but
         was observed to miss on at least one hidden-title/
         confidential job page).
      3. Fallback: if there's exactly one job in the collection at
         all, use it.

    Once found, we read that job's
    `attributes.userContentTranslations.description.en` - already
    plain text (no HTML tags at all), not just HTML-stripped. This
    is the actual quality-of-service description text an applicant
    would read, not page chrome.

    Falls back to `attributes.description` (HTML, run through
    strip_html_tags) if the translation block is missing.

    Returns a dict with any of "title", "description", "company"
    that were found, or None if the state blob isn't present/parseable
    or no matching job entry is found.
    """

    try:
        html_text = response.body.decode("utf-8", "ignore")
    except Exception:
        try:
            html_text = response.text
        except Exception:
            return None

    if not html_text:
        return None

    marker = re.search(
        r"Wuzzuf\.initialStoreState\s*=\s*",
        html_text
    )

    if not marker:
        return None

    decoder = json.JSONDecoder()

    try:
        state, _ = decoder.raw_decode(
            html_text,
            marker.end()
        )
    except Exception:
        return None

    try:
        jobs = state["entities"]["job"]["collection"]
    except Exception:
        return None

    if not jobs:
        return None

    target_attrs = None

    # Method 1: jobPage.similarJobs top-level key = viewed job's ID.
    try:
        similar_jobs_keys = list(
            state.get("jobPage", {})
            .get("similarJobs", {})
            .keys()
        )
    except Exception:
        similar_jobs_keys = []

    for candidate_id in similar_jobs_keys:

        if candidate_id in jobs:
            target_attrs = jobs[candidate_id].get(
                "attributes", {}
            )
            break

    # Method 2: match serverRenderedURL against each job's own "uri".
    if target_attrs is None:

        server_url_match = re.search(
            r'Wuzzuf\.serverRenderedURL\s*=\s*"([^"]+)"',
            html_text
        )

        server_url = (
            server_url_match.group(1)
            if server_url_match
            else ""
        )

        for job_entry in jobs.values():

            attrs = job_entry.get("attributes", {})
            uri = attrs.get("uri", "")

            if (
                uri
                and server_url
                and uri.lstrip("/") in server_url.lstrip("/")
            ):
                target_attrs = attrs
                break

    # Method 3: sole entry in the collection.
    if target_attrs is None and len(jobs) == 1:
        target_attrs = next(iter(jobs.values())).get("attributes", {})

    if target_attrs is None:
        return None

    result = {}

    if target_attrs.get("title"):
        result["title"] = clean_text(target_attrs["title"])

    translated_description = (
        target_attrs.get("userContentTranslations", {})
        .get("description", {})
        .get("en")
    )

    if translated_description:
        result["description"] = clean_text(translated_description)
    elif target_attrs.get("description"):
        result["description"] = strip_html_tags(
            target_attrs["description"]
        )

    return result if result else None


def canonical_url(url):
    """
    Remove tracking parameters and fragments.
    """

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

    return urljoin(
        base,
        href
    )


# ============================================================
# YOUR EXISTING FILTER
# ============================================================

def job_passes_existing_filter(job):
    """
    ========================================================
    PUT YOUR EXISTING FILTRATION LOGIC HERE.
    ========================================================

    IMPORTANT:

    This function runs BEFORE expensive detail crawling.

    That means:
        5,000 discovered jobs
             ↓
        your filtering
             ↓
        perhaps 100 relevant jobs
             ↓
        ONLY those 100 get detail requests

    For now it returns True so the script works immediately.

    Replace the body with your actual filtration logic.

    Example:

        title = job["Title"].lower()
        text = job.get("CardText", "").lower()

        return (
            "data analyst" in title
            or "power bi" in text
        )
    """

    return True


# ============================================================
# FILTERING
# ============================================================

def filter_before_details(jobs):

    print("\n" + "=" * 70)
    print("PRE-DETAIL FILTRATION")
    print("=" * 70)

    filtered = []

    rejected = 0

    for job in jobs:

        try:

            accepted = (
                job_passes_existing_filter(
                    job
                )
            )

        except Exception as exc:

            print(
                f"[Filter ERROR] "
                f"{job.get('Title', '')}: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            accepted = False

        if accepted:

            filtered.append(
                job
            )

        else:

            rejected += 1

    print(
        f"Input jobs:     {len(jobs)}"
    )

    print(
        f"Passed filter:  {len(filtered)}"
    )

    print(
        f"Rejected:       {rejected}"
    )

    return filtered


# ============================================================
# WUZZUF SEARCH
# ============================================================

WUZZUF_BASE = "https://wuzzuf.net"


def build_wuzzuf_search_url(
    keyword=None,
    start=0
):

    # NOTE ON "NEWEST FIRST" FOR WUZZUF:
    # Unlike LinkedIn's guest API (documented sortBy/f_TPR params),
    # wuzzuf.net/search/jobs/ does not expose a public, documented
    # "sort by date" query parameter. Checked wuzzuf.net directly plus
    # every third-party Wuzzuf scraper's documented inputs; none
    # reference a URL-level sort/date param - their "recent" filters
    # are implemented by scraping normally and filtering client-side
    # on the posted date, not via a query string. Adding a made-up
    # param here (e.g. "sort=date") would silently be ignored by the
    # server, which is exactly the failure mode this fix needs to
    # avoid. See scrape_wuzzuf() / the Posted-field extraction below
    # for the actual fix: instrumenting the real posted date per job
    # so staleness can be measured, plus persistent seen-job tracking
    # across polls so "new" is computed from history, not from
    # assuming the page order changes.

    params = []

    if keyword:

        params.append(
            f"q={quote(keyword)}"
        )

    if start > 0:

        params.append(
            f"start={start}"
        )

    query = "&".join(
        params
    )

    if query:

        return (
            f"{WUZZUF_BASE}/search/jobs/"
            f"?{query}"
        )

    return (
        f"{WUZZUF_BASE}/search/jobs/"
    )


def extract_wuzzuf_total(
    response
):

    try:

        body = response.body.decode(
            "utf-8",
            "ignore"
        )

    except Exception:

        return None

    patterns = [
        r"Showing\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)",
        r"showing\s+\d+\s*-\s*\d+\s+of\s+([\d,]+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            body,
            re.I
        )

        if match:

            try:

                return int(
                    match.group(1)
                    .replace(
                        ",",
                        ""
                    )
                )

            except Exception:

                pass

    return None


def extract_wuzzuf_jobs(
    response
):

    cards = response.css(
        "div.css-pkv5jc"
    )

    jobs = []

    for card in cards:

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        title_nodes = (
            card.css("h2 a")
            or card.css("h2")
        )

        title = ""

        if title_nodes:

            title = clean_text(
                title_nodes[0].text
            )

        if not title:
            continue

        # ----------------------------------------------------
        # LINK
        # ----------------------------------------------------

        link = ""

        if title_nodes:

            href = get_attr(
                title_nodes[0],
                "href"
            )

            if href:

                link = canonical_url(
                    absolute_url(
                        WUZZUF_BASE,
                        href
                    )
                )

        if not link:

            for node in card.css(
                "a[href]"
            ):

                href = get_attr(
                    node,
                    "href"
                )

                if not href:
                    continue

                full = absolute_url(
                    WUZZUF_BASE,
                    href
                )

                if (
                    "/jobs/"
                    in full
                    or "/internship/"
                    in full
                ):

                    link = canonical_url(
                        full
                    )

                    break

        if not link:
            continue

        # ----------------------------------------------------
        # COMPANY
        # ----------------------------------------------------

        company_nodes = card.css(
            "a.css-17s97q8, "
            "span.css-17s97q8, "
            "div.css-d7j1kk a, "
            "a.css-o171kl"
        )

        company = "N/A"

        if company_nodes:

            company = clean_text(
                company_nodes[0].text
            )

        # ----------------------------------------------------
        # CARD TEXT
        # ----------------------------------------------------

        card_text = clean_text(
            card.text
        )

        # ----------------------------------------------------
        # POSTED DATE (diagnostic - was previously not captured
        # for Wuzzuf at all, even though the site shows a relative
        # "posted X days/hours ago" label on every card). Parsed
        # from card_text via regex rather than a hashed CSS class,
        # since Wuzzuf's build-generated class names (e.g.
        # "css-pkv5jc") change across frontend deploys and a wrong
        # guess would just silently fall back to "N/A" - same as
        # today, no regression, but a real fix when it matches.
        # This lets you empirically confirm/refute the "stale top
        # list" theory across successive polls instead of guessing.
        # ----------------------------------------------------

        posted_match = re.search(
            r"\b(\d+|a|an)\s+"
            r"(minute|hour|day|week|month)s?\s+ago\b",
            card_text,
            re.I
        )

        posted = (
            clean_text(posted_match.group(0))
            if posted_match
            else "N/A"
        )

        jobs.append({

            "Platform":
                "Wuzzuf",

            "Title":
                title,

            "Company":
                company or "N/A",

            "Location":
                LOCATION,

            "Link":
                link,

            "Posted":
                posted,

            "CardText":
                card_text,
        })

    return jobs


async def fetch_wuzzuf_page(
    session,
    start
):

    url = build_wuzzuf_search_url(
        keyword=KEYWORD,
        start=start
    )

    try:

        response = await session.get(
            url
        )

        return {
            "start":
                start,

            "url":
                response.url,

            "status":
                response.status,

            "jobs":
                extract_wuzzuf_jobs(
                    response
                ),

            "total":
                extract_wuzzuf_total(
                    response
                ),
        }

    except Exception as exc:

        return {
            "start":
                start,

            "url":
                url,

            "status":
                None,

            "jobs":
                [],

            "total":
                None,

            "error":
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
        }


async def scrape_wuzzuf():

    print("\n" + "=" * 70)
    print("WUZZUF SEARCH")
    print("=" * 70)

    all_jobs = []

    seen_urls = set()

    async with FetcherSession(
        impersonate="chrome",
        timeout=HTTP_TIMEOUT,
        retries=HTTP_RETRIES,
        retry_delay=HTTP_RETRY_DELAY,
        follow_redirects="safe",
    ) as session:

        # ----------------------------------------------------
        # FIRST PAGE
        # ----------------------------------------------------

        first = await fetch_wuzzuf_page(
            session,
            0
        )

        total = first.get(
            "total"
        )

        print(
            f"[Wuzzuf] Page 1 | "
            f"jobs={len(first['jobs'])}"
        )

        if total:

            print(
                f"[Wuzzuf] Reported total: "
                f"{total}"
            )

        # Add first page.

        for job in first["jobs"]:

            link = job["Link"]

            if link in seen_urls:
                continue

            seen_urls.add(link)

            all_jobs.append(
                job
            )

        # ----------------------------------------------------
        # DETERMINE TOTAL PAGES
        # ----------------------------------------------------

        if total:

            total_pages = ceil(
                total /
                WUZZUF_PER_PAGE
            )

        else:

            total_pages = (
                MAX_WUZZUF_PAGES
                or 1
            )

        if (
            MAX_WUZZUF_PAGES
            is not None
        ):

            total_pages = min(
                total_pages,
                MAX_WUZZUF_PAGES
            )

        print(
            f"[Wuzzuf] Pages planned: "
            f"{total_pages}"
        )

        # ----------------------------------------------------
        # REMAINING PAGES IN PARALLEL
        # ----------------------------------------------------

        if total_pages > 1:

            semaphore = asyncio.Semaphore(
                WUZZUF_SEARCH_CONCURRENCY
            )

            async def worker(start):

                async with semaphore:

                    return await fetch_wuzzuf_page(
                        session,
                        start
                    )

            tasks = [
                asyncio.create_task(
                    worker(start)
                )
                for start in range(
                    1,
                    total_pages
                )
            ]

            results = await asyncio.gather(
                *tasks,
                return_exceptions=True
            )

            for result in results:

                if isinstance(
                    result,
                    Exception
                ):

                    print(
                        "[Wuzzuf] "
                        f"Page worker failed: "
                        f"{result}"
                    )

                    continue

                start = result[
                    "start"
                ]

                page_jobs = result[
                    "jobs"
                ]

                new_count = 0

                for job in page_jobs:

                    link = job["Link"]

                    if link in seen_urls:
                        continue

                    seen_urls.add(link)

                    all_jobs.append(
                        job
                    )

                    new_count += 1

                print(
                    f"[Wuzzuf] "
                    f"start={start} | "
                    f"jobs={len(page_jobs)} | "
                    f"new={new_count}"
                )

    print(
        f"\n[Wuzzuf] FINAL UNIQUE: "
        f"{len(all_jobs)}"
    )

    return all_jobs


# ============================================================
# LINKEDIN GUEST SEARCH
# ============================================================

LINKEDIN_BASE = (
    "https://www.linkedin.com"
)

LINKEDIN_GUEST_SEARCH = (
    f"{LINKEDIN_BASE}"
    "/jobs-guest/jobs/api/"
    "seeMoreJobPostings/search"
)


# ============================================================
# LINKEDIN HTTP CLIENT (curl_cffi, not scrapling.FetcherSession)
# ============================================================
#
# LinkedIn's guest API answers HTTP 200 with an EMPTY body specifically
# to scrapling.fetchers.FetcherSession's impersonation fingerprint, on
# every variant tried (different fingerprints, stealth off). curl,
# plain httpx, and curl_cffi.requests.AsyncSession directly all get the
# full response for the identical URL from the same IP. curl_cffi is
# already a scrapling dependency (FetcherSession uses it internally for
# "impersonate="), so no new package - only LinkedIn's request path is
# swapped. Wuzzuf keeps using FetcherSession, completely untouched.
#
# _LinkedInResponse wraps the curl_cffi response with the same
# .status / .url / .text / .body / .css() surface scrapling's Response
# object provides (via a scrapling Selector built from the response
# text), so extract_linkedin_jobs() and parse_linkedin_detail() did not
# need to change at all.

class _LinkedInResponse:

    __slots__ = (
        "status",
        "url",
        "text",
        "body",
        "_selector",
    )

    def __init__(self, raw):

        self.status = raw.status_code
        self.url = str(raw.url)
        self.text = raw.text
        self.body = raw.content
        self._selector = None

    def css(self, query):

        if self._selector is None:

            self._selector = Selector(
                content=self.text or "",
                url=self.url,
            )

        return self._selector.css(query)


def linkedin_session():
    """
    Single spot that creates LinkedIn's HTTP client. If curl_cffi ever
    needs swapping again, this is the only place to change.
    """

    return CurlCffiAsyncSession()


async def linkedin_get(
    session,
    url,
    retries=0,
    retry_delay=0.5,
):
    """
    Shared adapter for LinkedIn search + detail requests, used by
    fetch_linkedin_search_page() and fetch_detail_with_backoff() so
    both go through the same client and any future re-test is one spot.
    """

    attempt = 0

    last_exc = None

    while attempt <= retries:

        try:

            raw = await session.get(
                url,
                impersonate="chrome",
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )

            return _LinkedInResponse(
                raw
            )

        except Exception as exc:

            last_exc = exc

            attempt += 1

            if attempt <= retries:

                await asyncio.sleep(
                    retry_delay
                )

    raise last_exc


def build_linkedin_search_url(
    keyword=None,
    location="Egypt",
    start=0
):

    params = []

    if keyword:

        params.append(
            f"keywords={quote(keyword)}"
        )

    if location:

        params.append(
            f"location={quote(location)}"
        )

    # NEWEST FIRST: LinkedIn's guest seeMoreJobPostings/search endpoint
    # (which LINKEDIN_GUEST_SEARCH points at) honors both of these -
    # confirmed against LinkedIn's own jobs/search UI query params and
    # multiple independent guest-API scrapers built on this exact
    # endpoint. Without them the endpoint defaults to "R" (relevance),
    # which is a stable ranking unrelated to posting time - the same
    # failure mode described for Wuzzuf, but here it's a documented,
    # verifiable fix rather than a guess.
    params.append(
        f"sortBy={LINKEDIN_SORT_BY}"
    )

    params.append(
        f"f_TPR={LINKEDIN_TIME_POSTED_RANGE}"
    )

    params.append(
        f"start={start}"
    )

    return (
        f"{LINKEDIN_GUEST_SEARCH}"
        f"?{'&'.join(params)}"
    )


def extract_linkedin_jobs(
    response
):

    # Guest endpoint returns job-card <li>s.
    cards = (
        response.css(
            "li"
        )
    )

    jobs = []

    for card in cards:

        # ----------------------------------------------------
        # TITLE
        # ----------------------------------------------------

        title_nodes = (
            card.css(
                "[class*='_title']"
            )
            or card.css(
                ".base-search-card__title"
            )
        )

        title = ""

        if title_nodes:

            title = clean_text(
                title_nodes[0].text
            )

        # ----------------------------------------------------
        # URL
        # ----------------------------------------------------

        link = ""

        link_nodes = (
            card.css(
                "a[href*='/jobs/view/']"
            )
            or card.css(
                "a[class*='_full-link']"
            )
        )

        if link_nodes:

            href = get_attr(
                link_nodes[0],
                "href"
            )

            if href:

                link = canonical_url(
                    absolute_url(
                        LINKEDIN_BASE,
                        href
                    )
                )

        if not link:
            continue

        # ----------------------------------------------------
        # COMPANY
        # ----------------------------------------------------

        company_nodes = (
            card.css(
                "[class*='_subtitle']"
            )
            or card.css(
                ".base-search-card__subtitle"
            )
        )

        company = "N/A"

        if company_nodes:

            company = clean_text(
                company_nodes[0].text
            )

        # ----------------------------------------------------
        # LOCATION
        # ----------------------------------------------------

        location_nodes = (
            card.css(
                "[class*='_location']"
            )
            or card.css(
                ".job-search-card__location"
            )
        )

        location = LOCATION

        if location_nodes:

            location = clean_text(
                location_nodes[0].text
            )

        # ----------------------------------------------------
        # DATE
        # ----------------------------------------------------

        date_nodes = (
            card.css(
                "time"
            )
            or card.css(
                "[class*='listdate']"
            )
        )

        posted = "N/A"

        if date_nodes:

            posted = clean_text(
                date_nodes[0].text
            )

        # ----------------------------------------------------
        # JOB ID
        # ----------------------------------------------------

        job_id = extract_linkedin_job_id(
            link
        )

        jobs.append({

            "Platform":
                "LinkedIn",

            "Title":
                title or "N/A",

            "Company":
                company or "N/A",

            "Location":
                location or LOCATION,

            "Posted":
                posted,

            "Link":
                link,

            "JobID":
                job_id,

            "CardText":
                clean_text(card.text),
        })

    return jobs


def extract_linkedin_job_id(
    url
):

    if not url:
        return ""

    match = re.search(
        r"-(\d+)(?:/)?$",
        url
    )

    if match:
        return match.group(1)

    # Some URLs may end in an ID without a hyphen.
    match = re.search(
        r"/(\d+)$",
        url
    )

    if match:
        return match.group(1)

    return ""


async def fetch_linkedin_search_page(
    session,
    start
):

    url = build_linkedin_search_url(
        keyword=KEYWORD,
        location=LOCATION,
        start=start
    )

    try:

        response = await linkedin_get(
            session,
            url,
            retries=1,
            retry_delay=0.5,
        )

        return {
            "start":
                start,

            "url":
                response.url,

            "status":
                response.status,

            "jobs":
                extract_linkedin_jobs(
                    response
                ),
        }

    except Exception as exc:

        return {
            "start":
                start,

            "url":
                url,

            "status":
                None,

            "jobs":
                [],

            "error":
                (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                ),
        }


async def scrape_linkedin():

    print("\n" + "=" * 70)
    print("LINKEDIN SEARCH")
    print("=" * 70)

    all_jobs = []

    seen_urls = set()

    seen_signatures = set()

    async with linkedin_session() as session:

        start = 0

        page_number = 1

        while True:

            if (
                MAX_LINKEDIN_PAGES
                is not None
                and
                page_number >
                MAX_LINKEDIN_PAGES
            ):

                print(
                    "[LinkedIn] "
                    f"Reached max_pages="
                    f"{MAX_LINKEDIN_PAGES}"
                )

                break

            result = (
                await fetch_linkedin_search_page(
                    session,
                    start
                )
            )

            jobs = result[
                "jobs"
            ]

            print(
                f"[LinkedIn] "
                f"Page {page_number} "
                f"| start={start} "
                f"| jobs={len(jobs)}"
            )

            if not jobs:

                print(
                    "[LinkedIn] "
                    "No jobs returned."
                )

                break

            # ------------------------------------------------
            # PAGE DUPLICATION DETECTION
            # ------------------------------------------------

            signature = tuple(
                sorted(
                    job["Link"]
                    for job in jobs
                    if job.get("Link")
                )
            )

            if (
                signature
                in seen_signatures
            ):

                print(
                    "[LinkedIn] "
                    "Repeated page detected."
                )

                break

            seen_signatures.add(
                signature
            )

            # ------------------------------------------------
            # ADD JOBS
            # ------------------------------------------------

            new_count = 0

            for job in jobs:

                link = job["Link"]

                if link in seen_urls:
                    continue

                seen_urls.add(link)

                all_jobs.append(
                    job
                )

                new_count += 1

            print(
                f"[LinkedIn] "
                f"New={new_count} "
                f"| Total={len(all_jobs)}"
            )

            # ------------------------------------------------
            # LAST PAGE
            # ------------------------------------------------

            if len(jobs) < LINKEDIN_PER_PAGE:

                print(
                    "[LinkedIn] "
                    "Short page detected."
                )

                break

            # Guest endpoint uses 10-job offsets.
            start += LINKEDIN_PER_PAGE
            page_number += 1

            # Do not hammer LinkedIn.
            await asyncio.sleep(
                0.5
            )

    print(
        f"\n[LinkedIn] "
        f"FINAL UNIQUE: "
        f"{len(all_jobs)}"
    )

    return all_jobs


# ============================================================
# DETAIL URL HELPERS
# ============================================================

LINKEDIN_GUEST_DETAIL = (
    f"{LINKEDIN_BASE}"
    "/jobs-guest/jobs/api/jobPosting"
)


def linkedin_detail_url(
    job_id
):

    return (
        f"{LINKEDIN_GUEST_DETAIL}"
        f"/{job_id}"
    )


# ============================================================
# RATE-LIMITED HTTP DETAIL FETCH
# ============================================================

async def fetch_detail_with_backoff(
    session,
    url,
    platform,
    semaphore
):

    async with semaphore:

        # Paces how fast this pool cycles through the detail queue,
        # independent of the semaphore's max-in-flight limit. Only
        # applied to LinkedIn: Wuzzuf has shown no rate-limit pressure
        # and already runs at higher concurrency (WUZZUF_DETAIL_CONCURRENCY).
        if platform == "LinkedIn":

            await asyncio.sleep(
                LINKEDIN_DETAIL_PACING
            )

        retry_429 = 0

        while True:

            try:

                if platform == "LinkedIn":

                    response = await linkedin_get(
                        session,
                        url,
                    )

                else:

                    response = await session.get(
                        url
                    )

            except Exception as exc:

                return (
                    None,
                    (
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )
                )

            status = (
                response.status
            )

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            if status == 200:

                return (
                    response,
                    None
                )

            # ------------------------------------------------
            # RATE LIMITED
            # ------------------------------------------------

            if status == 429:

                if (
                    retry_429
                    >= MAX_429_RETRIES
                ):

                    return (
                        None,
                        "HTTP 429 "
                        "after retries"
                    )

                retry_429 += 1

                # Exponential backoff with jitter (4s, 8s, 16s + up to
                # 1s random jitter) instead of the previous linear
                # 4/8/12s. Spreads out retries from concurrent detail
                # workers so they don't all re-hit LinkedIn at the same
                # moment, which was itself contributing to repeat 429s.
                wait_time = (
                    BACKOFF_429
                    * (2 ** (retry_429 - 1))
                    + random.uniform(0, 1)
                )

                print(
                    f"[{platform}] "
                    f"429 -> waiting "
                    f"{wait_time:.1f}s"
                )

                await asyncio.sleep(
                    wait_time
                )

                continue

            # ------------------------------------------------
            # OTHER HTTP ERROR
            # ------------------------------------------------

            return (
                None,
                f"HTTP {status}"
            )


# ============================================================
# WUZZUF DETAILS
# ============================================================

def parse_wuzzuf_detail(
    job,
    response
):

    try:

        # ----------------------------------------------------
        # STRUCTURED EXTRACTION ONLY - NO WHOLE-PAGE FALLBACK
        # ----------------------------------------------------
        # 1) JSON-LD JobPosting - clean and standard, but confirmed
        #    ABSENT on every sampled Wuzzuf page (0/45). Kept first
        #    in case Wuzzuf adds it later.
        # 2) The embedded React hydration state
        #    (Wuzzuf.initialStoreState) - confirmed PRESENT and the
        #    actual source of the visible job description on every
        #    sampled page. This is the real fix: the description text
        #    genuinely isn't in the server-rendered DOM or in any
        #    JSON-LD, only in this inline script's JSON blob.
        #
        # There is intentionally NO third tier that dumps the raw
        # page as a description. That fallback previously let CSS,
        # JS, nav chrome, and unrelated page content (up to hundreds
        # of KB) become a job's "Description" whenever both
        # structured extractions above missed - which fed 60k-70k
        # token payloads into the downstream LLM/guard pipeline and
        # caused Groq 413s. A failed extraction is not a job
        # description, degraded or otherwise: it's not data, so we
        # do not fabricate a substitute for it. If both structured
        # extractions fail, record it as a clean failure instead.

        structured = extract_jsonld_jobposting(
            response
        )

        if not structured or not structured.get("description"):
            structured = extract_wuzzuf_embedded_state(
                response
            )

        if structured and structured.get("description"):

            job["Description"] = structured["description"]

            if structured.get("title"):
                job["Title"] = structured["title"]

            if structured.get("company"):
                job["Company"] = structured["company"]

        else:

            job["Description"] = ""
            job["DetailError"] = (
                "Wuzzuf description extraction failed: no "
                "JSON-LD JobPosting and no usable "
                "initialStoreState job entry found. Refusing to "
                "fall back to the raw page as a description."
            )

        job["DetailStatus"] = (
            response.status
        )
        job["DetailFetcher"] = (
            "HTTP"
        )

    except Exception as exc:

        job["Description"] = ""
        job["DetailError"] = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )

    return job


# ============================================================
# LINKEDIN DETAILS
# ============================================================

def parse_linkedin_detail(
    job,
    response
):

    try:

        # ----------------------------------------------------
        # DESCRIPTION
        # ----------------------------------------------------

        description_nodes = (
            response.css(
                "[class*='description'] "
                "> section > div"
            )
            or response.css(
                ".show-more-less-html__markup"
            )
            or response.css(
                ".description__text"
            )
            # NOTE: a "loosest" fallback selector
            # (any [class*='description'] container) was tried here
            # and REVERTED. It matched unrelated elements whose class
            # merely happened to contain the word "description" (seen
            # in production as one-line ad-copy snippets and literal
            # "-" placeholders instead of real job descriptions), and
            # because it's part of this `or` chain it short-circuited
            # BEFORE the JSON-LD fallback below could run - so it was
            # actively worse than leaving `description_nodes` empty
            # and falling through. Do not re-add a selector here
            # without verifying against a real captured page first.
        )

        description = ""

        if description_nodes:

            # NOTE: was `clean_text(description_nodes[0].text)`.
            # `.text` on a scrapling Selector node only returns that
            # node's own DIRECT text, not descendant text - and this
            # markup nests the actual content inside child <p>/<li>/
            # <strong> tags with no direct text on the matched div
            # itself. That made `.text` come back as pure whitespace
            # (confirmed against real captured pages: 11-30 chars of
            # blank space) even though the real content - thousands
            # of characters - was sitting one level deeper. This was
            # silently producing empty descriptions on jobs whose
            # page/selectors were otherwise working correctly.
            # `.get_all_text()` walks descendants and got the real
            # content (3,010 / 5,149 chars on the two confirmed
            # cases). strip_html_tags() (not just clean_text()) as a
            # safety net in case any markup slips into the joined text.
            description = strip_html_tags(
                description_nodes[0].get_all_text()
            )

        # ----------------------------------------------------
        # CRITERIA
        # ----------------------------------------------------

        criteria = []

        criteria_nodes = (
            response.css(
                "[class*='_job-criteria-list']"
            )
            or response.css(
                ".description__job-criteria-item"
            )
        )

        for node in criteria_nodes:

            # Same `.text`-only-grabs-direct-children issue as the
            # description above - these criteria items nest their
            # actual label/value text inside child spans, so `.text`
            # returned blank for every item on the same sampled pages.
            text = clean_text(
                node.get_all_text()
            )

            if text:

                criteria.append(
                    text
                )

        # ----------------------------------------------------
        # FULL TEXT FALLBACK
        # ----------------------------------------------------

        full_text = ""

        try:

            full_text = clean_text(
                response.text
            )

        except Exception:

            pass

        # If the known selectors didn't turn up a description (page
        # layout variant, A/B test, etc.), fall back to any embedded
        # JobPosting JSON-LD rather than leaving it empty.

        if not description:

            structured = extract_jsonld_jobposting(
                response
            )

            if structured and structured.get("description"):
                description = structured["description"]

        # ----------------------------------------------------
        # LOGIN-WALL / BLOCKED-RESPONSE DETECTION
        # ----------------------------------------------------
        # A confirmed source of empty LinkedIn descriptions in
        # production: the archived rows had desc_len=0 even though a
        # live re-fetch of the same client/IP/impersonation returned
        # a fully populated page. The description selectors and the
        # JSON-LD fallback above are correct for a normal guest page -
        # they just have nothing to select on an authwall/consent/
        # redirect variant. Flag that case explicitly instead of
        # silently recording description="", so downstream tooling
        # can tell "genuinely no description" apart from "blocked
        # response, worth retrying on a later run" and can choose not
        # to mark the job as seen.

        looks_blocked = (
            not description
            and (
                "authwall" in full_text.lower()
                or "join now to see" in full_text.lower()
                or "sign in to view" in full_text.lower()
                or len(full_text) < 500
            )
        )

        if looks_blocked:

            print(
                "[LinkedIn] Empty description looks like a "
                "login-wall/blocked response, not a real empty "
                f"posting: {job.get('Link', '')}"
            )

        job["Description"] = (
            description
        )

        job["JobCriteria"] = (
            criteria
        )

        job["FullText"] = (
            full_text
        )

        job["DetailBlocked"] = (
            looks_blocked
        )

        job["DetailStatus"] = (
            response.status
        )

        job["DetailFetcher"] = (
            "HTTP"
        )

    except Exception as exc:

        job["Description"] = ""

        job["JobCriteria"] = []

        job["DetailError"] = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )

    return job


# ============================================================
# DETAIL WORKER
# ============================================================

async def detail_worker(
    session,
    job,
    semaphore,
    abort=None,
):

    platform = job.get(
        "Platform"
    )

    # --------------------------------------------------------
    # UPWORK
    # --------------------------------------------------------
    # Upwork has its own fetch path (curl_cffi + UTF-8 response
    # wrapper + Retry-After-aware backoff), so it must not go through
    # fetch_detail_with_backoff(). A blocked/failed detail keeps the
    # full list-card description instead of failing the job.

    if platform == "Upwork":

        # Set by scrape_upwork_details() after a run of blocked/failed
        # details: leave the remaining jobs on their card data instead of
        # hammering an endpoint that is already refusing us.
        if abort is not None and abort.get("flag"):
            return job

        url = job["Link"]

        response, error = await fetch_upwork(
            session,
            url,
            semaphore,
            pacing=UPWORK_DETAIL_PACING,
        )

        if response is None:

            return handle_upwork_detail_failure(
                job,
                error
            )

        return parse_upwork_detail(
            job,
            response
        )

    # --------------------------------------------------------
    # LINKEDIN
    # --------------------------------------------------------

    if platform == "LinkedIn":

        job_id = job.get(
            "JobID"
        )

        if not job_id:

            job_id = (
                extract_linkedin_job_id(
                    job["Link"]
                )
            )

            job["JobID"] = (
                job_id
            )

        if not job_id:

            job["DetailError"] = (
                "Could not determine LinkedIn "
                "job ID"
            )

            return job

        url = linkedin_detail_url(
            job_id
        )

    # --------------------------------------------------------
    # WUZZUF
    # --------------------------------------------------------

    else:

        url = job["Link"]

    response, error = (
        await fetch_detail_with_backoff(
            session,
            url,
            platform,
            semaphore
        )
    )

    if response is None:

        job["DetailError"] = (
            error
            or
            "Unknown detail error"
        )

        return job

    if platform == "Wuzzuf":

        return parse_wuzzuf_detail(
            job,
            response
        )

    if platform == "LinkedIn":

        return parse_linkedin_detail(
            job,
            response
        )

    return job


# ============================================================
# CONCURRENT DETAIL CRAWL
# ============================================================

async def scrape_all_details(
    jobs
):

    print("\n" + "=" * 70)
    print("CONCURRENT DETAIL CRAWL")
    print("=" * 70)

    if not jobs:

        return []

    wuzzuf_jobs = [
        job
        for job in jobs
        if job["Platform"]
        == "Wuzzuf"
    ]

    linkedin_jobs = [
        job
        for job in jobs
        if job["Platform"]
        == "LinkedIn"
    ]

    upwork_jobs = [
        job
        for job in jobs
        if job["Platform"]
        == "Upwork"
    ]

    # Only detail-crawl the newest handful (see LINKEDIN_DETAIL_MAX_PER_RUN).
    # Jobs arrive newest-first because build_linkedin_search_url() now
    # sends sortBy=DD, so a head-slice keeps the freshest postings.
    # Discovery (MAX_LINKEDIN_PAGES) is untouched - only how many of the
    # discovered jobs get the expensive per-job detail fetch.
    if (
        LINKEDIN_DETAIL_MAX_PER_RUN
        is not None
        and
        len(linkedin_jobs)
        > LINKEDIN_DETAIL_MAX_PER_RUN
    ):

        print(
            f"[LinkedIn] Capping detail crawl to newest "
            f"{LINKEDIN_DETAIL_MAX_PER_RUN} of "
            f"{len(linkedin_jobs)} discovered jobs."
        )

        linkedin_jobs = linkedin_jobs[
            :LINKEDIN_DETAIL_MAX_PER_RUN
        ]

    # Upwork cards already carry the full description, so the detail
    # fetch only enriches the newest handful (datePosted + client
    # country). Discovery stays at RUN_OPTIONS["max_upwork_pages"].
    upwork_snapshot_limit = RUN_OPTIONS.get(
        "upwork_snapshot_limit",
        UPWORK_DETAIL_MAX_PER_RUN,
    )

    if (
        upwork_snapshot_limit
        is not None
        and
        len(upwork_jobs)
        > upwork_snapshot_limit
    ):

        print(
            f"[Upwork] Capping snapshot to newest "
            f"{upwork_snapshot_limit} of "
            f"{len(upwork_jobs)} discovered jobs."
        )

        upwork_jobs = upwork_jobs[
            :upwork_snapshot_limit
        ]

    results = []

    # --------------------------------------------------------
    # Separate sessions / concurrency per platform.
    # --------------------------------------------------------

    async def scrape_wuzzuf_details():

        completed = []

        if not wuzzuf_jobs:
            return completed

        semaphore = asyncio.Semaphore(
            WUZZUF_DETAIL_CONCURRENCY
        )

        async with FetcherSession(
            impersonate="chrome",
            timeout=HTTP_TIMEOUT,
            retries=HTTP_RETRIES,
            retry_delay=HTTP_RETRY_DELAY,
            follow_redirects="safe",
        ) as session:

            tasks = [
                asyncio.create_task(
                    detail_worker(
                        session,
                        job,
                        semaphore
                    )
                )
                for job in wuzzuf_jobs
            ]

            processed = 0

            for future in asyncio.as_completed(
                tasks
            ):

                result = await future

                completed.append(
                    result
                )

                processed += 1

                if (
                    processed % 25 == 0
                    or
                    processed == len(
                        wuzzuf_jobs
                    )
                ):

                    successful = sum(
                        1
                        for item
                        in completed
                        if not item.get(
                            "DetailError"
                        )
                    )

                    print(
                        f"[Wuzzuf Detail] "
                        f"{processed}/"
                        f"{len(wuzzuf_jobs)} "
                        f"| OK={successful}"
                    )

        return completed

    async def scrape_linkedin_details():

        completed = []

        if not linkedin_jobs:
            return completed

        semaphore = asyncio.Semaphore(
            LINKEDIN_DETAIL_CONCURRENCY
        )

        async with linkedin_session() as session:

            tasks = [
                asyncio.create_task(
                    detail_worker(
                        session,
                        job,
                        semaphore
                    )
                )
                for job in linkedin_jobs
            ]

            processed = 0

            for future in asyncio.as_completed(
                tasks
            ):

                result = await future

                completed.append(
                    result
                )

                processed += 1

                if (
                    processed % 10 == 0
                    or
                    processed == len(
                        linkedin_jobs
                    )
                ):

                    successful = sum(
                        1
                        for item
                        in completed
                        if not item.get(
                            "DetailError"
                        )
                    )

                    print(
                        f"[LinkedIn Detail] "
                        f"{processed}/"
                        f"{len(linkedin_jobs)} "
                        f"| OK={successful}"
                    )

        return completed

    async def scrape_upwork_details():

        if not upwork_jobs:
            return []

        cache = load_upwork_detail_cache()

        # Serve already-enriched URLs from the cache: no request at all.
        targets = []
        for job in upwork_jobs:
            if not _apply_cached_upwork_detail(job, cache):
                targets.append(job)

        cache_hits = len(upwork_jobs) - len(targets)
        budget = RUN_OPTIONS.get(
            "upwork_detail_budget",
            UPWORK_DETAIL_MAX_PER_RUN,
        )
        if budget is not None:
            budget = max(0, int(budget))
            targets = targets[:budget]

        now = time.time()
        cooldown_until = float(cache.get("cooldown_until") or 0)
        if now < cooldown_until:
            print(
                f"[Upwork Detail] cooldown active for another "
                f"{cooldown_until - now:.0f}s; skipping "
                f"{len(targets)} detail fetch(es)."
            )
            save_upwork_detail_cache(cache)
            return upwork_jobs

        if not targets:
            print(
                f"[Upwork Detail] {cache_hits} job(s) served from cache; "
                f"no detail request needed."
            )
            save_upwork_detail_cache(cache)
            return upwork_jobs

        print(
            f"[Upwork Detail] {cache_hits} cache hit(s), "
            f"fetching {len(targets)} of {len(upwork_jobs)}."
        )

        semaphore = asyncio.Semaphore(
            UPWORK_DETAIL_CONCURRENCY
        )

        # Shared within this run: once enough consecutive details are
        # blocked/failed, stop spending requests (and persist a cooldown
        # so the next run does not immediately repeat the mistake).
        abort = {"flag": False}
        blocked_streak = {"count": 0}
        scraped = {}

        async with upwork_session() as session:

            tasks = [
                asyncio.create_task(
                    detail_worker(
                        session,
                        job,
                        semaphore,
                        abort=abort,
                    )
                )
                for job in targets
            ]

            processed = 0

            for future in asyncio.as_completed(
                tasks
            ):

                result = await future

                scraped[result.get("Link") or ""] = result

                ok = not (
                    result.get("DetailBlocked")
                    or result.get("DetailError")
                )

                if ok:
                    blocked_streak["count"] = 0
                else:
                    blocked_streak["count"] += 1
                    if (
                        blocked_streak["count"] >= UPWORK_BLOCK_STREAK_LIMIT
                        and not abort["flag"]
                    ):
                        abort["flag"] = True
                        cache["cooldown_until"] = (
                            time.time() + UPWORK_BLOCK_COOLDOWN_SECONDS
                        )
                        print(
                            f"[Upwork Detail] {blocked_streak['count']} "
                            f"consecutive blocked/failed details; "
                            f"pausing detail enrichment for "
                            f"{UPWORK_BLOCK_COOLDOWN_SECONDS}s."
                        )

                processed += 1

                if (
                    processed % 5 == 0
                    or
                    processed == len(
                        targets
                    )
                ):

                    successful = sum(
                        1
                        for item
                        in scraped.values()
                        if not item.get(
                            "DetailBlocked"
                        )
                        and not item.get(
                            "DetailError"
                        )
                    )

                    print(
                        f"[Upwork Detail] "
                        f"{processed}/"
                        f"{len(targets)} "
                        f"| OK={successful}"
                    )

        # Rebuild the snapshot slice in discovery order; jobs whose fetch
        # was skipped (budget/cooldown/abort) keep their card data.
        completed = [
            scraped.get(job.get("Link") or "", job)
            for job in upwork_jobs
        ]

        for job in completed:
            _remember_upwork_detail(job, cache)

        # Persist every run: loading prunes stale/card-only/over-cap
        # entries, and this write is what makes that pruning durable.
        save_upwork_detail_cache(cache)

        return completed

    wuzzuf_results, linkedin_results, upwork_results = (
        await asyncio.gather(
            scrape_wuzzuf_details(),
            scrape_linkedin_details(),
            scrape_upwork_details(),
        )
    )

    results.extend(
        wuzzuf_results
    )

    results.extend(
        linkedin_results
    )

    results.extend(
        upwork_results
    )

    return results


# ============================================================
# UPWORK SEARCH
# ============================================================

# Markers that indicate a bot challenge / block page rather than real
# content. Deliberately does NOT include the bare word "captcha":
# Upwork's normal search page embeds googleRecaptchaEnterpriseSiteKey
# in its Nuxt config, and matching that word marked every real page as
# a challenge.
UPWORK_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-chl",
    "enable javascript and cookies",
    "verify you are human",
    "checking your browser",
    "unusual traffic",
    "access denied",
    "attention required",
)

# Detail-specific login-wall markers. Applied only to detail responses,
# never to search pages (whose nav always mentions logging in).
UPWORK_DETAIL_BLOCK_MARKERS = (
    "sign in to view",
    "log in to view",
    "join upwork to see",
    "sign up to view",
    "login required",
    "authwall",
    "this job is no longer available",
    "this job has been closed",
)


class _UpworkResponse:
    """
    curl_cffi response wrapper with the same surface scrapling's
    Response object gives the extractors.

    Text is decoded explicitly as UTF-8: curl_cffi otherwise falls back
    to ISO-8859-1 on some responses, turning Upwork's en-dashes and
    bullets into mojibake before extraction even sees them.
    """

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
            self.text = self.body.decode(
                "utf-8",
                "replace"
            )
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

        lower = (self.text or "").lower()

        return any(
            marker in lower
            for marker in UPWORK_CHALLENGE_MARKERS
        )


def upwork_session():
    """Single spot that creates Upwork's HTTP client."""

    return CurlCffiAsyncSession()


def _first_text(nodes):

    if not nodes:
        return ""

    return clean_text(
        nodes[0].get_all_text()
    )


def _parse_retry_after(value):
    """Return a sane number of seconds from a Retry-After header."""

    if value is None:
        return None

    try:
        seconds = float(
            str(value).strip()
        )
    except (TypeError, ValueError):
        return None

    if seconds < 0:
        return None

    return min(
        seconds,
        UPWORK_MAX_BACKOFF_SECONDS
    )


async def fetch_upwork(
    session,
    url,
    semaphore,
    pacing=0.0,
):
    """
    Fetch one Upwork URL with the same guardrails as the other sources.

    A 429/503 is retried with exponential backoff + jitter (honouring
    Retry-After when present). A Cloudflare challenge, 401, 403 or 999
    is terminal for this request and reported as blocked/login-walled.
    """

    async with semaphore:

        if pacing:

            await asyncio.sleep(
                pacing
            )

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
                    f"{type(exc).__name__}: {exc}"
                )

            response = _UpworkResponse(
                raw
            )

            if (
                response.status == 200
                and
                not response.looks_challenged
            ):

                return (
                    response,
                    None
                )

            if (
                response.status == 200
                and
                response.looks_challenged
            ):

                return (
                    None,
                    "HTTP 200 but Cloudflare/bot-challenge page"
                )

            if response.status in (429, 503):

                if retry_429 >= MAX_429_RETRIES:

                    return (
                        None,
                        f"HTTP {response.status} after "
                        f"{MAX_429_RETRIES} retries"
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
                    UPWORK_MAX_BACKOFF_SECONDS
                )

                print(
                    f"[Upwork] {response.status} -> waiting "
                    f"{wait_time:.1f}s "
                    f"(retry {retry_429}/{MAX_429_RETRIES})"
                )

                await asyncio.sleep(
                    wait_time
                )

                continue

            if response.status in (401, 403, 999):

                return (
                    None,
                    f"HTTP {response.status} (blocked/login-walled)"
                )

            return (
                None,
                f"HTTP {response.status}"
            )


def build_upwork_search_url(
    keyword=None,
    page=1,
    per_page=UPWORK_PER_PAGE,
    job_type=UPWORK_JOB_TYPE,
):
    """
    Build a guest search URL.

    No location filter is applied: the caller asked for all jobs, and
    Upwork's guest search already returns a global result set. Its
    page size is capped at 50 even though the query accepts larger
    numbers (larger values silently fall back to 10).
    """

    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = UPWORK_PER_PAGE

    per_page = max(
        1,
        min(per_page, UPWORK_PER_PAGE)
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
    """
    Extract the URL id from ..._~022120881892029947726.

    The card's data-ev-job-uid is a different tracking id, so it is
    kept separately as CardUID and never used as JobID.
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
            return int(
                match.group(1).replace(",", "")
            )
        except (TypeError, ValueError):
            pass

    return None


def extract_upwork_next_url(response):

    nodes = (
        response.css(
            "link[rel='next']"
        )
        or response.css(
            "a[rel='next']"
        )
    )

    if not nodes:
        return ""

    href = get_attr(
        nodes[0],
        "href"
    )

    if not href:
        return ""

    return absolute_url(
        UPWORK_BASE,
        href
    )


def extract_upwork_jobs(response):
    """
    Extract one page of Upwork job cards.

    The card description is a structured field of the result card, not
    a raw page dump: Upwork puts the full posting text in
    p.body-medium. Confirmed on 50/50 sampled cards (many 1,500-8,000+
    chars, ending on a complete sentence), so it is safe to store as
    the job description even when a detail fetch is unavailable.
    CardText is kept as a diagnostic only.
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

        title = _first_text(
            title_nodes
        )

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

                href = get_attr(
                    node,
                    "href"
                )

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

            hourly_budget = clean_text(
                hourly_budget
            )

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

        if (
            budget
            and
            job_type == "Hourly"
            and
            "/hr" not in budget.lower()
        ):

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

            if (
                skill
                and
                skill not in seen_skills
            ):

                seen_skills.add(
                    skill
                )

                skills.append(
                    skill
                )

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

            # Guest search does not expose the client name/country.
            "Company":
                UPWORK_COMPANY_PLACEHOLDER,

            "Location":
                UPWORK_LOCATION_PLACEHOLDER,

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


async def scrape_upwork():

    print("\n" + "=" * 70)
    print("UPWORK SEARCH")
    print("=" * 70)

    all_jobs = []

    seen_urls = set()

    seen_signatures = set()

    page_results = []

    async with upwork_session() as session:

        semaphore = asyncio.Semaphore(
            UPWORK_SEARCH_CONCURRENCY
        )

        first = await fetch_upwork_search_page(
            session,
            semaphore,
            page=1,
            keyword=KEYWORD,
            per_page=UPWORK_PER_PAGE,
            job_type=UPWORK_JOB_TYPE,
        )

        page_results.append(
            first
        )

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

        # ----------------------------------------------------
        # PAGE COUNT
        # ----------------------------------------------------

        if total:

            pages_planned = ceil(
                total / max(1, int(UPWORK_PER_PAGE))
            )

        else:

            pages_planned = (
                (RUN_OPTIONS.get("max_upwork_pages") or MAX_UPWORK_PAGES)
                or 1
            )

        max_upwork_pages = RUN_OPTIONS.get(
            "max_upwork_pages",
            MAX_UPWORK_PAGES,
        )

        if max_upwork_pages is not None:

            pages_planned = min(
                pages_planned,
                max_upwork_pages
            )

        if (
            not first["next"]
            and
            not total
        ):

            pages_planned = 1

        pages_planned = max(
            1,
            pages_planned
        )

        print(
            f"[Upwork] Pages planned: "
            f"{pages_planned}"
        )

        if first["next"] and total:

            implied = ceil(
                total / max(1, int(UPWORK_PER_PAGE))
            )

            if pages_planned < implied:

                print(
                    f"[Upwork] Capped at max_pages="
                    f"{pages_planned} of "
                    f"{implied} implied pages."
                )

        # ----------------------------------------------------
        # REMAINING PAGES IN PARALLEL (small, gated pool)
        # ----------------------------------------------------

        if pages_planned > 1:

            tasks = [
                asyncio.create_task(
                    fetch_upwork_search_page(
                        session,
                        semaphore,
                        page=page,
                        keyword=KEYWORD,
                        per_page=UPWORK_PER_PAGE,
                        job_type=UPWORK_JOB_TYPE,
                    )
                )
                for page in range(
                    2,
                    pages_planned + 1
                )
            ]

            results = await asyncio.gather(
                *tasks,
                return_exceptions=True
            )

            for result in results:

                if isinstance(
                    result,
                    Exception
                ):

                    print(
                        "[Upwork] "
                        f"Page worker failed: "
                        f"{result}"
                    )

                    continue

                page_results.append(
                    result
                )

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

        if (
            signature
            and
            signature in seen_signatures
        ):

            print(
                f"[Upwork] Page {page} repeated; "
                "skipping."
            )

            continue

        seen_signatures.add(
            signature
        )

        new_count = 0

        for job in jobs:

            link = job["Link"]

            if link in seen_urls:
                continue

            seen_urls.add(
                link
            )

            all_jobs.append(
                job
            )

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

    return all_jobs


# ============================================================
# UPWORK DETAIL EXTRACTION
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
            "keeping the full list-card description."
        )

    else:

        _keep_card_description(
            job,
            f"Detail fetch failed ({error}); "
            "keeping the full list-card description."
        )

    return job


def parse_upwork_detail(
    job,
    response
):
    """
    Structured detail extraction only.

    Tier 1: JobPosting JSON-LD.
    Tier 2: known Upwork description containers.
    No tier 3: the raw page is never used as a description.

    Detail pages are intermittently accessible to guests (confirmed
    4/5 HTTP 200 in live sampling, with the rest 403), so a blocked
    detail keeps the full list-card description instead of failing the
    whole job.
    """

    try:

        description = ""

        source = ""

        structured = extract_jsonld_jobposting(
            response
        )

        if (
            structured
            and
            structured.get("description")
        ):

            description = structured["description"]

            source = "detail_jsonld"

            if structured.get("title"):
                job["Title"] = structured["title"]

            # Upwork's JobPosting always names Upwork itself as the
            # hiringOrganization, not the real client. Never copy that
            # into Company; leave the list-card placeholder alone.
            company = structured.get("company") or ""

            if (
                company
                and
                company.strip().lower() != "upwork"
            ):

                job["Company"] = company

            if structured.get("datePosted"):

                job["DatePosted"] = structured["datePosted"]

        else:

            nodes = response.css(
                "[data-test='Description'], "
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

        # ----------------------------------------------------
        # CLIENT LOCATION
        # ----------------------------------------------------
        # Only the detail page exposes it; the search card does not.
        # Recorded as its own diagnostic rather than overwriting the
        # card's placeholder Location.

        location_nodes = (
            response.css(
                "[data-qa='client-location'] strong"
            )
            or response.css(
                "[data-qa='client-location']"
            )
        )

        if location_nodes:

            client_location = _first_text(
                location_nodes
            )

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
            for marker in UPWORK_DETAIL_BLOCK_MARKERS
        )

        if blocked:

            job["DetailBlocked"] = True

            _keep_card_description(
                job,
                "Detail page is login-walled/blocked for guests; "
                "keeping the full list-card description."
            )

        else:

            _keep_card_description(
                job,
                "Detail extraction found no structured description; "
                "keeping the full list-card description."
            )

        return job

    except Exception as exc:

        _keep_card_description(
            job,
            f"Detail parse error ({type(exc).__name__}: {exc}); "
            "keeping the full list-card description."
        )

        return job


# ============================================================
# GLOBAL DEDUPLICATION
# ============================================================

def deduplicate_jobs(
    jobs
):

    unique = []

    seen = set()

    for job in jobs:

        platform = job.get(
            "Platform",
            ""
        )

        link = canonical_url(
            job.get(
                "Link",
                ""
            )
        )

        if not link:
            continue

        key = (
            platform,
            link
        )

        if key in seen:
            continue

        seen.add(key)

        job["Link"] = link

        unique.append(
            job
        )

    return unique


# ============================================================
# CROSS-RUN "SEEN" TRACKING (diagnostic + real new-job detection)
# ============================================================
#
# jobs_results.json is a full snapshot of the current poll and its
# schema is unchanged. This sidecar file is separate and only used to
# report how many of today's jobs are actually new relative to prior
# polls - the real question behind "no new jobs are ever discovered",
# independent of whatever order either site returns results in.

SEEN_STATE_FILE = "seen_jobs_state.json"


def load_seen_keys(filename=SEEN_STATE_FILE):

    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as file:

            return set(
                json.load(file)
            )

    except (FileNotFoundError, json.JSONDecodeError):

        return set()


def save_seen_keys(keys, filename=SEEN_STATE_FILE):

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
    """
    Compares this run's (Platform, Link) pairs against seen_keys.
    Returns (new_count, updated_seen_keys). Does not mutate `jobs`.
    """

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


# ============================================================
# OUTPUT SCHEMA
# ============================================================
#
# Internal fields (Title/Company/Description/Link/Platform/...) stay
# as they are - this only shapes what actually gets written out, so
# jobs_results.json holds clean records instead of the raw page/
# internal-tracking noise.


# Even after HTML stripping, some pages can still yield a lot of
# text. Cap at write time to keep jobs_results.json / the DB small;
# the app-side 32 KB classification cap already protects the event
# loop independently of this.
MAX_STORED_DESCRIPTION_CHARS = 200_000


def build_output_record(job):

    title = clean_text(
        job.get("Title", "")
    )

    # Belt & braces: strip tags here too, so the emitted schema is
    # guaranteed clean text regardless of what any detail parser put
    # into the internal "Description" field.
    description = strip_html_tags(
        job.get("Description", "")
    )[:MAX_STORED_DESCRIPTION_CHARS]

    company = clean_text(
        job.get("Company", "")
    )

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


def to_output_schema(jobs):

    # Skip jobs whose detail fetch hit a login-wall/blocked response
    # (DetailBlocked) or failed outright for any other reason
    # (DetailError -- HTTP failure, parse failure, missing job id,
    # or Wuzzuf structured-description extraction failing), *unless*
    # the job still has a real description from discovery.
    #
    # For Wuzzuf/LinkedIn a failed detail leaves Description == "", so
    # these are still dropped. Upwork is the exception: its search
    # cards already carry the full posting text, so a blocked detail
    # (confirmed 1-in-5 live) must keep the job rather than throw away
    # a perfectly usable description. Emitting an empty description
    # would let the app permanently commit a bad classification for
    # that URL, so the empty case is still excluded and retried on the
    # next scrape.
    def _is_failed_detail(job):
        return bool(
            (job.get("DetailBlocked") or job.get("DetailError"))
            and not job.get("Description")
        )

    skipped = sum(1 for job in jobs if _is_failed_detail(job))

    if skipped:
        print(
            f"[Output] Skipping {skipped} failed/blocked detail fetch(es) "
            f"this run; they will be retried on the next scrape."
        )

    return [
        build_output_record(job)
        for job in jobs
        if not _is_failed_detail(job)
    ]


# ============================================================
# SAVE
# ============================================================

def save_results(
    jobs,
    filename=OUTPUT_FILE
):

    if set(_selected_sources()) != set(ALL_SOURCES):

        kept = _merge_snapshot(
            filename,
            jobs,
        )

        print(
            f"\nSaved {len(jobs)} "
            f"jobs to {filename} "
            f"(kept {kept} record(s) from "
            f"sources not scraped this run)"
        )

        return

    _atomic_write_json(
        filename,
        jobs,
    )

    print(
        f"\nSaved {len(jobs)} "
        f"jobs to {filename}"
    )


# ============================================================
# MAIN PIPELINE
# ============================================================

async def unified_job_scraper():

    started = (
        asyncio.get_running_loop()
        .time()
    )

    print("\n" + "=" * 70)
    print("HIGH-SPEED JOB CRAWLER")
    print("=" * 70)

    print(
        f"Keyword: "
        f"{KEYWORD if KEYWORD else 'ALL JOBS'}"
    )

    print(
        f"Location: {LOCATION} "
        f"(Upwork is global: no location filter)"
    )

    # --------------------------------------------------------
    # 1. DISCOVERY
    # --------------------------------------------------------

    selected_sources = _selected_sources()

    print(
        f"Sources:        "
        f"{', '.join(selected_sources)}"
    )

    fetchers = {
        "wuzzuf": scrape_wuzzuf,
        "linkedin": scrape_linkedin,
        "upwork": scrape_upwork,
    }

    discovery_tasks = {
        name: asyncio.create_task(fetchers[name]())
        for name in selected_sources
    }

    wuzzuf_jobs = (
        await discovery_tasks["wuzzuf"]
        if "wuzzuf" in discovery_tasks
        else []
    )

    linkedin_jobs = (
        await discovery_tasks["linkedin"]
        if "linkedin" in discovery_tasks
        else []
    )

    upwork_jobs = (
        await discovery_tasks["upwork"]
        if "upwork" in discovery_tasks
        else []
    )

    discovered = (
        wuzzuf_jobs
        + linkedin_jobs
        + upwork_jobs
    )

    print("\n" + "=" * 70)

    print(
        f"Discovered before dedup: "
        f"{len(discovered)}"
    )

    # --------------------------------------------------------
    # 2. DEDUP
    # --------------------------------------------------------

    discovered = deduplicate_jobs(
        discovered
    )

    print(
        f"Unique discovered: "
        f"{len(discovered)}"
    )

    # --------------------------------------------------------
    # 2b. CROSS-RUN NEW-JOB DIAGNOSTIC (does not affect output file)
    # --------------------------------------------------------

    seen_keys = load_seen_keys()

    _, updated_seen_keys = report_new_jobs(
        discovered,
        seen_keys
    )

    save_seen_keys(updated_seen_keys)

    # --------------------------------------------------------
    # 3. YOUR FILTER
    # --------------------------------------------------------

    filtered = (
        filter_before_details(
            discovered
        )
    )

    # --------------------------------------------------------
    # 4. EXPENSIVE DETAIL CRAWL
    # --------------------------------------------------------

    detailed = (
        await scrape_all_details(
            filtered
        )
    )

    detailed = deduplicate_jobs(
        detailed
    )

    # --------------------------------------------------------
    # 5. SUMMARY
    # --------------------------------------------------------

    wuzzuf_count = sum(
        1
        for job in detailed
        if job["Platform"]
        == "Wuzzuf"
    )

    linkedin_count = sum(
        1
        for job in detailed
        if job["Platform"]
        == "LinkedIn"
    )

    upwork_count = sum(
        1
        for job in detailed
        if job["Platform"]
        == "Upwork"
    )

    detail_errors = sum(
        1
        for job in detailed
        if job.get(
            "DetailError"
        )
    )

    detail_blocked = sum(
        1
        for job in detailed
        if job.get(
            "DetailBlocked"
        )
    )

    detail_excluded = sum(
        1
        for job in detailed
        if (
            job.get("DetailBlocked")
            or job.get("DetailError")
        )
        and not job.get("Description")
    )

    detail_kept_on_card = sum(
        1
        for job in detailed
        if (
            job.get("DetailBlocked")
            or job.get("DetailError")
        )
        and job.get("Description")
    )

    elapsed = (
        asyncio.get_running_loop()
        .time()
        - started
    )

    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    print(
        f"Discovered:     "
        f"{len(discovered)}"
    )

    print(
        f"Passed filter:  "
        f"{len(filtered)}"
    )

    print(
        f"Wuzzuf:         "
        f"{wuzzuf_count}"
    )

    print(
        f"LinkedIn:       "
        f"{linkedin_count}"
    )

    print(
        f"Upwork:         "
        f"{upwork_count}"
    )

    print(
        f"Detail errors:  "
        f"{detail_errors}"
    )

    print(
        f"Detail blocked: "
        f"{detail_blocked}"
    )

    print(
        f"Kept on search-card description: "
        f"{detail_kept_on_card}"
    )

    print(
        f"Excluded from output (no usable description): "
        f"{detail_excluded}"
    )

    print(
        f"Elapsed:        "
        f"{elapsed:.1f}s"
    )

    # --------------------------------------------------------
    # 6. SAVE
    # --------------------------------------------------------

    output_records = to_output_schema(
        detailed
    )

    save_results(
        output_records
    )

    return output_records


# ============================================================
# RUN
# ============================================================

def parse_args(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Scrape Wuzzuf/LinkedIn/Upwork job listings into "
            "jobs_results.json."
        )
    )

    parser.add_argument(
        "--sources",
        default="all",
        help=(
            "Comma-separated subset of: "
            f"{', '.join(ALL_SOURCES)} (default: all). "
            "A partial run merges its records into the existing "
            "snapshot instead of overwriting it."
        ),
    )

    parser.add_argument(
        "--max-upwork-pages",
        type=int,
        default=None,
        help="Override MAX_UPWORK_PAGES for this run.",
    )

    parser.add_argument(
        "--upwork-detail-max",
        type=int,
        default=None,
        help=(
            "Maximum number of uncached Upwork detail pages to fetch "
            "this run."
        ),
    )

    parser.add_argument(
        "--upwork-snapshot-limit",
        type=int,
        default=None,
        help=(
            "How many newest Upwork jobs make it into the snapshot "
            "(independent of the detail budget)."
        ),
    )

    return parser.parse_args(argv)


def configure_run(args):

    try:
        sources = _normalize_source_list(args.sources)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")

    RUN_OPTIONS["sources"] = sources

    if args.max_upwork_pages is not None:
        RUN_OPTIONS["max_upwork_pages"] = max(
            1,
            int(args.max_upwork_pages),
        )

    if args.upwork_detail_max is not None:
        RUN_OPTIONS["upwork_detail_budget"] = max(
            0,
            int(args.upwork_detail_max),
        )

    if args.upwork_snapshot_limit is not None:
        RUN_OPTIONS["upwork_snapshot_limit"] = max(
            0,
            int(args.upwork_snapshot_limit),
        )


def main(argv=None):

    configure_run(parse_args(argv))

    results = asyncio.run(
        unified_job_scraper()
    )

    print("\nDONE.")

    return results


if __name__ == "__main__":

    main()