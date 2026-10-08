from app.domain.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="backend",
    name="Backend Development",
    description="Backend Development and Server-side freelance work.",
    arbitration_context=(
        "Primary deliverables are backend development, API design, "
        "database management, server-side logic, and infrastructure. Odoo "
        "backend development (custom modules, database/API work) is in "
        "scope; ERP administration without development is not. "
        "Make.com/n8n/Zapier automation, API-to-feed pipelines, "
        "automation-script builds (importers moving Excel rows into a web "
        "form, file/image deduplication tooling) are backend data work. "
        "Reject frontend, mobile, game, data analysis, ML, or other "
        "non-backend tasks. Self-promotion / service-advertising posts "
        "('looking for freelance projects', 'I specialize in', portfolio "
        "pitches) with no concrete client deliverable are not projects: "
        "reject. Consultation-only, action-plan-only, or high-level "
        "advisory posts with no code deliverable: reject (advisory-first "
        "engagements per LOKI_ACCEPTANCE_POLICY §10.17 are not backend "
        "builds). Bug-fix-only or CMS/plugin maintenance-and-configuration "
        "engagements (bug reports, small WordPress/plugin fixes, template "
        "tweaks) where the primary deliverable is not a build: reject. "
        "Low-code configuration without development (Zoho/Deluge, Power "
        "Platform, Airtable, Bubble config, ERP admin) is not backend "
        "engineering: reject."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.domain.categories.backend.guard_prompt",
)
