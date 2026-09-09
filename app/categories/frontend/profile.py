from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

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
        "Custom WordPress/CMS plugin development and plugin-extended "
        "platform builds (booking engines like Easy Appointments: "
        "business layer, hooks/APIs) are frontend/CMS web work even in "
        "PHP. Reject backend, mobile apps, games, data analysis, ML, "
        "enterprise administration, or other non-frontend tasks."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.frontend.guard_prompt",
)
