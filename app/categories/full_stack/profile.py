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

    arbitration_only: bool = False
    enabled: bool = True

    # Conservative thresholds for shadow mode — high SPW required.
    supporting_positive_min_for_gemini: int = 12
    supporting_negative_downgrade_threshold: int = 14
    min_supporting_positive_for_lone_core: int = 5
    title_positive_supporting_negative_threshold: int = 10


PROFILE = CategoryProfile(
    id="full_stack",
    name="Full Stack Development",
    description="Full Stack WEBSITE / WEB APPLICATION development spanning frontend, backend, database, and deployment.",
    arbitration_context=(
        "Primary deliverable is a complete new WEBSITE or WEB APPLICATION that "
        "genuinely spans two or more meaningful application layers (e.g., "
        "frontend + backend + database + deployment). The client is asking to "
        "BUILD a new website/web application, not integrate, configure, "
        "customize, maintain, or fix an existing one. Mobile apps, mobile-first "
        "products, and desktop applications are NOT acceptable deliverables: "
        "when the client asks for customer/partner/driver APPS as the primary "
        "deliverable (Android/iOS), select the mobile category instead, even "
        "if the project also includes a web admin panel or companion web "
        "version. A multi-stage product that explicitly requires a native "
        "iOS/Android app with the same end-user features as its website is "
        "mobile-primary, not Full Stack, even when the website is also a "
        "first-class deliverable. Gambling, casino, sports betting, igaming, and real-money "
        "gaming platforms (including crypto gambling, sportsbooks, slot "
        "machines, poker, roulette, blackjack) are NOT acceptable deliverables "
        "and must be REJECTED. Hiring/employment posts whose advertised "
        "role is a full-stack web developer/maintainer are LEADS and "
        "select this category; only reject hiring posts for roles outside "
        "the full-stack website/web application domain. No existing "
        "specialist category clearly owns the primary deliverable. A viable "
        "specialist category ALWAYS beats full_stack."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.categories.full_stack.guard_prompt",
    arbitration_only=True,
    enabled=True,
)
