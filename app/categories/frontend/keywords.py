# Frontend Development category — tiered classifier vocabulary
#
# Core keywords are strong standalone evidence; supporting keywords require
# aggregation. NOISE_KEYWORDS are excluded from scoring entirely.

# Positive evidence: Frontend Development / UI Implementation / Web UI.
POSITIVE_KEYWORDS = {

    "frameworks": {
        "core": {
            "react": 9,
            "reactjs": 9,
            "react.js": 9,
            "nextjs": 9,
            "next.js": 9,
            "vue": 9,
            "vuejs": 9,
            "vue.js": 9,
            "angular": 9,
            "angularjs": 9,
            "svelte": 9,
            "sveltejs": 9,
            "solidjs": 8,
            "preact": 8,
            "qwik": 8,
        },
        "supporting": {
            "redux": 4,
            "zustand": 4,
            "pinia": 4,
            "vuex": 4,
            "ngrx": 4,
            "recoil": 4,
            "jotai": 4,
        },
    },

    "styling": {
        "core": {
            "tailwind": 8,
            "tailwindcss": 8,
            "bootstrap": 7,
            "material ui": 8,
            "mui": 7,
            "chakra ui": 7,
            "shadcn": 8,
            "shadcn ui": 8,
            "ant design": 7,
            "antd": 7,
            "styled components": 7,
            "emotion": 6,
            "sass": 7,
            "scss": 7,
            "css modules": 7,
            "gsap": 6,
        },
        "supporting": {
            "less": 3,
            "responsive design": 4,
            "responsive web": 4,
            "mobile first": 3,
            "grid layout": 3,
            "flexbox": 3,
            "css grid": 3,
            "animation": 2,
            "transitions": 2,
        },
    },

    "ui_ux": {
        "core": {
            "frontend": 8,
            "front-end": 8,
            "front end": 8,
            "ui development": 8,
            "ui implementation": 8,
            "figma to code": 9,
            "figma to html": 9,
            "figma to react": 9,
            "figma to vue": 9,
            "figma to angular": 9,
            "figma to next": 9,
            "figma to svelte": 9,
            "figma to tailwind": 9,
            "figma to component": 9,
            "figma to page": 9,
            "figma to ui": 9,
            "figma to web": 9,
            "figma to app": 9,
            "figma to dashboard": 9,
            "figma to landing": 9,
            "figma to website": 9,
            "figma to prototype": 9,
            "figma to mockup": 9,
            "figma to design": 9,
            "figma to pixel": 9,
            "figma to screen": 9,
            "figma to view": 9,
            "figma to layout": 9,
            "figma to interface": 9,
            "figma to element": 9,
            "figma to section": 9,
            "figma to block": 9,
            "figma to widget": 9,
            "figma to card": 9,
            "figma to form": 9,
            "figma to table": 9,
            "figma to modal": 9,
            "figma to popup": 9,
            "figma to menu": 9,
            "figma to sidebar": 9,
            "figma to header": 9,
            "figma to footer": 9,
            "figma to nav": 9,
            "figma to navbar": 9,
            "pixel perfect": 8,
            "pixel-perfect": 8,
            "pixel by pixel": 8,
            "design to code": 8,
            "mockup to code": 8,
            "design implementation": 8,
            "ui from figma": 8,
            "ui from design": 8,
            "ui from mockup": 8,
            "ui from prototype": 8,
        },
        "supporting": {
            "exact match": 3,
            "ui": 3,
            "ux": 3,
            "user interface": 3,
            "user experience": 3,
            "design system": 4,
            "component library": 4,
            "storybook": 4,
        },
    },

    "web_platforms": {
        "core": {
            "wordpress": 8,
            "woocommerce": 7,
            "shopify": 7,
            "webflow": 8,
            "wix": 7,
            "squarespace": 6,
            "elementor": 7,
            "divi": 6,
            "framer": 7,
            "duda": 7,
            "ثيم سلة": 6,
            "ثيمات سلة": 6,
            # Arabic e-store build-intent collocations (2026-08-24 audit).
            # Corpus: 'انشاء متجر' ~10 unique gigs, mostly genuine builds
            # (7881 accepted; SMM-spam variant now killed by hard reject);
            # 'تطوير متجر' Shopify/Next.js store builds (2767, 5628);
            # 'تصميم متجر' Silla/Zid store design+dev class.
            "انشاء متجر": 7,
            "تطوير متجر": 7,
            "تصميم متجر": 7,
            # Attached-prefix (لـ) surface form of تصميم متجر.
            "لتصميم متجر": 7,
            # Arabic site-build collocations (2026-08-28 run 20 audit):
            # mostaql:1272540 'متخصص في انشاء المواقع الاعلانات المبوبة'
            # (classified-ads website: membership, balance, ads, chats,
            # ratings, payments, QR, delivery reps) was deterministically
            # REJECTED -- no site-build vocabulary matched while
            # 'موقع'/'مواقع' stay in NOISE_KEYWORDS by design.
            "انشاء المواقع": 7,
            # Singular build phrase (2026-08-29 audit): 'انشاء موقع' fires
            # at word/start-of-text boundaries, whereas the و-prefixed
            # 'وانشاء المواقع' never forms under \b boundary matching.
            # Recovers the Arabic singular website-build class
            # (انشاء موقع / انشاء موقع الكتروني).
            "انشاء موقع": 7,
            "الاعلانات المبوبة": 7,
            # Compound verb-chain forms of تصميم متجر (verb + وتجهيز).
            "تصميم وتجهيز متجر": 7,
            "لتصميم وتجهيز متجر": 7,
            # Arabic Salla store BUILD-intent collocations (2026-08-28
            # audit). Corpus: mostaql:1272458 'تصميم وتأسيس متجر إلكتروني
            # على منصة سلة' rejected -- bare-platform support words
            # (منصة سلة + متجر إلكتروني + سله) totaled 8 < the 12
            # arbitration bar, and 'انشاء متجر' never forms contiguously
            # ('إنشاء وإعداد المتجر' normalizes to 'انشاء واعداد المتجر').
            # 'تأسيس متجر' 1/1 (ؤ->و normalize) build intent.
            "تصميم وتأسيس متجر": 7,
            "تأسيس متجر": 7,
            # Arabic transliteration: simple info/landing page builds.
            "صفحة تعريفية": 6,
            "ووردبريس": 7,
            "الووردبريس": 7,
            "وورد بريس": 7,
            "وردبيرس": 7,
            "ووردبيرس": 7,
            "وورد بيرس": 7,
            "وردبريس": 7,
            "ورد بريس": 7,
            "شوبيفاي": 7,
            "ووكومرس": 7,
            "e-commerce website": 7,
            "e-commerce site": 7,
            "e-commerce store": 7,
            "ecommerce website": 7,
            "ecommerce site": 7,
            "ecommerce store": 7,
            "multi vendor": 6,
            "متعددة التجار": 6,
            "متعدد التجار": 6,
            "موقع إلكتروني": 6,
            "مواقع إلكترونية": 6,
            "تصميم موقع": 6,
            "تطوير مواقع": 6,
            "تصميم مواقع": 6,
            "مطور مواقع": 6,
            "web developer": 7,
            "web development": 7,
            "web designer": 6,
            "web design": 6,
            "website development": 7,
            "website design": 6,
            "website build": 6,
            "website redesign": 7,
            "landing page": 6,
            # Arabic: "landing page".
            "صفحة هبوط": 6,
            # Exam/testing platform builds (English).
            "exam platform": 6,
            "web app": 6,
            "webapp": 6,
        },
        "supporting": {
            # Store mention alone is not web-dev evidence (customer service,
            # store management, product-entry gigs all name their store).
            "متجر إلكتروني": 4,
            # Salla platform names demoted core->supporting: marketing/
            # management/CRO hybrids (إدارة متجر سلة, تحسين معدل التحويل)
            # were direct-selecting on the platform name alone.
            "منصة سلة": 4,
            "متجر سلة": 4,
            # Plural/definite forms missed by the singular bigrams
            # (تصميم وتطوير المتاجر الإلكترونية في سلة, rowid 6067 class).
            "متاجر الكترونيه": 5,
            # Bare Salla platform token: 69 corpus hits dominated by the
            # Silla store design/build/management class that was silently
            # rejected for lack of vocabulary (8087 store UI redesign).
            "سله": 4,
            "e-commerce": 4,
            "ecommerce": 4,
            # Arabic e-learning/training platform builds (منصة تعليمية family);
            # supporting-only so content/QA gigs stay below the arbitration bar.
            "منصة تعليمية": 6,
            "منصة تدريب": 6,
            # English LMS/exam-platform build vocabulary (supporting tier:
            # course-content gigs also use these words).
            "lms": 4,
            "learning platform": 4,
            "e-learning": 4,
            "elearning": 4,
            "theme": 3,
            "plugin": 3,
            "template": 3,
            "custom theme": 4,
            "custom plugin": 4,
        },
    },

    "languages": {
        "core": {
            "html": 7,
            "css": 7,
            "javascript": 7,
            "typescript": 7,
        },
        "supporting": {
            "jsx": 3,
            "tsx": 3,
            "es6": 3,
            "es2015": 3,
            "web components": 3,
            "shadow dom": 3,
        },
    },

    # QA-tool names are deliberately absent from core: in production they
    # mark scraping/automation gigs, not frontend builds.
    "testing": {
        "core": {},
        "supporting": {
            "jest": 3,
            "unit test": 3,
            "integration test": 3,
            "e2e": 3,
            "end to end": 3,
            "testing library": 3,
        },
    },
}


