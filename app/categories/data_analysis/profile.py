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

    # The tiered decision engine is shared. These values preserve the
    # current Data Analysis behavior while making thresholds part of the
    # category profile so another category can define its own.
    supporting_positive_min_for_gemini: int = 12
    supporting_negative_downgrade_threshold: int = 14
    min_supporting_positive_for_lone_core: int = 5
    title_positive_supporting_negative_threshold: int = 10


PROFILE = CategoryProfile(
    id="data_analysis",
    name="Data Analysis",
    description="Data Analysis and Business Intelligence freelance work.",
    arbitration_context=(
        "Primary deliverables are data analysis, business intelligence, "
        "analytical dashboards/reports, KPI reporting, data cleaning and "
        "preparation, data transformation/ETL for analytics, and "
        "business-focused statistical analysis. Reject when the primary "
        "deliverable is machine learning, predictive modeling, AI model "
        "development, general software development, manual data entry, "
        "transcription, or another non-analytical task."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.data_analysis.guard_prompt",
)
