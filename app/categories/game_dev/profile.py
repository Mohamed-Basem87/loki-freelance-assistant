from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

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
