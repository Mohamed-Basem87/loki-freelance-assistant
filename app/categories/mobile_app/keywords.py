# Mobile App Development category — tiered classifier vocabulary
#
# Core keywords are strong standalone evidence; supporting keywords require
# aggregation. NOISE_KEYWORDS are excluded from scoring entirely.

# Positive evidence: Mobile App Development / iOS / Android / Cross-platform.
POSITIVE_KEYWORDS = {

    "frameworks": {
        "core": {
            "flutter": 9,
            "dart": 8,
            "react native": 9,
            "swift": 8,
            "swiftui": 8,
            "uikit": 7,
            "kotlin": 8,
            "jetpack compose": 8,
            "ionic": 7,
            "capacitor": 7,
            "cordova": 7,
            "xamarin": 7,
            "maui": 7,
            ".net maui": 7,
        },
        "supporting": {
            "cross-platform": 4,
            "cross platform": 4,
            "hybrid app": 4,
            "native app": 4,
        },
    },

    "platforms": {
        "core": {
            "android": 7,
            "android studio": 8,
            "ios": 7,
            "iphone": 6,
            "ipad": 6,
            # Arabic iPhone forms (2026-08-28 FN recovery): nafezly:54135
            # 'مقاسات التطبيق' -- iPhone app spacing/size adaptation across
            # all devices -- matched only غضف weak 'تطبيق' and was rejected.
            # 'ايفون' mirrors the English 'iphone'; device-variant phrases
            # cover 'كل اجهزه الايفون / جميع اجهزة الايفون'.
            "ايفون": 6,
            "اجهزه الايفون": 7,
            "اجهزة الايفون": 7,
            "xcode": 8,
            "playstore": 7,
            "play store": 7,
            "app store": 7,
            "google play": 6,
            "apple developer": 7,
            "developer account": 6,
        },
        "supporting": {
            "mobile": 3,
            "smartphone": 3,
            "tablet": 3,
            "phone": 2,
        },
    },

    "app_types": {
        "core": {
            "mobile app": 8,
            "mobile application": 8,
            "android app": 8,
            "ios app": 8,
            "iphone app": 8,
            "ipad app": 7,
            "تطبيق جوال": 8,
            "تطبيق موبايل": 8,
            "تطبيق اندرويد": 8,
            "تطبيق أندرويد": 8,
            "تطبيق ايفون": 8,
            "تطبيق ios": 8,
            "تطبيق iphone": 7,
            # Arabic transliterations of "App Store" / store names
            # (publishing gigs). Corpus evidence: نشر تطبيق على ابل ستور,
            # رفع تطبيق على متجر أبل, رف تطبيق طبي (متجر بلي).
            "ابلستور": 8,
            "ابل ستور": 8,
            "اب ستور": 8,
            "متجر أبل": 8,
            "متجر بلي": 8,
            # Generic Arabic phrase for "a mobile/web application" --
            # corpus: انشاء وبرمجة تطبيق للعقود, تطبيق إلكتروني خياطة.
            "تطبيق إلكتروني": 8,
            # Arabic build-intent collocations (2026-08-24 audit). Corpus:
            # 'تطوير تطبيق' 21/21 genuine app builds (7049/7257/7568 accepted,
            # older rows silently rejected by vocabulary poverty);
            # 'انشاء تطبيق' 16 matches all builds (NFC reader gig accepted);
            # 'تصميم تطبيق' / 'برمجة تطبيق' same family;
            # 'تطبيق هاتف' 8 matches all app/system builds (7940+7960
            # video-call client app missed pair); 'تطبيق بث' live-streaming
            # apps (7934+7936 missed pair).
            "تطوير تطبيق": 8,
            "انشاء تطبيق": 8,
            "تصميم تطبيق": 7,
            "برمجة تطبيق": 7,
            "تطبيق هاتف": 7,
            "تطبيق بث": 7,
            "بناء تطبيق": 7,
            "لبناء تطبيق": 7,
            "بناء تطبيقات": 7,
            # Voice-chat / voice-room social apps.
            "تواصل صوتي": 8,
            "غرف صوتية": 7,
            "دردشة صوتية": 7,
            # Text chat-app builds (2026-08-29 audit): Arabic "group chat
            # app" class. Corpus: 'تطبيق دردشة' 3 hits all mobile chat
            # apps (9113, 9838 accepted); 'دردشة جماعية' 1 hit (the
            # recovered job). Extends the voice-chat family to text chat.
            "تطبيق دردشة": 8,
            "دردشة جماعية": 7,
        },
        "supporting": {
            "app development": 4,
            "app developer": 4,
            "تطبيق": 2,
            "تطبيقات": 2,
            # Arabic web-app collocation (2026-08-26 audit): not in app_types
            # core; genuine mobile/web hybrid builds.
            "تطبيق ويب": 6,
            # UI layout-adaptation phrasing (2026-08-28 FN recovery):
            # 'تعديل المساحات والاحجام / تعديل الشاشات' in nafezly:54135
            # corroborate the iPhone-app core matched in the same posting
            # (satisfies the lone-core supporting-weight requirement).
            "تطبيق للايفون": 6,
            "تعديل المساحات والاحجام": 4,
            "تعديل الشاشات": 4,
        },
    },

    "mobile_specific": {
        "core": {
            "firebase": 7,
            "supabase": 6,
            "push notification": 7,
            "in-app purchase": 7,
            "app monetization": 6,
            "admob": 7,
            "ads integration": 5,
            "gps": 4,
            "location services": 5,
            "biometric": 5,
            "face id": 5,
            "touch id": 5,
            "fingerprint": 4,
            "offline storage": 5,
            "local database": 4,
            "core data": 6,
            "shared preferences": 4,
        },
        "supporting": {
            "subscription": 3,
            "notifications": 3,
            "sqlite": 3,
            "camera": 3,
            "splash screen": 3,
            "onboarding": 3,
            "deep linking": 4,
            "app links": 3,
            "universal links": 4,
            "app lifecycle": 3,
            "background sync": 4,
            "battery optimization": 3,
            "performance optimization": 3,
            "crashlytics": 4,
            "analytics": 2,
            "app store optimization": 4,
            "aso": 4,
        },
    },
}


