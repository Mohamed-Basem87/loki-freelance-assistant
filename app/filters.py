from app.keywords import (
    HARD_REJECT_KEYWORDS,
    INTEREST_CATEGORIES,
    SOFT_NEGATIVE_KEYWORDS,
)
from app.normalize import normalize


def keyword_filter(text: str):
    normalized = normalize(text)

    score = 0

    matched_categories = set()
    positive_matches = []
    soft_negative_matches = []
    hard_reject_matches = []

    # Positive keywords
    for category, keywords in INTEREST_CATEGORIES.items():
        for keyword, weight in keywords.items():
            if normalize(keyword) in normalized:
                score += weight
                matched_categories.add(category)
                positive_matches.append({
                    "keyword": keyword,
                    "weight": weight,
                    "category": category,
                })

    # Soft negatives
    for keyword, penalty in SOFT_NEGATIVE_KEYWORDS.items():
        if normalize(keyword) in normalized:
            score += penalty
            soft_negative_matches.append({
                "keyword": keyword,
                "weight": penalty,
            })

    # Hard rejects
    for keyword in HARD_REJECT_KEYWORDS:
        if normalize(keyword) in normalized:
            hard_reject_matches.append(keyword)

    hard_reject = (
        len(hard_reject_matches) > 0
        and len(positive_matches) == 0
    )

    notify_directly = (
        score >= 70
        and not hard_reject
    )

    needs_gemini = (
        not notify_directly
        and not hard_reject
    )

    return {
        "matched": len(positive_matches) > 0,

        "score": score,

        "categories": sorted(matched_categories),

        "positive_matches": positive_matches,

        "soft_negative_matches": soft_negative_matches,

        "hard_reject_matches": hard_reject_matches,

        "hard_reject": hard_reject,

        "notify_directly": notify_directly,

        "needs_gemini": needs_gemini,

        "normalized_text": normalized,
    }
