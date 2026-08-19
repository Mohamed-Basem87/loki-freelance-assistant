"""Category matching orchestration.

This module separates the shared tiered keyword engine from category
selection. Keyword classification is deterministic and can be run
against every enabled category. An LLM is deliberately NOT called here:
when more than one category remains plausible, the caller can perform
one future category-arbitration LLM call and use its returned category.
"""

from app.categories.registry import enabled_categories
from app.filters import keyword_filter


def classify_categories(text, title=""):
    """Return deterministic results for every enabled category."""
    return {
        profile.id: {
            "category_id": profile.id,
            "category_name": profile.name,
            "result": keyword_filter(text, title=title, profile=profile),
        }
        for profile in enabled_categories()
    }


def select_category(results):
    """Select a single category from deterministic results.

    Returns:
      - ``category_id`` when exactly one category is a confident direct
        match.
      - ``None`` when the job is a clear rejection or requires
        category arbitration.

    The ambiguous case intentionally has no category selected. A later
    single LLM call will choose the category from the available
    profiles rather than asking the LLM once per category.
    """
    direct = [
        item for item in results.values()
        if item["result"]["decision"] == "notify_directly"
    ]
    ambiguous = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]

    # A job may end up in exactly one category. A direct match is only
    # final when every other category is a clear deterministic reject.
    # If another category is still plausible, defer the whole category
    # choice to the single future arbitration LLM call.
    if len(direct) == 1 and not ambiguous:
        return direct[0]["category_id"]

    return None


def classify_and_select(text, title=""):
    results = classify_categories(text, title=title)
    category_id = select_category(results)

    needs_arbitration = any(
        item["result"]["decision"] == "needs_gemini"
        for item in results.values()
    )

    llm_candidates = [
        item for item in results.values()
        if item["result"]["decision"] == "needs_gemini"
    ]

    return {
        "categories": results,
        "category_id": category_id,
        "llm_candidate_category_id": (
            llm_candidates[0]["category_id"] if len(llm_candidates) == 1 else None
        ),
        "needs_category_arbitration": category_id is None and needs_arbitration,
        "has_direct_match": category_id is not None,
        "candidate_category_ids": list(results.keys()),
    }
