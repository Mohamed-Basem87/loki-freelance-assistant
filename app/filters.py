import re

from app.keywords import (
    HARD_REJECT_KEYWORDS,
    NEGATIVE_KEYWORDS,
    POSITIVE_KEYWORDS,
)
from app.normalize import normalize


# ------------------------------------------------------------------
# Tunable thresholds
#
# These are the ONLY numbers in the whole decision path. Everything
# else is boolean presence/absence of core signals. Keep them here,
# not scattered through the logic, so tuning is a one-line change.
# ------------------------------------------------------------------

# Minimum total supporting-positive weight required to send a job to
# Gemini when NO core signal (positive or negative) was found at all.
# Below this, the job is rejected outright as "not enough signal to
# bother a human/LLM with".
SUPPORTING_POSITIVE_MIN_FOR_GEMINI = 12

# If a core-positive keyword is present with no core-negative keyword,
# but the supporting-negative evidence is unusually heavy (i.e. the
# posting also reads a lot like a full software project), downgrade
# from an automatic notification to a Gemini review instead of trusting
# the core-positive signal blindly.
SUPPORTING_NEGATIVE_DOWNGRADE_THRESHOLD = 14

# If exactly ONE core-positive keyword fired (no second core hit to
# corroborate it) and there is no supporting-positive evidence backing
# it up either, treat it as too thin to auto-notify on. Two or more
# core-positive hits are exempt from this check — they already
# corroborate each other, which is a stronger position than one core
# hit plus some supporting words.
MIN_SUPPORTING_POSITIVE_FOR_LONE_CORE = 4


# ------------------------------------------------------------------
# Keyword matching helpers
# ------------------------------------------------------------------

def _contains_keyword(text: str, keyword: str) -> bool:
    """
    All keywords use word-boundary matching to avoid false positives
    like 'bot' matching 'robotics', or the Arabic word 'شيت' matching
    inside an unrelated longer word like 'شيتات'.

    Python's `re` module is Unicode-aware for `str` patterns, so `\\b`
    (which is defined in terms of `\\w`) works correctly for Arabic
    too -- verified directly: `\\bشيت\\b` matches a standalone "شيت"
    but not the "شيت" inside "شيتات". Note this does mean a keyword
    won't match when a common Arabic prefix (ل/ب/و/ك/ال) is attached
    directly with no space (e.g. "لإكسل"); that's a distinct,
    acceptable tradeoff given the keyword lists already hand-curate
    common spelling/attachment variants as separate entries.
    """
    pattern = r"\b" + re.escape(keyword) + r"\b"
    return re.search(pattern, text) is not None


def _mask_keyword(text: str, keyword: str) -> str:
    """
    Replace matched keyword with spaces so shorter overlapping
    keywords cannot match afterwards. Keeps string length unchanged.
    """
    pattern = r"\b" + re.escape(keyword) + r"\b"
    return re.sub(pattern, lambda m: " " * len(m.group(0)), text)


def _flatten(keyword_dict: dict, tier: str):
    """
    Flatten {category: {"core": {...}, "supporting": {...}}} into a
    single list of match records for one tier, sorted longest-first so
    longer phrases claim their text before shorter substrings can match.
    """
    items = []
    for category, tiers in keyword_dict.items():
        for keyword, weight in tiers.get(tier, {}).items():
            items.append({
                "keyword": keyword,
                "normalized_keyword": normalize(keyword),
                "weight": weight,
                "category": category,
                "tier": tier,
            })
    return sorted(items, key=lambda x: len(x["normalized_keyword"]), reverse=True)


# Built once at import time.
_POSITIVE_CORE = _flatten(POSITIVE_KEYWORDS, "core")
_POSITIVE_SUPPORTING = _flatten(POSITIVE_KEYWORDS, "supporting")
_NEGATIVE_CORE = _flatten(NEGATIVE_KEYWORDS, "core")
_NEGATIVE_SUPPORTING = _flatten(NEGATIVE_KEYWORDS, "supporting")

_HARD_REJECT = sorted(
    ({"keyword": kw, "normalized_keyword": normalize(kw)} for kw in HARD_REJECT_KEYWORDS),
    key=lambda x: len(x["normalized_keyword"]),
    reverse=True,
)


def _match_tier(text: str, tier_items: list) -> list:
    """
    Match every keyword in tier_items against text. Matches within the
    SAME tier mask each other (longest phrase wins) so e.g. 'excel
    sheet' claims its text before bare 'sheet' can also match. Matching
    is independent across tiers/polarities by design — a supporting
    positive word and a core negative word are different concerns and
    should both be visible to the decision table.
    """
    remaining = text
    hits = []
    for item in tier_items:
        keyword = item["normalized_keyword"]
        if _contains_keyword(remaining, keyword):
            hits.append(item)
            remaining = _mask_keyword(remaining, keyword)
    return hits


def _has_core_hit(text: str, tier_items: list) -> bool:
    """Cheap existence check used for title analysis (no masking needed)."""
    for item in tier_items:
        if _contains_keyword(text, item["normalized_keyword"]):
            return True
    return False


# ------------------------------------------------------------------
# Main entry point
# ------------------------------------------------------------------

