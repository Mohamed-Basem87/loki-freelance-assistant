"""Category matching and deterministic selection orchestration."""

from app.domain.categories.registry import deterministic_categories
from app.domain.filters import (
    MIXED_CATEGORY_AMBIGUOUS_DIRECT_SCORE_THRESHOLD,
    MIXED_CATEGORY_CLAIM_DIRECT_SCORE_THRESHOLD,
    MIXED_CATEGORY_MIN_SCORE_MARGIN,
    category_select_score,
    keyword_filter,
)


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


def _select_category_internal(results, text="", title=""):
    """Shared implementation for select_category().

    Returns (category_id, tiebreak_resolved). `tiebreak_resolved` is True
    when the pick came from the deterministic resolution path (lone
    ambiguous candidate or multi-candidate highest-score pick) -- a clean
    single notify_directly match and arbitration yields are False, so the
    notification path can tell an auto-resolved pick apart for
    guard-fallback routing.
    """
    direct = [
        item for item in results.values()
        if item["result"]["decision"] == "notify_directly"
    ]
    ambiguous = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]

    if len(direct) == 1 and not ambiguous:
        return direct[0]["category_id"], False

    if len(direct) == 0 and len(ambiguous) == 1:
        # A lone needs_gemini candidate is auto-resolved to that category.
        # Historically ~2/3 of this population was rejected, but the guard
        # gates keyword-direct picks downstream, so the deterministic pick
        # still gets filtered before delivery.
        return ambiguous[0]["category_id"], True

    candidates = direct + ambiguous
    if not candidates:
        # Zero candidates: no deterministic category matched the job.
        return None, False

    top = None
    second = None
    for item in candidates:
        score = category_select_score(item["result"])
        if top is None or score > top[0]:
            second, top = top, (score, item["category_id"])
        elif second is None or score > second[0]:
            second = (score, item["category_id"])

    top_score, top_id = top
    runner_up_score = second[0] if second else float("-inf")
    margin = top_score - runner_up_score
    top_result = next(
        item["result"] for item in candidates if item["category_id"] == top_id
    )

    threshold = (
        MIXED_CATEGORY_CLAIM_DIRECT_SCORE_THRESHOLD
        if top_result["decision"] == "notify_directly"
        else MIXED_CATEGORY_AMBIGUOUS_DIRECT_SCORE_THRESHOLD
    )

    if top_score >= threshold and margin >= MIXED_CATEGORY_MIN_SCORE_MARGIN:
        return top_id, True

    return None, False


def select_category(results, text="", title=""):
    """Return a final deterministic category when the match is unambiguous.

    Priority order:
      1. Exactly one notify_directly and no needs_gemini candidate: that
         category wins (unchanged behavior).
      2. Zero candidates (all profiles rejected the job): None.
      3. One lone needs_gemini candidate: that category wins directly.
      4. Multiple candidates: pick the highest-scoring one when it clears
         the score threshold for its structure and beats the runner-up by
         MIXED_CATEGORY_MIN_SCORE_MARGIN. Anything else goes to the LLM.
    """
    category_id, _ = _select_category_internal(results, text, title)
    return category_id


def classify_and_select(text, title=""):
    results = classify_categories(text, title=title)
    category_id, tiebreak_resolved = _select_category_internal(
        results, text=text, title=title
    )
    llm_candidates = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]
    direct_count = sum(
        1 for item in results.values()
        if item["result"]["decision"] == "notify_directly"
    )
    ambiguous_count = sum(
        1 for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    )
    # A TRUE multi-candidate tie-break: the pick came from a fight between
    # >= 2 competing categories (any scored resolution), NOT a single
    # clean direct match or a lone needs_gemini candidate. Only these
    # deserve the guard-fallback rescue when the guard rejects them.
    multi_candidate_resolved = bool(category_id) and (direct_count + ambiguous_count) >= 2

    return {
        "categories": results,
        "category_id": category_id,
        "tiebreak_resolved": tiebreak_resolved,
        "multi_candidate_resolved": multi_candidate_resolved,
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
