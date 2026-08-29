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
    id="backend",
    name="Backend Development",
    description="Backend Development and Server-side freelance work.",
    arbitration_context=(
        "Primary deliverables are backend development, API design and "
        "implementation, database management, server-side logic, and "
        "infrastructure. Backend development on enterprise systems such as "
        "Odoo (custom modules, server-side logic, database/API work) is in "
        "scope; ERP administration or configuration without development is "
        "not. Reject when the primary deliverable is frontend "
        "development, mobile app development, game development, data "
        "analysis, machine learning, or another non-backend-related task."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.backend.guard_prompt",
)
