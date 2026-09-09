from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="ai_ml",
    name="AI/ML Data Science",
    description="AI, Machine Learning, and Data Science freelance work.",
    arbitration_context=(
        "Primary deliverables are machine learning model development, "
        "deep learning, NLP, computer vision, data science projects, "
        "and AI/ML systems. Reject when the primary deliverable is "
        "data analysis, business intelligence, web development, "
        "mobile app development, or another non-AI/ML-related task."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.ai_ml.guard_prompt",
)