# Negative evidence: other software domains to avoid overlap.
NEGATIVE_KEYWORDS = {

    # Domain/hosting transfer & connection config gigs are platform
    # operations, not frontend builds.
    "site_config": {
        "core": {
            "نقل الدومين": 7,
        },
        "supporting": {},
    },

    # Build-fix/maintenance contracts are ongoing operations, not builds.
    # Negative cores, not hard rejects: a genuine rebuild that mentions
    # maintenance still reaches arbitration instead of being killed here.
    # Corpus (2026-08-28 audit): 40676226 'WordPress Speed & Weekly
    # Maintenance' was DETERMINISTICALLY NOTIFIED; 'wordpress maintenance'
    # 8 hits / 0 accepted; 'website maintenance' 17 hits with 2 accepted
    # direct notifies (40675755 3-Month maintenance, 40672242 migration).
    "maintenance": {
        "core": {
            "wordpress maintenance": 8,
            "website maintenance": 8,
            "site maintenance": 8,
            "weekly maintenance": 8,
        },
        "supporting": {},
    },

    # Hosting/domain-transfer gigs are platform operations, not builds.
    # Corpus: 'نقل موقع' 7 hits/3 accepted -- two WordPress-transfer FPs
    # (40675962 in window guard-caught, 40673971) direct-selected via the
    # wordpress core positive (نقل مواقع absent from vocab).
    "site_transfer": {
        "core": {
            "نقل موقع": 8,
            "نقل موقعين": 8,
            "نقل مواقع": 8,
        },
        "supporting": {},
    },

    "backend": {
        "core": {
            "laravel": 8,
            "django": 7,
            "flask": 6,
            "fastapi": 6,
            "spring boot": 8,
            "spring": 7,
            ".net": 7,
            "asp.net": 7,
            "nodejs": 7,
            "node.js": 7,
            "express": 6,
            "nestjs": 7,
            "ruby on rails": 7,
            "ruby": 5,
            "golang": 4,
            "rust": 4,
            "rest api": 6,
            "restful": 6,
            "graphql": 5,
            "grpc": 5,
            "microservice": 6,
            "microservices": 6,
            "api development": 6,
            "api integration": 5,
            "backend": 7,
            "back-end": 7,
            "server side": 6,
        },
        "supporting": {
            "postgresql": 3,
            "mysql": 3,
            "mongodb": 3,
            "redis": 3,
            "elasticsearch": 3,
            "authentication": 2,
            "authorization": 2,
            "oauth": 2,
            "jwt": 2,
            "database design": 3,
            "database schema": 3,
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
            "تطوير لعبة": 7,
            "بناء لعبة": 7,
            "انشاء لعبة": 7,
            "مبرمج ألعاب": 7,
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
        "core": {
            "erp": 7,
            "crm": 7,
            "wms": 7,
            "saas": 6,
            "netsuite": 7,
            "netsuite": 7,
            "business central": 7,
            "dynamics 365": 7,
        },
        "supporting": {
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
    "data_entry": {
        "core": {
            "إدخال بيانات": 6,
            "ادخال بيانات": 6,
            "وإدخال بيانات": 6,
            "بإدخال بيانات": 6,
            "لإدخال بيانات": 6,
            "data entry": 6,
        },
        "supporting": {},
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
    # Data-entry/illustrator TOKENS are intentionally NOT hard-rejected
    # here: platform builds that enumerate their target users list them
    # (freelancer:40676186 "Perancangan Aplikasi Freelancer" listed Data
    # Entry/Illustrator as user roles and every category hard-rejected,
    # blocking arbitration for a genuine Flutter/React+Laravel build).
    # Data-entry gigs still fall to the data_entry negative core or
    # insufficient_signal; graphic-design gigs reject in data_analysis.
    # Hacked-site restore / account-recovery gigs are maintenance, not builds.
    "hacked",
    # PCB/hardware engineering misfires fire frontend tech-stack cores.
    "pcb",
    # Gambling/real-money gaming products are out of scope (policy block).
    "gambling", "casino", "igaming", "jackpot", "poker", "roulette",
    "blackjack", "satta matka", "spin and win", "spin win", "lucky jet",
    "sportsbook", "sports betting", "slot machine",
    # lottery/lotto added 2026-08-28 run 20: full-corpus sweep found 1
    # historical ACCEPTED 'Automated Lottery' backend job (rowid 8863);
    # unambiguous gambling, 0 window / 0 corpus innocent collateral.
    "lottery", "lotto",
    "betting", "wager", "wagering",
    "1xbet", "dragon tiger", "dragon vs tiger",
    "quotex", "iq option", "binary options", "olymptrade", "pocket option",
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
    # Arbitration-none sweep (2026-08-26 run 14): 0 accepted corpus hits.
    "zid", "زد",
    # 'digital marketing' is NOT hard-rejected here (2026-08-28 audit):
    # the phrase follows the same enumeration-misfire pattern as data
    # entry/illustrator (freelancer:40676186 platform build rejected when
    # target-user types were listed). The marketing NEGATIVE core already
    # rejects pure-marketing work; mixing with build vocab correctly routes
    # to arbitration instead of a blind hard kill.
}


# Noise: intentionally unscored terms that are too ambiguous to classify.
NOISE_KEYWORDS = {
    "system", "systems",
    "platform", "platforms",
    "api",
    "app", "apps", "application", "applications",
    "user", "users",
    "login", "signup", "session",
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
