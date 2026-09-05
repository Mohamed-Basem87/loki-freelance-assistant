# Backend Development category — tiered classifier vocabulary
#
# Core keywords are strong standalone evidence; supporting keywords require
# aggregation. NOISE_KEYWORDS are excluded from scoring entirely.

# Positive evidence: Backend Development / APIs / Databases / Server-side.
POSITIVE_KEYWORDS = {

    "frameworks": {
        "core": {
            "laravel": 9,
            "php": 7,
            "django": 8,
            "flask": 7,
            "fastapi": 8,
            "spring boot": 9,
            "spring": 8,
            "asp.net": 8,
            "nodejs": 8,
            "node.js": 8,
            "nestjs": 8,
            "ruby on rails": 8,
            "ruby": 6,
            "golang": 6,
            "rust": 6,
            "java": 6,
        },
        "supporting": {
            "express": 3,
            "go": 3,
            "c#": 3,
            ".net": 4,
            "python": 3,
            "microservice": 4,
            "microservices": 4,
            "serverless": 4,
            "lambda": 4,
            "cloud function": 4,
        },
    },

    "databases": {
        "core": {
            "postgresql": 8,
            "postgres": 8,
            "mysql": 8,
            "mongodb": 8,
            "redis": 7,
            "elasticsearch": 7,
            "sqlite": 6,
            "oracle": 7,
            "sql server": 7,
            "dynamodb": 7,
            "cassandra": 7,
            "neo4j": 7,
            "firebase": 6,
            "supabase": 6,
            # Database-engineering collocation (2026-08-24 audit): 13
            # corpus hits, predominantly genuine DB builds (445, 4029, 6612
            # accepted; 8148 trucking web-database missed).
            "database development": 7,
        },
        "supporting": {
            "database design": 4,
            "database schema": 4,
            "migration": 3,
            "seed": 3,
            "orm": 3,
            "prisma": 4,
            "sequelize": 4,
            "typeorm": 4,
            "sqlalchemy": 4,
            "mongoose": 4,
        },
    },

    "api": {
        "core": {
            "rest api": 9,
            "restful": 8,
            "restful api": 9,
            "graphql": 8,
            "grpc": 7,
            "api development": 8,
            "api design": 8,
            "api integration": 7,
            "web api": 7,
            "web service": 7,
            "microservice": 7,
            "microservices": 7,
            "soap": 6,
            "websocket": 7,
            "socket.io": 7,
            "بناء api": 8,
            # Payment gateway integrations (2026-08-26 audit): voucher/redemption
            # API job (40671815) lacked payment-gateway-specific vocabulary.
            "stripe": 8,
            "razorpay": 8,
            "paypal": 7,
            # Integration/automation-workflow engineering (plural forms +
            # webhook/automation-tooling tokens so API-integration and
            # workflow-automation gigs claim backend instead of defaulting
            # to frontend or being suppressed). 2026-09-02 run 31:
            # 12576 (Shopify<->Camex sync via Make/Integromat + webhooks)
            # wrongly routed to frontend; 12588 (API integrations &
            # automation workflows) Gemini-picked frontend though its
            # tracks are backend/AI-agent integration work.
            "api integrations": 8,
            "rest apis": 8,
            # NOTE: 'webhook'/'webhooks'/'integration developer' are
            # intentionally NOT added here (2026-09-02 run 31 collateral):
            # they are too broad and fire on AI-agent, CRM, and trading
            # jobs, pushing clean ai_ml jobs (12434/12768) into spurious
            # arbitration and pulling previously-rejected CRM/trading jobs
            # (12274/12425) into consideration. Only specific integration
            # engineering tokens stay core so 12576 (via integromat/rest
            # apis) and 12588 (via api integrations/workflow automation)
            # claim backend without broad collateral.
            "workflow automation": 7,
            "workflow automations": 7,
            "automation workflow": 7,
            "automation workflows": 7,
            "integromat": 7,
        },
        "supporting": {
            "endpoint": 3,
            "endpoints": 3,
            "route": 2,
            "routes": 2,
            "controller": 3,
            "middleware": 3,
            "swagger": 4,
            "openapi": 4,
            "postman": 3,
        },
    },

    "architecture": {
        "core": {
            "backend": 8,
            "back-end": 8,
            "server side": 7,
            "server-side": 7,
            "serverless": 7,
            "hexagonal architecture": 7,
            "domain driven design": 7,
            "ddd": 6,
            "cqrs": 7,
            "event sourcing": 7,
            "message queue": 6,
            "rabbitmq": 7,
            "kafka": 7,
            "redis queue": 6,
            # ERP/CRM platforms (2026-08-26 audit): "ربط نظام Odoo" was
            # rejected with no matching keywords — genuine backend integration.
            "odoo": 8,
            "zoho": 7,
            "salesforce": 7,
            # Arabic backend-integration collocations.
            "ربط نظام": 8,
            "ربط مشروع": 7,
            # ERPNext/Frappe ERP platforms — custom builds & customization.
            "erpnext": 8,
            "frappe": 8,
            "فرابي": 8,
        },
        "supporting": {
            "clean architecture": 3,
            "repository pattern": 4,
            "service layer": 4,
            "dependency injection": 4,
            "inversion of control": 4,
        },
    },

    "auth_security": {
        "core": {
            "authentication": 7,
            "authorization": 7,
            "oauth": 7,
            "oauth2": 7,
            "jwt": 7,
            "session management": 6,
            "rbac": 7,
            "role-based access": 7,
            "access control": 6,
            "encryption": 5,
            "hashing": 4,
            "bcrypt": 5,
        },
        "supporting": {
            "ssl": 3,
            "tls": 3,
            "login": 3,
            "signup": 3,
            "registration": 3,
            "password reset": 3,
            "two factor": 3,
            "2fa": 3,
            "mfa": 3,
        },
    },

    "devops_backend": {
        "core": {
            "docker": 7,
            "دوكر": 7,
            "vps": 7,
            "kubernetes": 7,
            "nginx": 6,
            "apache": 6,
            "linux server": 6,
            "ci/cd": 6,
            "jenkins": 6,
            "github actions": 6,
            "gitlab ci": 6,
            "heroku": 5,
            "digitalocean": 5,
            "linode": 5,
            "vultr": 5,
            "devops": 7,
            "dev ops": 7,
        },
        "supporting": {
            "aws": 3,
            "gcp": 3,
            "azure": 3,
            "deployment": 3,
            "scaling": 3,
            "load balancer": 3,
            "reverse proxy": 3,
            "cors": 3,
            "rate limiting": 3,
            "caching": 3,
            "cdn": 3,
        },
    },

    "healthcare": {
        "core": {
            "hl7": 8,
            "mirth": 7,
            "fhir": 7,
        },
        "supporting": {},
    },

    # Arabic web-platform build family (2026-08-30 run 24). CORE here (and
    # in frontend) so a generic 'منصة تعليمية' build produces two direct
    # matches -> needs_gemini -> LLM lands it on full_stack/backend/frontend
    # instead of a silent reject (mostaql:1272797 + 30 corpus siblings).
    # Variants: 'ت' typo; V ل-prefixed forms (لمنصة تعليمية) break \b after
    # normalize (ل attaches to منصة), so they need their own entries.
    "web_platforms": {
        "core": {
            "منصة تعليمية": 7,
            "منصت تعليمية": 7,
            "لمنصة تعليمية": 7,
            "لمنصت تعليمية": 7,
            # Generic Arabic web-platform build (2026-09-03 run 32). Mirrors
            # frontend 'منصة ويب' so a generic platform build produces two
            # direct matches -> needs_gemini -> full_stack (12804 subscriptions
            # platform, 13035 travel-planning platform). Concrete 'منصة ويب'
            # phrase only, not bare منصة (collateral).
            "منصة ويب": 7,
            "منصت ويب": 7,
        },
        "supporting": {},
    },

    # Email-infrastructure/automation builds. Build vocabulary only:
    # pure tool-onboarding / cold-email-service gigs reach arbitration
    # (lone 'email deliverability' < supporting minimum) and reject.
    "email_infrastructure": {
        "core": {
            "email automation": 8,
            "outbound email": 8,
            "outbound email system": 9,
            "email sequence": 7,
            "email sequences": 8,
            "email infrastructure": 8,
            "email deliverability": 7,
            "reply detection": 6,
            "smartlead": 8,
            "n8n": 8,
            "cold email system": 8,
        },
        "supporting": {},
    },

    # Data-pipeline/feed engineering. API pull -> transform (CSV) ->
    # publish as a live feed/ticker. 2026-09-04 run 34 fn_llm:
    # freelancer:40690663 'Real-time Data Integration for Webpage' only
    # matched frontend 'web development', Gemini had no backend candidate
    # and chose none. The Sharekhan API->CSV->live-feed task is backend
    # integration/scripting; explicit feed-pipeline cores claim it.
    "data_feed_pipelines": {
        "core": {
            "data integration": 8,
            "real-time data": 7,
            "real time data": 7,
            "live data": 7,
            "live feed": 6,
            "live feeds": 6,
            "live ticker": 6,
        },
        "supporting": {
            "csv": 2,
            "csv file": 2,
            "csv files": 2,
        },
    },

    # No-code / low-code automation-platform engineering. Certified
    # routing rule (2026-09-04 run 34): Make.com and sibling
    # workflow-automation platforms are BACKEND integration canvases.
    # fn_guard freelancer:40691106 'Make.com Automated Invoicing Setup'
    # direct-selected frontend via an HTML invoice template and was
    # guard-suppressed; the deliverable is a Make.com scenario/blueprint
    # wired to a checkout event. Recurring family (12576/12936/13132).
    # 'webhook' stays OUT (2026-09-02 run 31 broad-collateral lesson).
    "automation_platforms": {
        "core": {
            "make.com": 8,
            "make scenario": 7,
            "make scenarios": 7,
            "make automation": 7,
            "make automations": 7,
            "make blueprint": 7,
            "make.com account": 8,
            "zapier": 7,
            "rpa": 6,
            "rpa automation": 7,
        },
        "supporting": {},
    },

    # Auction/marketplace platform builds (VIN-search, live bidding,
    # membership, payments).
    "marketplace": {
        "core": {
            "auction": 7,
            "auction platform": 8,
            "auction website": 8,
            "live auction": 8,
            "online auction": 8,
            "مزاد": 7,
            "المزاد": 7,
        },
        "supporting": {},
    },
}


