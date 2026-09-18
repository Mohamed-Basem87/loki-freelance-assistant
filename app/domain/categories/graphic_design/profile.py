from app.domain.categories.profile import CategoryProfile

from .keywords import POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS, HARD_REJECT_KEYWORDS

PROFILE = CategoryProfile(
    id="graphic_design",
    name="Graphic Design",
    description="Graphic Design and visual communication freelance work.",
    arbitration_context=(
        "Primary deliverables are graphic-design assets and visual "
        "identity systems: logo design, brand identity and branding "
        "systems/guidelines, visual identity, marketing and advertising "
        "graphics, social media graphics, banners, posters, flyers, "
        "brochures, business cards and stationery, packaging and label "
        "design, print design, editorial/publication design, "
        "infographics, presentation/pitch-deck graphics, thumbnails, "
        "illustration, vector artwork, icons and graphic assets, "
        "typography-focused graphic design, photo manipulation and "
        "compositing, image retouching, and merchandise/apparel "
        "graphics. Reject when the primary deliverable is UI/UX or "
        "product design, website/web-application or frontend "
        "development, WordPress/Shopify builds, mobile apps, games, "
        "backend, data analysis, machine learning, video editing, "
        "motion graphics, 2D/3D animation, 3D modeling, CAD, "
        "architecture, interior design, photography services, "
        "social-media/marketing management, copywriting/content, SEO, "
        "data entry, virtual assistance, education, or printing/"
        "manufacturing services. Tools alone (Photoshop, Illustrator, "
        "Canva, Figma, CorelDRAW) never make a project graphic design; "
        "judge the primary deliverable."
    ),
    positive_keywords=POSITIVE_KEYWORDS,
    negative_keywords=NEGATIVE_KEYWORDS,
    hard_reject_keywords=HARD_REJECT_KEYWORDS,
    guard_prompt_module="app.domain.categories.graphic_design.guard_prompt",
)