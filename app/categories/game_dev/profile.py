from dataclasses import dataclass

from .keywords import (
    POSITIVE_KEYWORDS,
    NEGATIVE_KEYWORDS,
    HARD_REJECT_KEYWORDS,
)


@dataclass(frozen=True)
class CategoryProfile:
    id: str
    name: str
    description: str
    arbitration_context: str
    positive_keywords: dict
    negative_keywords: dict
    hard_reject_keywords: set
    guard_prompt_module: str

    # Conservative thresholds for shadow mode — high SPW required.
    supporting_positive_min_for_gemini: int = 12
    supporting_negative_downgrade_threshold: int = 14
    min_supporting_positive_for_lone_core: int = 5
    title_positive_supporting_negative_threshold: int = 10


PROFILE = CategoryProfile(
    id="game_dev",
    name="Game Development",
    description="Game Development and Interactive Media freelance work.",
    arbitration_context=(
        "Primary deliverables are game development, game programming, "
        "game design, game prototypes, game assets, game mechanics, "
        "and interactive media. Reject when the primary deliverable is "
        "web development, mobile app development, data analysis, "
        "machine learning, or another non-game-related task."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.game_dev.guard_prompt",
)