# Negative evidence: other software domains to avoid overlap.
NEGATIVE_KEYWORDS = {

    # Recruitment/staffing ads are employment offers, not project gigs.
    "recruitment": {
        "core": {
            "staff augmentation": 8,
        },
        "supporting": {},
    },

    "writing_content": {
        "core": {
            "copywriter": 14,
        },
        "supporting": {},
    },

    "nocode_platforms": {
        "core": {
            "power apps": 8,
            "power platform": 8,
            # 2026-08-28 audit: freelancer:40676123 "Adalo App Logic Revamp"
            # (a no-code logic-FIXING gig, explicitly "NOT build-from-scratch")
            # direct-selected backend via rbac/access-control cores. Adalo is
            # the specific no-code platform; keeps the FP from the direct
            # notify path (guard happened to suppress it, but the classifier
            # should not have accepted deterministically).
            "adalo": 8,
        },
        "supporting": {},
    },

    "frontend": {
        "core": {
            "react": 8,
            "reactjs": 8,
            "react.js": 8,
            "nextjs": 8,
            "next.js": 8,
            "vue": 8,
            "vuejs": 8,
            "vue.js": 8,
            "angular": 8,
            "angularjs": 8,
            "svelte": 8,
            "sveltejs": 8,
            "html": 6,
            "css": 6,
            "scss": 6,
            "sass": 6,
            "tailwind": 6,
            "tailwindcss": 6,
            "bootstrap": 6,
            "material ui": 6,
            "chakra ui": 6,
            "frontend": 7,
            "front-end": 7,
            "front end": 7,
            "landing page": 8,
            "landing pages": 8,
            "portfolio website": 7,
            "personal website": 7,
            "responsive design": 6,
            "responsive web": 6,
            "figma to code": 7,
            "figma to html": 7,
            "figma to react": 7,
            "wordpress": 8,
            "woocommerce": 8,
            "shopify": 8,
            "webflow": 7,
            "wix": 7,
            "elementor": 7,
            "divi": 7,
        },
        "supporting": {
            "javascript": 3,
            "typescript": 3,
            "ui implementation": 4,
            "pixel perfect": 4,
            "browser": 2,
            "web app": 4,
            "web application": 4,
        },
    },

    "mobile": {
        "core": {
            "flutter": 8,
            "dart": 7,
            "react native": 8,
            "swift": 7,
            "swiftui": 7,
            "uikit": 7,
            "kotlin": 7,
            "jetpack compose": 7,
            "android": 6,
            "android studio": 7,
            "ios": 6,
            "iphone": 6,
            "ipad": 6,
            "xcode": 7,
            "mobile app": 7,
            "mobile application": 7,
            "app store": 5,
            "google play": 5,
        },
        "supporting": {
            "cross-platform": 3,
            "cross platform": 3,
            "ionic": 4,
            "capacitor": 4,
            "cordova": 4,
            "firebase": 3,
            "supabase": 3,
        },
    },

    "game": {
        "core": {
            "unity": 8,
            "unity3d": 8,
            "unreal engine": 8,
            "unreal": 7,
            "ue5": 8,
            "ue4": 8,
            "godot": 8,
            "game design": 8,
            "game development": 8,
            "game dev": 7,
            "gameplay": 7,
            "game mechanic": 7,
            "game mechanics": 7,
            "game programmer": 7,
            "game developer": 7,
            "indie game": 7,
            "mobile game": 7,
            "pc game": 6,
            "console game": 6,
            "game prototype": 7,
            "تصميم لعبة": 7,
            "برمجة ألعاب": 7,
            "تطوير ألعاب": 7,
            "مطور ألعاب": 7,
        },
        "supporting": {
            "game level": 3,
            "level design": 3,
            "game world": 3,
            "game ai": 3,
            "npc": 2,
            "inventory system": 2,
            "quest system": 3,
            "dialogue system": 3,
            "game physics": 3,
            "collision detection": 3,
            "game state": 2,
            "game save": 2,
            "save system": 2,
            "game balance": 3,
            "game economy": 3,
            "microtransaction": 2,
            "in-app purchase": 2,
            "game monetization": 3,
        },
    },

    "enterprise": {
        # CRM integration/customization builds are legitimate work
        # (customization IS work for a linked business function).
        # 'crm' demoted core->supporting (2026-08-29 audit): the core
        # token was title-killing the whole WhatsApp/CRM/AI-bot class
        # (e.g. rowid 10758, a WhatsApp AI-bot + CRM build).
        "core": {
            "erp": 7,
            "wms": 7,
            "saas": 6,
            "netsuite": 7,
            "netsuite": 7,
            "business central": 7,
            "dynamics 365": 7,
        },
        "supporting": {
            "crm": 4,
            "mvp": 3,
            "software development": 4,
            "admin panel": 4,
            "control panel": 3,
            "admin dashboard": 4,
            "user management": 4,
            "inventory management": 4,
        },
    },

    "data_analysis": {
        "core": {
            "data analysis": 8,
            "data analyst": 8,
            "power bi": 8,
            "tableau": 8,
            "business intelligence": 7,
            "business analyst": 7,
            "financial analysis": 8,
            "financial analyst": 8,
            "looker": 7,
            "looker studio": 7,
            "qlik": 7,
            "تحليل بيانات": 8,
            "محلل بيانات": 8,
            "تحليل مالي": 8,
        },
        "supporting": {
            "excel": 4,
            "pivot table": 4,
            "dashboard": 3,
            "analytics": 2,
            "kpi": 3,
            "reporting": 2,
            "etl": 3,
        },
    },

    "ai_ml": {
        "core": {
            "machine learning": 8,
            "deep learning": 8,
            "neural network": 7,
            "tensorflow": 8,
            "keras": 7,
            "pytorch": 8,
            "scikit-learn": 8,
            "sklearn": 8,
            "nlp": 7,
            "natural language processing": 7,
            "computer vision": 7,
            "llm": 7,
            "large language model": 7,
            "openai": 7,
            "gpt": 6,
            "data science": 8,
            "data scientist": 8,
            "predictive model": 7,
            "forecasting model": 7,
            "recommendation system": 7,
            "recommendation engine": 7,
        },
        "supporting": {
            "chatgpt": 3,
            "transformer": 3,
            "regression": 3,
            "classification": 3,
            "clustering": 3,
            "random forest": 4,
            "xgboost": 4,
            "lightgbm": 4,
            "hugging face": 4,
            "langchain": 4,
            "rag": 3,
            "fine-tuning": 4,
            "fine tuning": 4,
            "model training": 4,
        },
    },

    "education": {
        "core": {
            "teach": 7,
            "teaching": 7,
            "teacher": 7,
            "tutor": 7,
            "tutoring": 7,
            "مدرب": 6,
            "تعليم": 6,
            "تعليمي": 6,
            "معلم": 6,
            "مدرس": 6,
        },
        "supporting": {
            "mentor": 3,
            "mentoring": 3,
            "training": 3,
            "course": 3,
            "courses": 3,
            "curriculum": 3,
            "lesson": 2,
            "lessons": 2,
            "student": 2,
            "students": 2,
        },
    },
    "marketing": {
        # Negative-core placement (not HARD_REJECT): these phrases occur
        # inside genuine build scopes; pure-marketing gigs still reject.
        "core": {
            "translation": 8,
            "google ads": 8,
            "facebook ads": 8,
            "digital marketing": 8,
            "lead generation": 8,
            "lead gen": 7,
            "lead generating": 7,
            "lead generator": 7,
            "lead generators": 7,
            "generación de leads": 7,
            "جلب العملاء": 7,
        },
        "supporting": {},
    },
}


