# Graphic Design category - tiered classifier vocabulary
#
# Core keywords are strong standalone evidence; supporting keywords require
# aggregation. NOISE_KEYWORDS are excluded from scoring entirely.
#
# Accuracy contract, derived from the full job corpus (loki_freelance_bot.db,
# 21,499 historical jobs):
#
# * A GD core hit in TITLE or BODY forces notify_directly (title) or a
#   core-positive path (body). A needless GD core hit on a job another
#   category already accepts adds a needs_gemini / second-direct candidate ->
#   an LLM arbitration call where none existed. So CORE is restricted to
#   phrases whose corpus occurrences were genuine graphic-design deliverables:
#     - 'graphic design'   title 31 / accepted 0 ; body 138 / accepted 5
#     - 'graphic designer' title 25 / accepted 0 ; 'ad design' 63/0
#     - 'logo design' 7/0 ; 'تصميم لوجو' 7/0 ; 'تصميم شعار' 4/0 ; 'مصمم جرافيك' 10/0
#     - 'هوية بصرية' title 16 / accepted 1 (portfolio-site + identity mix;
#       the single arbitration it triggers is genuinely correct behavior)
# * Generic terms are NOT core: they swarm accepted non-GD jobs
#   ('brand identity' 70 body / 16 accepted, 'branding' 651/272,
#   'typography' 533/266, 'banner' 257/99, 'poster' 162/28, 'logo' title
#   24/6). They are supporting with low weights so their sum stays under
#   the needs_gemini threshold (12) on out-of-scope jobs, while real design
#   clusters still escalate to arbitration.
# * In-scope deliverables follow the category llm_prompt/guard_prompt: logo,
#   brand identity, branding systems/guidelines, visual identity, marketing &
#   advertising graphics, social media graphics, banners/posters/flyers/
#   brochures, business cards/stationery, packaging & label design, print
#   design, editorial/publication, infographics, presentation/pitch-deck,
#   thumbnails, illustration, vector artwork, icons/assets, typography-focused
#   work, photo manipulation/retouching/compositing, apparel graphics.
# * Out-of-scope negatives (UI/UX, product design, web/frontend/mobile apps,
#   games, video/motion, 3D/CAD/interior, photography as primary service,
#   marketing/social management, copywriting/content, SEO, data entry/VA,
#   printing/manufacturing) are CORE NEGATIVES so they reject cleanly (R5)
#   when no GD core accompanies them, and genuine mixed deliverables go to
#   arbitration (R4 -> needs_gemini).
# * Tools are UNSCORED on both sides (Photoshop/Illustrator/Canva/CorelDRAW
#   per the prompt scope rule). Figma/فيجما are a supporting NEGATIVE only:
#   "a Figma project is not automatically Graphic Design".

