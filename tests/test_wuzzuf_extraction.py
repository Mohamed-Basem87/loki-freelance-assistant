"""
Regression tests for the Wuzzuf description-extraction fix.

Context (see scraper-html-description-bug-report.md and follow-up
findings): parse_wuzzuf_detail() used to fall back to storing the
ENTIRE raw HTML page as a job's "Description" whenever structured
extraction failed. That raw page could contain CSS, JavaScript, nav
chrome, and unrelated content - up to hundreds of KB - which fed
60k-70k token payloads into the downstream LLM/guard pipeline and
caused Groq 413s.

The fix removes that fallback entirely. On extraction failure,
Description must be "" and DetailError must be set - never a
substitute payload built from the page itself.

These tests are self-contained: they build minimal synthetic HTML
fixtures that mimic Wuzzuf's actual page structure (confirmed via a
real captured page: no JSON-LD, description lives inside an inline
`Wuzzuf.initialStoreState = {...}` script) rather than depending on
a large external fixture file, so they run anywhere scraper.py's
dependencies (scrapling) are installed.

Run with: pytest test_wuzzuf_extraction.py -v
"""

import json

import pytest
from scrapling.parser import Selector

from scraper import (
    build_output_record,
    extract_jsonld_jobposting,
    extract_wuzzuf_embedded_state,
    parse_wuzzuf_detail,
    strip_html_tags,
)


# ================================================================
# Fixtures / helpers
# ================================================================

class FakeResponse:
    """
    Minimal stand-in for the scrapling response object, exposing
    only what parse_wuzzuf_detail() and its helpers actually use:
    .css(), .text, .body, .status.
    """

    def __init__(self, html, status=200):
        self._html = html
        self.status = status

    @property
    def text(self):
        return self._html

    @property
    def body(self):
        return self._html.encode("utf-8")

    def css(self, selector):
        return Selector(self._html).css(selector)


PAGE_CHROME = """
<!DOCTYPE html>
<html lang="en">
<head>
<title>Some Job title | Apply on Wuzzuf</title>
<style>
html, body { padding: 0; margin: 0; font-family: 'Open Sans', sans-serif; }
@keyframes spin { to { transform: rotate(360deg); } }
.entry-loader { z-index: 999999; }
</style>
</head>
<body>
<header>Browse Jobs For Companies Log in Get Started</header>
"""

PAGE_FOOTER = """
<footer>Members Directory: a b c ... © 2026 WUZZUF. All Rights Reserved.</footer>
<script>
(function () {
    function trackClick(x) { window.dataLayer.push(x); }
    trackClick({ event: "pageview" });
})();
</script>
</body>
</html>
"""


def make_wuzzuf_page(
    job_id="1293104f-0a6e-497b-935e-c0178c527c02",
    uri="jobs/p/gn3elcws7lfz-calibration-specialist",
    server_rendered_url="/jobs/p/gn3elcws7lfz-calibration-specialist",
    description_en="Responsible for quality of service and accuracy of results.",
    title="Calibration Specialist",
    include_similar_jobs_key=True,
    include_uri=True,
    extra_decoy_job=True,
):
    """
    Build a synthetic Wuzzuf job page with an embedded
    initialStoreState blob shaped like the real thing, so the tests
    exercise the actual matching logic (jobPage.similarJobs primary
    key, uri-substring fallback) without needing a huge fixture file.
    """

    job_collection = {
        job_id: {
            "attributes": {
                "title": title,
                "uri": uri if include_uri else "",
                "description": f"<p>{description_en}</p>",
                "userContentTranslations": {
                    "description": {
                        "en": description_en,
                        "confidence": 1,
                    }
                },
            }
        }
    }

    if extra_decoy_job:
        job_collection["decoy-similar-job-id"] = {
            "attributes": {
                "title": "Some Other Similar Job",
                "uri": "jobs/p/some-other-similar-job",
                "description": "<p>Unrelated similar job description.</p>",
                "userContentTranslations": {
                    "description": {
                        "en": "Unrelated similar job description.",
                        "confidence": 1,
                    }
                },
            }
        }

    state = {
        "entities": {
            "job": {
                "collection": job_collection,
            }
        },
        "jobPage": {
            "similarJobs": (
                {job_id: {"similar": {"ids": ["decoy-similar-job-id"]}}}
                if include_similar_jobs_key
                else {}
            )
        },
    }

    state_json = json.dumps(state)

    script = (
        "<script>(function(){ var Wuzzuf = window.Wuzzuf = "
        "window.Wuzzuf || {}; "
        f"Wuzzuf.initialStoreState = {state_json}; "
        f'Wuzzuf.serverRenderedURL = "{server_rendered_url}"; '
        "})();</script>"
    )

    return PAGE_CHROME + script + PAGE_FOOTER


# ================================================================
# strip_html_tags: script/style content must never leak through
# ================================================================

