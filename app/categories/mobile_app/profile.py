from app.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="mobile_app",
    name="Mobile App Development",
    description="Mobile App Development for iOS and Android platforms.",
    arbitration_context=(
        "Primary deliverables are mobile application development "
        "(native iOS/Android, Flutter, React Native). Design phases "
        "(first-time-user onboarding, interactive prototype screens) of "
        "an app build are in scope. Releasing an app the freelancer also "
        "builds is in scope. Reject pure store-publishing/account "
        "administration of an already-built app: account opening, "
        "D-U-N-S, submitting an existing build, review follow-up. Also "
        "reject standalone non-mobile design, web, game, data analysis, "
        "ML, or other non-mobile tasks."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.mobile_app.guard_prompt",
)