# Hard rejects: unrelated work rejected when no positive signal exists.
HARD_REJECT_KEYWORDS = {
    # Data-entry/illustrator TOKENS intentionally NOT hard-rejected here:
    # platform builds enumerate them as target-user roles
    # (freelancer:40676186 Perancangan Aplikasi Freelancer listed Data
    # Entry/Illustrator and every category hard-rejected, blocking
    # arbitration of a genuine Laravel/Node/MySQL build). Pure data-entry
    # work still rejects via insufficient_signal in all categories and the
    # data_analysis automation negatives; graphic-design work rejects in
    # data_analysis. Arabic protection kept via the إدخال بيانات family in
    # data_analysis negatives.
    "graphic design",
    "photoshop",
    "video editing",
    "motion graphics",
    "backlink",
    "backlinks",
    "link building",
    "copywriting",
    "commission only",
    "عمولة فقط",
    "internship",
    "intern",
    "volunteer",
    "unpaid",
    "تدريب صيفي",
    "متدرب",
    "متطوع",
    "بدون مقابل",
    # Recurring SMM-reseller storefront spam (8 corpus occurrences,
    # all rejected); hard-rejected so reposts don't burn arbitration
    # calls now that store-build collocations became positive cores.
    "خدمات smm",
    "letter writing", "request letter",
    # PCB/hardware engineering gigs reach backend via generic api/db vocab.
    "pcb",
    # Gambling/real-money gaming products are out of scope (policy block).
    "gambling", "casino", "igaming", "jackpot", "poker", "roulette",
    "blackjack", "satta matka", "spin and win", "spin win", "lucky jet",
    "sportsbook", "sports betting", "slot machine",
    # lottery/lotto added 2026-08-28 run 20: full-corpus sweep found 1
    # historical ACCEPTED 'Automated Lottery' backend job (rowid 8863);
    # unambiguous gambling, 0 window / 0 corpus innocent collateral.
    "lottery", "lotto",
    # Adult/18+ web-content products are out of scope (policy block,
    # 2026-08-29 run 22). freelancer:40677922 'Website Development'
    # (porn site build modelled on beeg.com: uploader roles, paysite
    # tags, VR player) was keyword_direct NOTIFIED as frontend/full_stack
    # and DELIVERED -- fp_delivered. Full-corpus: these tokens have 0
    # accepted collateral in any category (historical adult/escort jobs
    # all already rejected). 'nsfw' added 2026-08-29 run 23 -- full-corpus
    # impact is exactly 2 jobs: freelancer:40660265 (ai_ml, ACCEPTED
    # pre-policy, explicit PG violation: 'AI mini-series ... along with
    # NSFW variants') and freelancer:40641766 (already rejected); both
    # llm-arbitrated so replay-safe.
    "porn", "paysite", "paysites", "beeg", "nsfw",
    "adult site", "adult website",
    "escort service", "escort services",
    # companion-rental / rental-boyfriend / escort-style booking platforms are
    # adult-adjacent and OUT OF SCOPE (2026-09-02 run 31). full_stack delivered
    # FPs 12635/12717 + rejected siblings 12329/12602/12692. Narrow phrases
    # only (deliberately NOT bare 'companion'/'escort' - care-companion /
    # dating collateral risk).
    "rental boyfriend", "boyfriend rental", "rent-a-boyfriend",
    "rental companion", "companion rental",
    "betting", "wager", "wagering",
    "1xbet", "dragon tiger", "dragon vs tiger",
    "quotex", "iq option", "binary options", "olymptrade", "pocket option",
    # Arbitration-none sweep (2026-08-26 run 14): 0 accepted corpus hits.
    "video player",
    "lead generation",
    "google ads",
    "facebook ads",
    "task creator",
    "software testing consultant",
    # 'digital marketing' NOT hard-rejected (2026-08-28 audit): same
    # enumeration-misfire pattern as data entry/illustrator
    # (freelancer:40676186). Marketing negative core still rejects pure
    # marketing; build+marketing hybrids route to arbitration.
    # Hardware/IoT/firmware manufacturing is out of scope (same rationale
    # as mobile_app).  Rowid 13128 reached backend via guard; hard-rejecting
    # at keyword layer avoids wasting Gemini API calls.
    "wearable electronics", "embedded firmware",
}


# Noise: intentionally unscored terms that are too ambiguous to classify.
NOISE_KEYWORDS = {
    "system", "systems",
    "platform", "platforms",
    "api",
    "app", "apps", "application", "applications",
    "user", "users",
    "roles", "permissions",
    "نظام", "أنواع",
    "منصة", "منصات",
    "برنامج", "برامج", "برمجية",
    "موقع", "مواقع",
    "متجر", "متاجر",
    "واجهة", "تكامل", "التوثيق",
    "جوال", "موبايل",
    "المستخدمين",
    "صلاحيات",
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