# Positive evidence: Graphic Design deliverables.
POSITIVE_KEYWORDS = {

    # --- General graphic design / designer roles (EN + essential foreign) ---
    "graphic_design_role": {
        "core": {
            # NOTE: the bare noun phrase 'graphic design' is deliberately
            # SUPPORTING, not core: in the corpus it appears descriptively in
            # thousands of web/video/marketing postings ("WordPress developer
            # with graphic design skills"), so as core it forced ~59 main-LLM
            # arbitration calls on previously-rejected jobs with zero new
            # notifications. The compound ROLE 'graphic designer' IS core
            # ('Senior Graphic Designer', 'Graphic Designer') -- it is the
            # single highest-value title phrase and stayed cost-efficient.
            # 'graphic designers', 'graphic designing' and 'جرافيك ديزاينر'
            # are SUPPORTING: their corpus occurrences were inside mixed
            # web/video/social postings (0 direct notifications that rely on
            # them alone), so as core they only produced arbitration calls.
            "graphic designer": 8,
            "graphic design specialist": 8,
            "graphic artist": 6,
            # Arabic graphic-design roles (normalized: ة->ه).
            "مصمم جرافيك": 8,
            "مصممه جرافيك": 8,
            "مصممين جرافيك": 8,
            "مصمم جرافيك ديزاينر": 7,
            "غرافيك ديزاينر": 7,
        },
        "supporting": {
            "graphic designing": 7,
            "graphic designers": 8,
            "جرافيك ديزاينر": 7,
            "creative design": 3,
            "creative designer": 4,
            "visual designer": 4,
            "design studio": 3,
            # Generic noun phrase / typo variant / foreign equivalents: too
            # prone to incidental mentions in out-of-scope postings to be core
            # (all moved here after the LDK corpus cut confirmed ~0 notify
            # value for the main-LLM cost they generated as core).
            "graphic design": 7,
            "graphics designer": 5,
            "graphic design work": 5,
            "diseño gráfico": 5,
            "designer gráfico": 5,
            "desain grafis": 5,
            "desainer grafis": 5,
            "design gráfico": 5,
            "graphiste": 5,
            "design graphique": 5,
        },
    },

    # --- Logo / identity / branding systems ---
    "branding_identity": {
        "core": {
            "logo design": 8,
            "logo designing": 7,
            "logo designer": 8,
            "logo redesign": 7,
            "design a logo": 7,
            "design the logo": 7,
            "design logos": 6,
            "brand identity designer": 7,
            "brand design": 7,
            "brand kit design": 6,
            "stationery design": 7,
            "logo and branding": 6,
            # Arabic logo/identity (normalized: ة->ه, أإآ->ا). _keyword_forms
            # auto-adds Arabic clitic prefixes (ال/و/ف/ب/ل...) to multi-word
            # keywords whose first word is Arabic; list definite and plural
            # variants explicitly since plurals never match singulars.
            "تصميم شعار": 8,
            "تصميم لوجو": 8,
            "تصميم اللوجو": 8,
            "تصميم شعارات": 7,
            "تصميم لوجوهات": 6,
            "مصمم شعارات": 7,
            "مصمم لوجو": 7,
            "تصميم هوية": 8,
            "تصميم الهوية البصرية": 7,
            "هوية بصرية": 7,
            "تصميم هوية تجارية": 7,
            "هوية تجارية": 6,
            "مصممه هوية": 7,
        },
        "supporting": {
            # Demoted from core (simulation-verified zero notification loss):
            # these phrases only ever surfaced in mixed web/social/video
            # postings and carried waiting-cost, never a direct notification.
            "brand identity design": 8,
            "visual identity design": 7,
            "business card design": 7,
            "تصميم الشعار": 8,
            "تصميم الهوية": 7,
            "مصمم هوية": 7,
            "logo": 4,
            "logos": 4,
            "branding": 5,
            "brand identity": 4,
            "visual identity": 4,
            "brand book": 4,
            "brand board": 3,
            "logo variations": 4,
            "business card": 3,
            "business cards": 3,
            "stationery": 2,
            "letterhead": 4,
            "brand guidelines": 5,
            "brand guidelines design": 5,
            "branding design": 4,
            "هوية متكاملة": 5,
            "brand designer": 5,
        },
    },

    # --- Marketing & promotional graphics ---
    "marketing_assets": {
        "core": {
            "ad design": 7,
            "ads design": 7,
            "advertising design": 6,
            "advertisement design": 6,
            "social media design": 7,
            "social media designer": 7,
            "social media post design": 7,
            "social media posts design": 7,
            "instagram post design": 7,
            "instagram story design": 7,
            "facebook post design": 7,
            "banner design": 7,
            "banners design": 6,
            "flyer design": 8,
            "flyer designing": 7,
            "poster design": 8,
            "poster designing": 7,
            "brochure design": 7,
            "billboard design": 6,
            "menu design": 7,
            "carousel design": 7,
            "invitation design": 6,
            # Arabic marketing/print creatives.
            "تصميم اعلان": 7,
            "تصميم اعلانات": 7,
            "تصميم الاعلان": 6,
            "تصميم بانر": 7,
            "تصميم بنر": 7,
            "تصميم بانرات": 7,
            "تصميم بنرات": 7,
            "تصميم بوستر": 6,
            "تصميم بوسترات": 6,
            "تصميم منشورات": 7,
            "تصميم منشور": 7,
            "تصميم بوست": 7,
            "تصميم سوشيال ميديا": 7,
            "تصميم منيو": 7,
            "تصميم مينيو": 7,
        },
        "supporting": {
            # Demoted from core (simulation-verified zero notification loss).
            "marketing graphics": 6,
            "marketing materials design": 6,
            "تصميم الاعلانات": 6,
            "تصميم بوستات": 7,
            "banner": 3,
            "banners": 3,
            "poster": 3,
            "posters": 3,
            "flyer": 3,
            "flyers": 3,
            "brochure": 3,
            "brochures": 3,
            "pamphlet": 3,
            "e-flyer": 4,
            "post design": 5,
            "posts design": 5,
            "story design": 5,
            "بنرات": 4,
            "بوستات": 3,
            "منشورات": 3,
            "اعلانات": 2,
            "فلاير": 5,
            "مطويه": 4,
            # Ad-creative wording that in the corpus belongs to marketing
            # campaign management (mixed only -> LLM). Supporting now.
            "ad creatives": 4,
            "social media graphic": 5,
            "social media graphics": 5,
        },
    },

    # --- Digital assets: thumbnails / infographics / presentations / icons ---
    "digital_assets": {
        "core": {
            "infographic design": 8,
            "infographics design": 7,
            "youtube thumbnail": 6,
            "video thumbnail": 6,
            "pitch deck design": 6,
            "icon design": 7,
            "vector art": 7,
            "vector graphics": 6,
            "graphic asset": 6,
            # Arabic digital assets.
            "تصميم انفوجرافيك": 7,
            "تصميم برزنتيشن": 7,
            "تصميم برزنتيشنز": 6,
            "تصميم عرض تقديمي": 7,
            "تصميم ايقونات": 6,
            "تصميم استيكر": 7,
        },
        "supporting": {
            # Demoted from core (simulation-verified zero notification loss).
            "presentation design": 7,
            "icons design": 6,
            "vector artwork": 7,
            "thumbnail": 5,
            "thumbnails": 5,
            "thumbnail design": 5,
            "thumbnails design": 5,
            "infographic": 4,
            "infographics": 4,
            "presentation": 3,
            "pitch deck": 4,
            "slides": 2,
            "icon": 2,
            "icons": 2,
            "vector": 2,
            "graphic assets": 3,
            "visual assets": 3,
            "استيكر": 3,
            "انفوجرافيك": 4,
            "برزنتيشن": 4,
            "عرض تقديمي": 4,
        },
    },

    # --- Print / editorial / publication / packaging (in scope) ---
    "print_editorial": {
        "core": {
            "print designer": 7,
            "labels design": 6,
            "magazine design": 6,
            "book cover design": 7,
            "publication design": 6,
            "book design": 6,
            "menu card design": 6,
            "signage design": 6,
            # Arabic print/packaging.
            "تصميم مطبوعات": 7,
            "تصميم تغليف": 7,
            "تصميم غلاف": 7,
            "غلاف كتاب": 6,
            "تصميم كتاب": 6,
            "تصميم مجله": 5,
            "تصميم كروت": 6,
            "تصميم كرت": 6,
        },
        "supporting": {
            # Demoted from core (simulation-verified zero notification loss):
            # zero direct notifications depended on them alone; their hits
            # were mixed print/web/social postings that only cost arbitration.
            "print design": 7,
            "packaging design": 8,
            "packaging designer": 7,
            "label design": 7,
            "editorial design": 7,
            "packaging": 2,
            "print": 2,
            "editorial": 3,
            "magazine": 3,
            "book cover": 4,
            "postcard": 3,
            "signage": 4,
            "مطبوعات": 3,
            "تغليف": 2,
            "غلاف": 2,
            "كروت": 3,
        },
    },

    # --- Illustration / vector / apparel + photo production work ---
    "illustration_photo": {
        "core": {
            "photo manipulation": 8,
            "photo retouching": 7,
            "image retouching": 7,
            "photo compositing": 7,
            "t-shirt design": 7,
            "tshirt design": 7,
            "tee design": 6,
            "cartoon drawing": 6,
            # Arabic illustration / merch.
            "تصميم تيشيرت": 7,
            "تصميم قميص": 5,
            "رسم كاريكاتير": 6,
        },
        "supporting": {
            # Demoted from core (simulation-verified zero notification loss).
            "apparel design": 6,
            "illustration": 4,
            "illustrations": 4,
            "typography": 4,
            "retouching": 5,
            "photo editing": 4,
            "photoshop editing": 4,
            "t-shirt": 4,
            "tshirt": 4,
            "apparel": 3,
            "merch": 3,
            "merchandise": 2,
            "caricature": 4,
            "كاريكاتير": 4,
            "تيشيرت": 4,
        },
    },

    # Arabic book-layout / calligraphy-vector / bot-visual builds
    # (2026-09-24 run 1 gate review). Three Arabic deterministic FNs had
    # ZERO graphic_design vocabulary: 4732 (تنسيق كتاب مع الصور -- book
    # layout/typesetting with images), 5070 (خط قصيدة بخط الثلث بصيغة
    # فيكتور -- Thuluth calligraphy poem vector), 4447 (تحديث تصاميم بوت
    # Discord وموقعه -- visual refresh of a Discord bot + its site).
    # All rejected insufficient_signal with no LLM arbitration.
    # Collocations only; lone-core hits route to needs_gemini.
    "arabic_book_vector_bot": {
        "core": {
            "تنسيق كتاب": 7,
            "تخطيط كتاب": 7,
            "تحرير كتاب": 6,
            "خط الثلث": 8,
            "خط عربي": 7,
            "خطاط": 7,
            "صيغة فيكتور": 7,
            "فيكتور": 6,
            "تصميم بوت": 6,
            "تصاميم بوت": 6,
            "بوت ديسكورد": 6,
            "بوت ديسكورد وموقعه": 6,
        },
        "supporting": {
            "كتاب": 2,
            "انيق": 3,
            "خط": 2,
            "بوت": 2,
        },
    },

    # --- General Arabic/creative design vocabulary (weak, aggregation only) ---
    "arabic_general": {
        "supporting": {
            "الجرافيك": 3,
            "جرافيك": 4,
            "غرافيك": 4,
            "تصميم": 2,
            "بنر": 3,
            "لوجو": 4,
            "شعار": 3,
            "الشعار": 3,
            "مصمم": 3,
            "مصممه": 3,
            "مصممين": 3,
            "جرافيك ديزاين": 7,
            "غرافيك ديزاين": 7,
            "ديزاين": 3,
            "تصميمات": 3,
        },
    },
}