def keyword_filter(text: str, title: str = ""):
    """
    Classify a job posting using a tiered decision table instead of an
    additive score. See module docstring in keywords.py for the tier
    model (core / supporting / noise).

    `title` is optional but recommended: a core keyword found in the
    job title is treated as the single strongest available signal,
    since the client wrote the title specifically to say what the job
    IS.

    Returns a dict with the full evidence trail plus a routing
    decision and a plain-English `reason` explaining exactly which
    rule fired.
    """
    normalized_text = normalize(text)
    normalized_title = normalize(title) if title else ""

    # --- Hard reject keywords (checked against full text) -----------
    hard_reject_matches = [
        item["keyword"] for item in _HARD_REJECT
        if _contains_keyword(normalized_text, item["normalized_keyword"])
    ]

    # --- Body-level matches, tier by tier ----------------------------
    positive_core_hits = _match_tier(normalized_text, _POSITIVE_CORE)
    positive_supporting_hits = _match_tier(normalized_text, _POSITIVE_SUPPORTING)
    negative_core_hits = _match_tier(normalized_text, _NEGATIVE_CORE)
    negative_supporting_hits = _match_tier(normalized_text, _NEGATIVE_SUPPORTING)

    positive_matches = positive_core_hits + positive_supporting_hits
    negative_matches = negative_core_hits + negative_supporting_hits

    matched_categories = sorted({h["category"] for h in positive_matches})
    matched_negative_categories = sorted({h["category"] for h in negative_matches})

    supporting_positive_weight = sum(h["weight"] for h in positive_supporting_hits)
    supporting_negative_weight = sum(h["weight"] for h in negative_supporting_hits)

    has_core_positive = bool(positive_core_hits)
    has_core_negative = bool(negative_core_hits)

    matched = has_core_positive or bool(positive_supporting_hits)

    # Hard reject is an independent safety net (unpaid/internship/
    # translation/etc.) and must win regardless of whether the
    # posting also happens to mention a positive keyword -- e.g. an
    # "unpaid internship, Excel/Power BI a plus" posting should still
    # be rejected. Previously this was gated on `not matched`, which
    # let any positive hit silently disable the reject entirely.
    hard_reject = bool(hard_reject_matches)

    # --- Title-level signal (authoritative when present) -------------
    title_core_positive = bool(normalized_title) and _has_core_hit(normalized_title, _POSITIVE_CORE)
    title_core_negative = bool(normalized_title) and _has_core_hit(normalized_title, _NEGATIVE_CORE)

    # ------------------------------------------------------------------
    # Decision table. Rules are evaluated top-to-bottom; first match wins.
    # This replaces score-threshold + dominance-ratio arithmetic with an
    # explicit, explainable set of rules.
    # ------------------------------------------------------------------

    core_positive_hit_count = len(positive_core_hits)

    decision = None
    reason = None

    if hard_reject:
        decision = "reject"
        reason = "hard_reject_keyword"

    elif title_core_positive and not title_core_negative:
        # Title is the strongest available signal, but it must not
        # blind-override a genuine core-negative signal in the body —
        # that's a real contradiction (e.g. title says "Power BI
        # Analyst", body describes a full-stack/React build), not
        # something to auto-resolve in either direction.
        if has_core_negative:
            decision = "needs_gemini"
            reason = "title_positive_but_body_core_negative"
        else:
            decision = "notify_directly"
            reason = "title_core_positive"

    elif title_core_negative and not title_core_positive and not has_core_positive:
        decision = "reject"
        reason = "title_core_negative_no_body_positive"

    elif has_core_positive and has_core_negative:
        decision = "needs_gemini"
        reason = "mixed_core_signals"

    elif has_core_negative and not has_core_positive:
        decision = "reject"
        reason = "core_negative_no_core_positive"

    elif has_core_positive and not has_core_negative:
        if (
            core_positive_hit_count == 1
            and supporting_positive_weight < MIN_SUPPORTING_POSITIVE_FOR_LONE_CORE
        ):
            # A single core-positive keyword with no corroborating
            # supporting evidence is too thin to trust blindly — could
            # be an incidental mention (e.g. a lone "sql" in an
            # otherwise unrelated posting).
            decision = "needs_gemini"
            reason = "lone_core_positive_insufficient_support"
        elif supporting_negative_weight >= SUPPORTING_NEGATIVE_DOWNGRADE_THRESHOLD:
            decision = "needs_gemini"
            reason = "core_positive_but_heavy_supporting_negative"
        else:
            decision = "notify_directly"
            reason = "core_positive_clean"

    else:
        # No core signal in either direction (and title, if present,
        # was inconclusive or absent).
        if supporting_positive_weight >= SUPPORTING_POSITIVE_MIN_FOR_GEMINI:
            decision = "needs_gemini"
            reason = "supporting_positive_only"
        else:
            decision = "reject"
            reason = "insufficient_signal"

    notify_directly = decision == "notify_directly"
    needs_gemini = decision == "needs_gemini"

    def _fmt(hits):
        return [
            {"keyword": h["keyword"], "weight": h["weight"], "category": h["category"]}
            for h in hits
        ]

    return {
        "matched": matched,
        "decision": decision,
        "reason": reason,

        "categories": matched_categories,
        "negative_categories": matched_negative_categories,

        "has_core_positive": has_core_positive,
        "has_core_negative": has_core_negative,
        "core_positive_hit_count": core_positive_hit_count,
        "supporting_positive_weight": supporting_positive_weight,
        "supporting_negative_weight": supporting_negative_weight,

        "title_core_positive": title_core_positive,
        "title_core_negative": title_core_negative,

        "positive_core_matches": _fmt(positive_core_hits),
        "positive_supporting_matches": _fmt(positive_supporting_hits),
        "negative_core_matches": _fmt(negative_core_hits),
        "negative_supporting_matches": _fmt(negative_supporting_hits),

        "hard_reject_matches": hard_reject_matches,
        "hard_reject": hard_reject,

        "notify_directly": notify_directly,
        "needs_gemini": needs_gemini,

        "normalized_text": normalized_text,
    }
