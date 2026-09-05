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
    id="mobile_app",
    name="Mobile App Development",
    description="Mobile App Development for iOS and Android platforms.",
    arbitration_context=(
        "Primary deliverables are mobile application development "
        "(native iOS/Android, Flutter, React Native). Releasing an app "
        "the freelancer also builds is in scope. Reject pure "
        "store-publishing/account administration of an already-built "
        "app: account opening, D-U-N-S, submitting an existing build, "
        "review follow-up. Also reject web, game, data analysis, ML, "
        "or other non-mobile tasks."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.mobile_app.guard_prompt",
)