def test_strip_html_tags_drops_script_and_style_content():
    html = (
        "<style>.foo { color: red; font-family: 'Open Sans'; } "
        "@keyframes spin { to { transform: rotate(360deg); } }</style>"
        "<script>function track(x) { doSomething(x); }</script>"
        "<p>Actual job text here.</p>"
    )

    result = strip_html_tags(html)

    assert "font-family" not in result
    assert "@keyframes" not in result
    assert "function track" not in result
    assert "doSomething" not in result
    assert "Actual job text here." in result


# ================================================================
# extract_wuzzuf_embedded_state: correctness of job identification
# ================================================================

def test_embedded_state_uses_similarjobs_key_as_primary_match():
    """
    jobPage.similarJobs's own top-level key IS the viewed job's ID.
    This must be preferred over URL-substring matching, since it was
    observed to correctly identify the job on page variants (e.g.
    confidential/hidden-title postings) where URL matching failed.
    """
    html = make_wuzzuf_page()
    response = FakeResponse(html)

    result = extract_wuzzuf_embedded_state(response)

    assert result is not None
    assert result["title"] == "Calibration Specialist"
    assert result["description"] == (
        "Responsible for quality of service and accuracy of results."
    )
    # Must not pick up the decoy "similar job" sidebar entry.
    assert "Unrelated similar job" not in result["description"]


def test_embedded_state_falls_back_to_url_match_when_no_similarjobs_key():
    html = make_wuzzuf_page(include_similar_jobs_key=False)
    response = FakeResponse(html)

    result = extract_wuzzuf_embedded_state(response)

    assert result is not None
    assert result["description"] == (
        "Responsible for quality of service and accuracy of results."
    )


def test_embedded_state_returns_none_when_nothing_matches():
    """
    Neither the similarJobs key nor the URI match the target, and
    there's more than one candidate job - extraction should refuse
    to guess, not silently pick the wrong one.
    """
    html = make_wuzzuf_page(
        include_similar_jobs_key=False,
        include_uri=False,
        server_rendered_url="/jobs/p/totally-different-slug",
    )
    response = FakeResponse(html)

    result = extract_wuzzuf_embedded_state(response)

    assert result is None


def test_embedded_state_returns_none_when_no_state_blob_present():
    response = FakeResponse(PAGE_CHROME + PAGE_FOOTER)

    assert extract_wuzzuf_embedded_state(response) is None
    assert extract_jsonld_jobposting(response) is None


# ================================================================
# parse_wuzzuf_detail: the actual regression - no page-dump fallback
# ================================================================

def test_parse_wuzzuf_detail_extracts_clean_description_on_success():
    html = make_wuzzuf_page()
    response = FakeResponse(html)
    job = {}

    result = parse_wuzzuf_detail(job, response)

    assert result["Description"] == (
        "Responsible for quality of service and accuracy of results."
    )
    assert "DetailError" not in result
    assert len(result["Description"]) < 200  # nowhere near page-dump size


def test_parse_wuzzuf_detail_fails_clean_when_extraction_fails():
    """
    The core regression: when both structured extraction paths miss,
    Description must be empty and DetailError must be set - the raw
    page must NEVER become the description, regardless of how large
    or CSS/JS-laden that page is.
    """
    # A large page with plenty of CSS/JS/nav noise and NO usable
    # JSON-LD or initialStoreState - simulates a genuine extraction
    # failure (e.g. Wuzzuf changes their bundler output).
    huge_noisy_page = PAGE_CHROME + ("<div>filler text</div>" * 5000) + PAGE_FOOTER
    response = FakeResponse(huge_noisy_page)
    job = {}

    result = parse_wuzzuf_detail(job, response)

    assert result["Description"] == ""
    assert "DetailError" in result
    assert "extraction failed" in result["DetailError"].lower()

    # The literal regression this test exists to catch: the fix must
    # not just shrink the leaked content, it must eliminate it.
    assert "font-family" not in result["Description"]
    assert "@keyframes" not in result["Description"]
    assert len(result["Description"]) == 0


def test_parse_wuzzuf_detail_never_returns_page_scale_description():
    """
    Even if some future regression reintroduces a fallback, this
    guards the invariant directly: Description length must stay
    small relative to a real scraped page (the original bug produced
    350KB-650KB descriptions from ~1-2KB pages of actual content).
    """
    html = make_wuzzuf_page()
    response = FakeResponse(html)
    job = {}

    result = parse_wuzzuf_detail(job, response)

    assert len(result["Description"]) < 10_000, (
        "Description is suspiciously large - possible reintroduction "
        "of the whole-page-fallback bug."
    )


def test_build_output_record_never_exceeds_size_cap():
    """
    Belt-and-braces check on the output boundary itself (independent
    of the extraction fix above): build_output_record() must never
    emit a description larger than MAX_STORED_DESCRIPTION_CHARS, even
    if some other code path manages to put something huge into
    job["Description"].
    """
    from scraper import MAX_STORED_DESCRIPTION_CHARS

    job = {
        "Title": "Test Job",
        "Description": "x" * (MAX_STORED_DESCRIPTION_CHARS + 50_000),
        "Company": "Test Co",
        "Platform": "Wuzzuf",
        "Link": "https://example.com/job",
    }

    record = build_output_record(job)

    assert len(record["description"]) <= MAX_STORED_DESCRIPTION_CHARS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
