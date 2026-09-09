from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

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
