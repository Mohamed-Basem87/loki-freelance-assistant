# AI/ML Data Science category — tiered classifier vocabulary
#
# Core keywords are strong standalone evidence; supporting keywords require
# aggregation. NOISE_KEYWORDS are excluded from scoring entirely.

# Positive evidence: Machine Learning / Deep Learning / AI / Data Science.
POSITIVE_KEYWORDS = {

    "ml_frameworks": {
        "core": {
            "tensorflow": 9,
            "keras": 8,
            "pytorch": 9,
            "scikit-learn": 9,
            "sklearn": 9,
            "xgboost": 8,
            "lightgbm": 8,
            "catboost": 8,
            "jupyter": 7,
            "jupyter notebook": 7,
            "google colab": 7,
            "colab": 7,
        },
        "supporting": {
            "pandas": 4,
            "numpy": 4,
            "scipy": 4,
            "matplotlib": 4,
            "seaborn": 4,
            "plotly": 4,
            "dataframe": 4,
        },
    },

    "ml_concepts": {
        "core": {
            "machine learning": 9,
            "deep learning": 9,
            "neural network": 8,
            "neural networks": 8,
            "cnn": 7,
            "rnn": 7,
            "lstm": 7,
            # Demoted core->supporting (2026-08-24 audit): bare 'transformer'
            # phantom-fires on electrical-engineering gigs (23 corpus hits:
            # PSU/inverter/SMPS designs, rowid 7890 pure sine wave inverter)
            # producing lone-core arbitration calls that resolve to none.
            # Genuine transformer-model work carries sibling ML vocabulary
            # (nlp/llm/deep learning) and no longer depends on this token.
            "attention mechanism": 7,
            "reinforcement learning": 8,
            "supervised learning": 8,
            "unsupervised learning": 8,
            "semi-supervised learning": 7,
            "self-supervised learning": 7,
            "transfer learning": 8,
            "few-shot learning": 7,
            "zero-shot learning": 7,
            "meta learning": 7,
            "federated learning": 7,
        },
        "supporting": {
            "transformer": 4,
            "regression": 3,
            "classification": 3,
            "clustering": 3,
            "dimensionality reduction": 4,
            "feature engineering": 4,
            "feature selection": 4,
            "hyperparameter tuning": 4,
            "cross validation": 4,
            "train test split": 3,
            "overfitting": 3,
            "underfitting": 3,
            "bias variance": 3,
            "regularization": 3,
        },
    },

    "nlp": {
        "core": {
            "nlp": 8,
            "natural language processing": 8,
            "text classification": 7,
            "sentiment analysis": 7,
            "named entity recognition": 7,
            "ner": 6,
            "text generation": 7,
            "machine translation": 7,
            "text summarization": 7,
            "question answering": 7,
            "text mining": 7,
            "word embedding": 7,
            "word2vec": 7,
            "glove": 6,
            "bert": 7,
            "gpt": 6,
            "llm": 7,
            "large language model": 7,
            "openai": 7,
            "hugging face": 7,
            "langchain": 7,
            "rag": 6,
            "retrieval augmented generation": 7,
            "speech recognition": 7,
            "speaker recognition": 7,
            "voice recognition": 7,
            "speech to text": 7,
        },
        "supporting": {
            "chatgpt": 3,
            "claude": 3,
            "gemini": 3,
            "corpus": 3,
            "corpora": 3,
            "embedding": 3,
            "vector": 3,
            "semantic": 3,
            "syntax": 3,
            "pos tag": 3,
            "chunking": 3,
        },
    },

    "computer_vision": {
        "core": {
            "computer vision": 8,
            "image classification": 7,
            "object detection": 7,
            "image segmentation": 7,
            "semantic segmentation": 7,
            "instance segmentation": 7,
            "face detection": 7,
            "face recognition": 7,
            "ocr": 6,
            "optical character recognition": 6,
            "image processing": 7,
            "video processing": 7,
            "yolo": 7,
            "opencv": 7,
            "pillow": 6,
            "image augmentation": 6,
        },
        "supporting": {
            "cnn": 4,
            "convolutional": 3,
            "pooling": 3,
            "anchor": 3,
            "bounding box": 3,
            "iou": 3,
            "map": 3,
        },
    },

    "data_science": {
        "core": {
            "data science": 9,
            "data scientist": 9,
            "predictive model": 8,
            "predictive modeling": 8,
            "forecasting model": 7,
            "time series": 7,
            "anomaly detection": 7,
            "recommendation system": 7,
            "recommendation engine": 7,
            "experiment design": 7,
            "causal inference": 7,
            "survival analysis": 7,
            "bayesian": 6,
            "bayesian statistics": 7,
            "statistical modeling": 7,
            # ML/model-building regression work (NOT descriptive stats).
            # 2026-09-02 run 31: 12348 'Python OOP Linear Regression'
            # (a from-scratch linear-regression MODEL implementation) was
            # wrongly claimed by data_analysis via numpy and suppressed by
            # the DA guard. This core makes ai_ml a competing candidate so
            # the ML-vs-DA boundary goes to arbitration instead of a
            # deterministic DA suppression.
            "linear regression": 7,
            "regression model": 7,
            "regression analysis": 6,
        },
        "supporting": {
            "eda": 4,
            "exploratory data analysis": 4,
            "hypothesis testing": 4,
            # Descriptive statistical analysis (ANOVA/SPSS/R stats) is
            # data_analysis work. Demoted core->supporting 2026-09-02
            # run 31: 12655/12682 were deterministic-routed to ai_ml via
            # the lone 'statistical analysis' core and suppressed by the
            # ai_ml guard. Now the data_analysis candidate (which has its
            # own cores for this) wins routing instead.
            "statistical analysis": 4,
            "p-value": 3,
            "confidence interval": 3,
            "correlation": 2,
            "distribution": 2,
        },
    },

    "mlops": {
        "core": {
            "model deployment": 7,
            "model serving": 7,
            "ml pipeline": 7,
            "mlops": 8,
            "model monitoring": 7,
            "model versioning": 6,
            "experiment tracking": 7,
            "mlflow": 7,
            "wandb": 7,
            "neptune": 6,
            "kubeflow": 7,
            # Full name only: bare "airflow" also matches HVAC/CAD prose.
            "apache airflow": 6,
            "prefect": 6,
            "dagster": 6,
        },
        "supporting": {
            "docker": 3,
            "kubernetes": 3,
            "ci/cd": 3,
            "model registry": 4,
            "feature store": 4,
            "data drift": 4,
            "concept drift": 4,
        },
    },

    "ai_general": {
        # Arabic: "smart assistant" carries build intent safely; bare
        # "artificial intelligence" mentions are too often tool references
        # in non-AI gigs, so they only contribute supporting weight.
        # Chatbot/bot-building vocabulary added as core (2026-08-29
        # audit): the class previously had ZERO core positives here, so
        # legit chatbot/WhatsApp-bot/appointment-booker builds all died at
        # insufficient_signal or title-core-negative. Arabic
        # "بوت ذكاء اصطناعي" fills the AI-chatbot build gap; the score-6
        # "بوت واتساب" (WhatsApp bot coder) needs supporting evidence to
        # direct-notify (lone-core rule), otherwise it arbitrates.
        "core": {
            "مساعد ذكي": 7,
            # Definite-article form (2026-09-03 run 32): 13039 mostaql
            # 'AuditAI المساعد الذكي للمحاسبين' (accounting system with
            # built-in AI chat assistant that explains figures/transactions
            # step-by-step + audit trail) rejected insufficient_signal. The
            # indefinite 'مساعد ذكي' can't match 'المساعد الذكي' because the
            # 'ال' prefix on 'ذكي' breaks the bare word (same definite-form
            # gap class as لمنصة تعليمية). Corpus blast radius = exactly
            # 13039 (0 window collateral).
            "المساعد الذكي": 7,
            "متخصص في الذكاء الاصطناعي": 7,
            "مطور ذكاء اصطناعي": 7,
            "أتمتة و ذكاء اصطناعي": 7,
            "بوت ذكاء اصطناعي": 7,
            "chatbot": 7,
            "chatbots": 7,
            "chat bot": 7,
            "بوت واتساب": 6,
        },
        "supporting": {
            "ذكاء اصطناعي": 4,
            "الذكاء الاصطناعي": 4,
            "بالذكاء الاصطناعي": 4,
            "والذكاء الاصطناعي": 4,
        },
    },

    "generative_ai": {
        "core": {
            "generative ai": 8,
            "generative ai": 8,
            "diffusion model": 7,
            "stable diffusion": 7,
            "midjourney": 6,
            "dall-e": 6,
            "comfyui": 6,
            "comfy ui": 6,
            "gan": 7,
            "generative adversarial": 7,
            "vae": 6,
            "variational autoencoder": 6,
            "qlora": 6,
            "prompt engineering": 7,
            "prompt design": 6,
            "ai agent": 7,
            "ai agents": 7,
            "autonomous agent": 7,
        },
        "supporting": {
            "lora": 3,
            "fine-tuning": 3,
            "fine tuning": 3,
            "fine-tune": 3,
            "token": 3,
            "tokenization": 3,
            "inference": 3,
            "training": 3,
            "dataset": 2,
            "annotation": 3,
            "labeling": 3,
        },
    },
}


# Negative evidence: other software domains to avoid overlap.
NEGATIVE_KEYWORDS = {

    "community_content": {
        "core": {
            "community manager": 8,
            "community management": 8,
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
    # Arbitration-none sweep (2026-08-26 run 14): 0 accepted corpus hits.
    "lead generation",
    "digital marketing",
    "task creator",
    # Gambling/real-money gaming products are out of scope (policy block).
    "gambling", "casino", "igaming", "jackpot", "poker", "roulette",
    "blackjack", "sportsbook", "sports betting", "slot machine",
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
    # Crash/binary-Gambling variant phrases wrapped in benign-sounding
    # reward/spin language (2026-09-03 run 32). spin-win/lucky-jet/satta-matka
    # were missing here but present in frontend/backend/game_dev - a job naming
    # these would slip past data_analysis/ai_ml hard-reject. All unambiguous
    # gambling; 0 expected window/corpus innocent collateral.
    "spin and win", "spin win", "lucky jet", "satta matka",
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
