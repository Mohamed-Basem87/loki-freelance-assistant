"""Category matching and deterministic selection orchestration."""

from app.categories.registry import deterministic_categories
from app.filters import keyword_filter


# Bounds for the deterministic keyword-filter input. Scraper-backed sources can
# ship full HTML pages as descriptions (megabytes of markup dominated by
# boilerplate after the title/intro), and keyword_filter() runs one full-text
# regex pass per keyword per tier per profile on the asyncio event loop. Without
# a bound, a burst of fresh scraped jobs can stall the loop for minutes and trip
# the container healthcheck. Truncating keeps each classification cost bounded
# and source-agnostic while the meaningful content (title + body lead) is
# preserved; the authoritative title is capped far tighter because it is short.
MAX_CLASSIFY_TEXT_CHARS = 32_000
MAX_CLASSIFY_TITLE_CHARS = 2_000


def _bounded_input(text, title):
    if text is not None and len(text) > MAX_CLASSIFY_TEXT_CHARS:
        text = text[:MAX_CLASSIFY_TEXT_CHARS]
    if title is not None and len(title) > MAX_CLASSIFY_TITLE_CHARS:
        title = title[:MAX_CLASSIFY_TITLE_CHARS]
    return text, title


def classify_categories(text, title=""):
    """Return deterministic results for every deterministic category."""
    text, title = _bounded_input(text, title)
    return {
        profile.id: {
            "category_id": profile.id,
            "category_name": profile.name,
            "result": keyword_filter(text, title=title, profile=profile),
        }
        for profile in deterministic_categories()
    }


def select_category(results):
    """Return a final deterministic category only when it is unambiguous."""
    direct = [
        item for item in results.values()
        if item["result"]["decision"] == "notify_directly"
    ]
    ambiguous = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]

    if len(direct) == 1 and not ambiguous:
        return direct[0]["category_id"]

    # Zero, multiple direct matches, or any ambiguous candidate all
    # require the caller to resolve the category explicitly.
    return None


def classify_and_select(text, title=""):
    results = classify_categories(text, title=title)
    category_id = select_category(results)
    llm_candidates = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]

    return {
        "categories": results,
        "category_id": category_id,
        "llm_candidate_category_ids": [
            item["category_id"] for item in llm_candidates
        ],
        # Backward-compatible convenience field for callers/tests that
        # still inspect the single-candidate case.
        "llm_candidate_category_id": (
            llm_candidates[0]["category_id"] if len(llm_candidates) == 1 else None
        ),
        "needs_category_arbitration": category_id is None and (
            bool(llm_candidates) or len([
                item for item in results.values()
                if item["result"]["decision"] == "notify_directly"
            ]) > 1
        ),
        "has_direct_match": category_id is not None,
        "candidate_category_ids": list(results.keys()),
    }