# Negative evidence: out-of-scope deliverables that must suppress GD.
# Core-negative weight is unused by the decision table (presence only),
# but mirrors the repo convention of a real weight per keyword.
NEGATIVE_KEYWORDS = {

    # --- Web / frontend / CMS development ---
    "web_development": {
        "core": {
            "web design": 6,
            "web designer": 6,
            "web designing": 6,
            "website design": 6,
            "website designer": 6,
            "website": 7,
            "web page": 6,
            "web pages": 6,
            "sitio web": 7,
            "pagina web": 6,
            "página web": 6,
            "web development": 7,
            "web developer": 7,
            "website development": 7,
            "web application": 6,
            "web app": 6,
            "wordpress": 8,
            "shopify": 8,
            "woocommerce": 7,
            "wix": 6,
            "webflow": 6,
            "squarespace": 6,
            "elementor": 6,
            "framer": 6,
            "landing page": 7,
            "landing pages": 7,
            "frontend": 7,
            "front-end": 7,
            "front end": 7,
            "تصميم موقع": 6,
            "تصميم مواقع": 6,
            "تصميم متجر": 6,
            "تصميم متاجر": 6,
            "تطوير موقع": 6,
            "تطوير مواقع": 6,
            "مطور موقع": 6,
            "مطور مواقع": 6,
            "مصمم مواقع": 6,
            "موقع الكتروني": 6,
            "موقع ويب": 6,
            "متجر الكتروني": 6,
            "متاجر الكترونيه": 6,
            "تصميم صفحات": 6,
        },
        "supporting": {},
    },

    # --- UI/UX, product design, digital-product interfaces ---
    "ui_ux_product": {
        "core": {
            "ui design": 7,
            "ux design": 7,
            "ui/ux": 7,
            "ui ux": 7,
            "ui designer": 7,
            "ux designer": 7,
            "ui/ux designer": 7,
            "user interface": 6,
            "user experience": 6,
            "wireframe": 6,
            "wireframes": 6,
            "prototype": 6,
            "prototypes": 6,
            "product design": 7,
            "product designer": 7,
            "design system": 6,
            "design systems": 6,
            "user flow": 6,
            "user flows": 6,
            "mobile app": 7,
            "mobile application": 7,
            "android app": 7,
            "ios app": 7,
            "app interface": 6,
            "dashboard interface": 6,
            "تصميم واجهات": 7,
            "تصميم واجهه": 7,
            "تصميم الواجهات": 6,
            "مصمم واجهات": 7,
            "مصمم واجهه": 7,
            "واجهه المستخدم": 6,
            "تصميم تطبيق": 6,
            "تصميم تطبيقات": 6,
        },
        "supporting": {
            "figma": 8,
            "فيجما": 8,
            "figma design": 8,
        },
    },

    # --- Video / motion / animation ---
    "video_motion": {
        "core": {
            "video editing": 8,
            "video editor": 8,
            "video production": 7,
            "motion graphics": 8,
            "motion design": 7,
            "motion designer": 7,
            "motion graphic designer": 8,
            "motion graphics designer": 8,
            "motion graphic design": 7,
            "motion graphics design": 7,
            "animation": 7,
            "animated": 6,
            "2d animation": 7,
            "3d animation": 7,
            "explainer video": 7,
            "reel": 6,
            "reels": 6,
            "tiktok editing": 7,
            "video compositing": 7,
            "youtube video": 6,
            "مونتاج": 8,
            "مونتاج فيديو": 8,
            "مونتير": 7,
            "موشن جرافيك": 8,
            "انيميشن": 7,
            "ريلز": 6,
            "تيك توك": 7,
            "محرر فيديو": 7,
            "مصمم فيديو": 6,
        },
        "supporting": {
            "فيديو": 5,
            "فيديوهات": 5,
        },
    },

    # --- 3D / CAD / architecture / interior ---
    "three_d_cad_interior": {
        "core": {
            "3d model": 7,
            "3d modeling": 7,
            "3d modelling": 7,
            "3d design": 7,
            "3d artist": 7,
            "blender": 7,
            "maya": 7,
            "3ds max": 7,
            "zbrush": 7,
            "cinema 4d": 6,
            "autocad": 7,
            "solidworks": 7,
            "cad": 7,
            "architectural": 6,
            "architecture": 6,
            "interior design": 7,
            "interior designer": 7,
            "interior decorator": 7,
            "ديكور": 7,
            "ديكورات": 7,
            "تصميم ديكور": 7,
            "تصميم داخلي": 7,
            "مصمم داخلي": 7,
            "تصميم معماري": 6,
        },
        "supporting": {},
    },

    # --- Photography as the primary service ---
    "photography": {
        "core": {
            "photography": 7,
            "photographer": 7,
            "photo shoot": 7,
            "photo shoots": 7,
            "product photography": 8,
            "event photography": 8,
            "portrait photography": 8,
            "real estate photography": 8,
            "wedding photography": 7,
            "تصوير": 7,
            "تصوير فوتوغرافي": 7,
            "مصور": 7,
            "مصورين": 7,
            "مصوره": 7,
            "جلسه تصوير": 7,
            "فوتوسيشن": 7,
        },
        "supporting": {},
    },

    # --- Marketing / socials / content management (not graphic assets) ---
    "marketing_management": {
        "core": {
            "social media management": 8,
            "social media manager": 8,
            "social media marketing": 7,
            "digital marketing": 6,
            "marketing manager": 7,
            "marketing management": 7,
            "ad management": 6,
            "ads management": 6,
            "google ads management": 6,
            "campaign management": 6,
            "content marketing": 6,
            "follower": 5,
            "followers": 5,
            "follower growth": 6,
            "instagram growth": 6,
            "account management": 6,
            "influencer": 5,
            "ادارة سوشيال": 8,
            "ادارة سوشيال ميديا": 8,
            "ادارة مواقع التواصل": 8,
            "ادارة منصات": 6,
            "مشرف سوشيال ميديا": 8,
            "مشرف صفحات": 7,
            "تسويق": 5,
            "مسوق": 5,
        },
        "supporting": {
            "moderation": 4,
            "posting": 4,
            "content planning": 4,
        },
    },

    # --- Copywriting / content / SEO / VA / data tasks ---
    "copy_content_admin": {
        "core": {
            "copywriting": 8,
            "copywriting services": 8,
            "content writing": 7,
            "content writer": 7,
            "article writing": 7,
            "blog posts": 6,
            "scriptwriting": 6,
            "seo": 7,
            "search engine optimization": 7,
            "data entry": 7,
            "virtual assistant": 7,
            "virtual assistance": 7,
            "transcription": 6,
            "data annotation": 6,
            "كتابه محتوى": 7,
            "كتابه محتوي": 7,
            "كاتب محتوى": 7,
            "ادخال بيانات": 7,
        },
        "supporting": {
            "backlinks": 4,
            "link building": 4,
        },
    },

    # --- Printing / manufacturing as the primary service ---
    "printing_manufacturing": {
        "core": {
            "printing services": 7,
            "printing press": 7,
            "print shop": 7,
            "manufacturing": 6,
            "factory": 6,
        },
        "supporting": {
            "printing": 8,
            "manufacture": 8,
        },
    },
}

