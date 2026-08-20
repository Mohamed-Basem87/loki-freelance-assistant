"""Category matching and deterministic selection orchestration."""

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
