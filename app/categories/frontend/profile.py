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
    id="frontend",
    name="Frontend Development",
    description="Frontend Development and UI Implementation freelance work.",
    arbitration_context=(
        "Primary deliverables are frontend development, UI implementation, "
        "responsive web design, and web UI work. Portfolio, media/"
        "rental-catalog, informational, and blog/news sites built in code "
        "are in scope, as are CMS builds (WordPress, Shopify, Wix, "
        "Webflow, Squarespace, Odoo) when the site is the deliverable. "
        "Custom WordPress/CMS plugin development is frontend/CMS work in "
        "scope even in PHP. Reject backend development, mobile apps, "
        "games, data analysis, ML, enterprise administration, or other "
        "non-frontend tasks."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.frontend.guard_prompt",
)
