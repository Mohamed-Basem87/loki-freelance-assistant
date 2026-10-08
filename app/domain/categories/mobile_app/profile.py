from app.domain.categories.profile import CategoryProfile

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
        "D-U-N-S, submitting an existing build, review follow-up, "
        "store configuration and submission without development. Also "
        "reject standalone non-mobile design, web, game, data analysis, "
        "ML, or other non-mobile tasks. "
        "UI/UX design-only engagements with no app development are "
        "graphic/UI work: reject. Monetized live voice-chat "
        "platforms (coins, gifting, wallets, payment from male users for "
        "private voice chat with women per LOKI_ACCEPTANCE_POLICY §10.6) "
        "are dating-adjacent social economies: reject; genuine voice-room "
        "communication apps without the gifting/wallet economy remain in "
        "scope."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.domain.categories.mobile_app.guard_prompt",
)