HARD_REJECT_KEYWORDS = {
    # Shared policy blocks, mirrored from the other categories so blocked
    # content never burns a GD keyword match or an arbitration call.
    "gambling", "casino", "igaming", "jackpot", "poker", "roulette",
    "blackjack", "satta matka", "spin and win", "spin win", "lucky jet",
    "sportsbook", "sports betting", "slot machine",
    "dating app", "dating apps", "online dating", "dating site",
    "dating website", "dating platform", "dating service",
    "matchmaking app", "matchmaking platform",
    "موقع تعارف", "تطبيق تعارف",
    "lottery", "lotto",
    "porn", "paysite", "paysites", "beeg", "nsfw",
    "adult site", "adult website",
    "escort service", "escort services",
    "rental boyfriend", "boyfriend rental", "rent-a-boyfriend",
    "rental companion", "companion rental",
    "betting", "wager", "wagering",
    "1xbet", "dragon tiger", "dragon vs tiger",
    "quotex", "iq option", "binary options", "olymptrade", "pocket option",
    "commission only", "عمولة فقط",
    "volunteer", "unpaid", "متطوع", "بدون مقابل",
    "backlink", "backlinks", "link building",
    "letter writing", "request letter",
    "خدمات smm",
    # Graphic-design-specific blockers.
    "free logo",
    "logo contest",
    "design contest",
    # Arbitration-none sweep (2026-09 run-c audit): deterministic rejects for
    # the 53 single-arb none jobs. Validated via clear_keyword_profile_cache
    # replay over DB-accepted + window-accepted + 5 flip-risk rows: 0 flips,
    # no change to flip-risk 3810/3912/3969/4184/4387 (still arbitration).
    "course development", "powerpoint course", "نشر كتابي",
    "دور النشر", "صانع محتوى",
}

# Noise: intentionally unscored terms that are too ambiguous to classify.
NOISE_KEYWORDS = {
    "design", "designs", "designed", "designing",
    "designer", "designers",
    "graphic", "graphics",
    "art", "arts", "artist",
    "creative", "creativity",
    "visual", "visuals",
    "brand",
}

# Runtime invariant: noise terms must never enter scored vocabulary.
def _all_scored_keywords():
    for polarity in (POSITIVE_KEYWORDS, NEGATIVE_KEYWORDS):
        for category in polarity.values():
            for tier in ("core", "supporting"):
                for kw in category.get(tier, {}):
                    yield kw


_collision = NOISE_KEYWORDS.intersection(_all_scored_keywords())
if _collision:
    raise AssertionError(
        "The following NOISE_KEYWORDS were found inside a scored "
        "(core/supporting) dict — this reintroduces the false-positive/"
        "false-negative bug the tiered model was designed to prevent: "
        f"{sorted(_collision)}"
    )