# Negative evidence: other software domains to avoid overlap.
NEGATIVE_KEYWORDS = {

    # App-store review/rating manipulation gigs (buying reviews and
    # ratings) -- not development work. Kept as a negative core rather
    # than hard reject so a genuine build that legitimately mentions a
    # reviews feature still reaches arbitration instead of dying here.
    "aso_reviews": {
        "core": {
            "مراجعات وتقييمات": 8,
        },
        "supporting": {},
    },

    "security_testing": {
        "core": {
            "bug bounty": 14,
        },
        "supporting": {},
    },

    # Developer-console account trading/transfer services, not app builds.
    "account_services": {
        "core": {
            "app transfer": 6,
        },
        "supporting": {},
    },

    # Phone-unlock / device-data-recovery gigs, not app builds. `unlock`
    # alone is NOT used: full-corpus scan shows ~14 genuine builds merely
    # mention it (game level unlocks, medical apps) and would wrongly
    # route to arbitration. `data recovery`/`forensics` target the class
    # surgically. Corpus (2026-08-28 run 20): the SAME 'Unlock Samsung
    # A35' posting split outcomes -- freelancer:40676863 rejected (gemini
    # none) while duplicate freelancer:40677010 was keyword_direct NOTIFIED
    # mobile_app on android/phone positives; historical FPs 40669950
    # (recover deleted texts, keyword_direct accepted) and 40670144 (phone
    # hacker, keyword_direct accepted) are the same class. Negatives force
    # placement to arbitration (mixed) instead of a wrong direct notify.
    "device_unlock": {
        "core": {
            "data recovery": 8,
            "forensics": 8,
        },
        "supporting": {},
    },

    # Hardware/mechanical product design that merely mentions an app.
    "hardware_design": {
        "core": {
            "cad": 8,
            "solidworks": 8,
            "feasibility study": 6,
            "feasibility": 5,
        },
        "supporting": {},
    },

    "beta_testing": {
        "core": {
            "beta testers": 7,
            "beta tester": 7,
            "closed beta": 7,
            "closed test": 7,
            "closed testing": 7,
            "app tester": 7,
            "mobile app tester": 7,
            "qa tester": 7,
            # Test-only engagements (2026-08-28 audit). Corpus + window:
            # 40676212 'Functional Testing for Vagaro Pro Application' and
            # mostaql:1272413 'إختبار تطبيق على هاتف الإيفون' were ACCEPTED
            # and NOTIFIED as mobile_app; 'app testing' 13 hits/3 accepted
            # (two direct NOTIFIED: 40667212, 40665366). Arabic form
            # normalizes from both اختبار/إختبار. Mixed with a mobile build
            # core these force arbitration instead of a wrong direct notify.
            "app testing": 8,
            "functional testing": 8,
            "user acceptance testing": 8,
            "اختبار تطبيق": 8,
            # Separator-surface forms (2026-08-28 run 20 audit):
            # mostaql:1272465 'عمل اختبار لتطبيق (iOS)' -- read the usage
            # guide, log defects with screenshots -- slipped past the
            # contiguous 'اختبار تطبيق' negative and was keyword_direct
            # NOTIFIED as mobile_app. The 'ل' breaks the boundary, so the
            # phrase never formed.
            "اختبار لتطبيق": 8,
            "اختبار للتطبيق": 8,
        },
        "supporting": {},
    },

    # App/ASO marketing gigs (2026-08-28 audit). Corpus: mostaql:1271628
    # 'إعلان ممول لتطبيق إدارة مالية' (paid ad campaign for an iPhone app,
    # $100 budget to boost downloads) is marketing, not development; the
    # Arabic iPhone vocabulary added for nafezly:54135 recovery would
    # otherwise surface it. Negative core forces reject when there is no
    # genuine build core, and arbitration when one is also present.
    "growth_marketing": {
        "core": {
            "حملة اعلانية": 8,
            "حملة تسويقية": 8,
            "اعلان ممول": 8,
            "زيادة تحميل التطبيق": 8,
            "زيادة التنزيلات": 8,
        },
        "supporting": {},
    },

    "desktop_os": {
        "core": {
            "native macos": 7,
            "native mac os": 7,
            "macos application": 7,
            "mac os application": 7,
        },
        "supporting": {},
    },

    "frontend": {
        "core": {
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

    "web": {
        "core": {
            "website": 6,
            "web development": 7,
            "web developer": 7,
            "web design": 6,
            "web app": 6,
            "web application": 6,
            "تطوير موقع": 6,
            "تطوير مواقع": 6,
            "مطور ويب": 6,
            "مطور مواقع": 6,
            "موقع إلكتروني": 6,
            "موقع الكتروني": 6,
        },
        "supporting": {
            "seo": 3,
            "search engine": 2,
            "domain": 2,
            "hosting": 2,
            "ssl": 2,
            "cms": 3,
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
    "graphic design",
    "photoshop",
    "illustrator",
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
    # Review/rating manipulation phrasing (رفع تقييم تطبيق أبل ستور).
    "رفع تقييم",
    # Developer-account purchase requests (شراء حساب مطوّر Google Play
    # Console) -- account trading, never app development.
    "شراء حساب",
    # PCB/hardware engineering gigs reach mobile via platform-name vocab.
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
    # Arbitration-none sweep (2026-08-26 run 14): 0 accepted corpus hits.
    "wechat",
    "lead generation",
    # 'digital marketing' NOT hard-rejected (2026-08-28 audit): same
    # enumeration-misfire pattern as data entry/illustrator
    # (freelancer:40676186). Marketing negative core still rejects pure
    # marketing; build+marketing hybrids correctly route to arbitration.
}


# Noise: intentionally unscored terms that are too ambiguous to classify.
NOISE_KEYWORDS = {
    "system", "systems",
    "platform", "platforms",
    "api",
    "app", "apps", "application", "applications",
    "user", "users",
    "ui", "ux",
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
