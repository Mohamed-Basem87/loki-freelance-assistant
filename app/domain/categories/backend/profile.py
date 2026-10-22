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
        "Automation and API-to-feed pipelines, "
        "automation-script builds (importers moving Excel rows into a web "
        "form, file/image deduplication tooling) are backend data work. "
        "Reject frontend, mobile, game, data analysis, ML, or other "
        "non-backend tasks. Self-promotion / service-advertising posts "
        "('I specialize in', portfolio pitches) with no concrete deliverable "
        "are not projects: reject. Consultation-only, action-plan-only, or "
        "advisory posts with no code deliverable: reject (advisory-first "
        "engagements per LOKI_ACCEPTANCE_POLICY §10.17 are not builds). "
        "Bug-fix-only or CMS/plugin maintenance engagements (bug reports, "
        "small WordPress/plugin fixes, template tweaks) with no build "
        "deliverable: reject. "
        "Low-code/no-code application builds (Newgen, OutSystems, Mendix, "
        "Appian, Power Apps, Zoho Creator, Airtable, Bubble, Make/n8n/Zapier) "
        "are in scope when the deliverable is a working application "
        "(workflows, screens, roles, integrations, deployment); "
        "platform-enablement alone is not backend work. "
        "Bulk catalogue/data-entry upload into an existing store (populating "
        "SKUs, pricing, stock, images) is data entry, not a build: reject; "
        "building the importer itself is in scope."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.domain.categories.backend.guard_prompt",
)
