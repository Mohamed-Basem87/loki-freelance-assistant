# Full Stack Development category — tiered classifier vocabulary
#
# IMPORTANT: Full Stack MUST NEVER be selected by deterministic keyword
# classification. It can ONLY be produced by the existing LLM arbitration
# system. Therefore all keyword dictionaries are EMPTY.

# Positive evidence: intentionally empty — no deterministic selection.
POSITIVE_KEYWORDS = {}

# Negative evidence: intentionally empty — no deterministic rejection.
NEGATIVE_KEYWORDS = {}

# Hard rejects: intentionally empty — no deterministic hard rejection.
HARD_REJECT_KEYWORDS = set()

# Noise: intentionally empty.
NOISE_KEYWORDS = set()


# Runtime invariant: noise terms must never enter scored vocabulary.
def _all_scored_keywords():
    for polarity in (POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS):
        for category in polarity.values():
            for tier in ("core", "supporting"):
                for kw in category.get(tier, {}):
                    yield kw


_collision = NOISE_KEYWORDS.intersection(_all_scored_keywords())
if _collision:
    raise AssertionError(
        "The following NOISE_KEYWORDS were found inside a scored "
        "(core/supporting) dict — this reintroduces the false-positive/"
        "false-negative bug the tiered model was designed to prevent: "
        f"{sorted(_collision)}"
    )