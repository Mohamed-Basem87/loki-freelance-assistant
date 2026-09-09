from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="full_stack",
    name="Full Stack Development",
    description="Full Stack WEBSITE / WEB APPLICATION development spanning frontend, backend, database, and deployment.",
    arbitration_context=(
        "Primary deliverable is a complete new WEBSITE / WEB APPLICATION "
        "spanning two or more meaningful application layers (frontend + "
        "backend + database + deployment), OR recurring/ongoing BUILD work "
        "that develops and extends such an app across layers (new client/"
        "server features, database work, iterative releases). Pure "
        "integration, configuration, customization, or maintenance-only "
        "work (bug fixes, monitoring, dependency upkeep, no new build) is "
        "NOT full_stack. Mobile apps, mobile-first products, and desktop "
        "apps are NOT acceptable: when customer/partner/driver APPS are the "
        "primary deliverable (Android/iOS), select mobile even if a web "
        "admin panel or companion version exists; a native iOS/Android app "
        "with the same end-user features as its website is mobile-primary, "
        "not Full Stack. Companion/escort-rental platforms are NOT "
        "acceptable - REJECT. Hiring/employment posts for a full-stack web "
        "developer role are LEADS - select this category; reject hiring "
        "only for roles outside the full-stack web domain. Only select it "
        "when no specialist owns the deliverable; a viable specialist "
        "ALWAYS beats full_stack."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.full_stack.guard_prompt",
    arbitration_only=True,
    arbitration_role="primary",
    enabled=True,
)
