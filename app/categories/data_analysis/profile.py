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
    positive_keywords: dict
    negative_keywords: dict
    hard_reject_keywords: set
    llm_prompt_module: str
    guard_prompt_module: str

    # The tiered decision engine is shared. These values preserve the
    # current Data Analysis behavior while making thresholds part of
    # the category profile so another category can define its own.
    supporting_positive_min_for_gemini: int = 12
    supporting_negative_downgrade_threshold: int = 14
    min_supporting_positive_for_lone_core: int = 5
    title_positive_supporting_negative_threshold: int = 10

PROFILE = CategoryProfile(
    id="data_analysis",
    name="Data Analysis",
    description="Data Analysis and Business Intelligence freelance work.",
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    llm_prompt_module="app.categories.data_analysis.llm_prompt",
    guard_prompt_module="app.categories.data_analysis.guard_prompt",
)
