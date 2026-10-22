from app.domain.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="data_analysis",
    name="Data Analysis",
    description="Data Analysis and Business Intelligence freelance work.",
    arbitration_context=(
        "Primary deliverables are data analysis/BI, dashboards/reports, KPI "
        "reporting, data cleaning/preparation, ETL for analytics, and "
        "business statistics. Also in scope: analysis-ready datasets "
        "(CSV/XLSX), document-text extraction, running the client's script "
        "and returning the export, and functional Excel/VBA tools. Reject "
        "when the primary deliverable is ML, predictive modeling, AI model "
        "development, general software development, manual data entry, "
        "transcription, or another non-analytical task. "
        "Reject outreach-list/lead-generation deliverables ('marketing "
        "leads', 'contact lists', 'company lists', prospecting for sales "
        "teams): those are marketing operations, not analysis. Reject "
        "HR/staffing roles (talent management, recruitment)."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.domain.categories.data_analysis.guard_prompt",
)
