# ============================================================
# keywords.py  —  Tiered keyword model
#
# ARCHITECTURE
# ------------
# Every keyword belongs to exactly one (polarity, category, tier):
#
#   polarity: POSITIVE_KEYWORDS  (Data Analysis / Excel / Power BI / SQL / Python)
#             NEGATIVE_KEYWORDS  (Web / Backend / Mobile / Enterprise / etc.)
#
#   tier:
#     "core"       -> unambiguous on its own. Presence of a single core
#                     keyword is sufficient evidence for that polarity.
#     "supporting" -> consistent with the domain but too generic to prove
#                     it alone (e.g. "dashboard", "report", "full stack").
#                     Only matters in aggregate, or to break ties.
#
# NOISE_KEYWORDS is a third, explicit bucket: words so overloaded across
# both domains (system, platform, api, users, ui, login, ...) that they
# are excluded from scoring ENTIRELY. They must never appear inside
# POSITIVE_KEYWORDS or NEGATIVE_KEYWORDS — a runtime check at the bottom
# of this file enforces that, so a future edit can't silently reintroduce
# the false-positive/false-negative bug this redesign fixes.
#
# filters.py contains NO scoring/weight logic of its own for domain
# classification — this file is the single source of truth for what is
# strong evidence, what is weak evidence, and what is not evidence at all.
# ============================================================


# ------------------------------------------------------------------
# POSITIVE_KEYWORDS — evidence the job IS Data Analysis / Excel /
# Power BI / SQL / Python-for-data.
# ------------------------------------------------------------------

POSITIVE_KEYWORDS = {

    "power_bi": {
        "core": {
            "power bi": 10,
            "powerbi": 10,
            "microsoft power bi": 10,
            "pbix": 10,
            "dax": 10,
            "power query": 9,
            "power query editor": 9,
            "power pivot": 8,
            "m query": 8,
            "m code": 7,
            "باور بي": 10,
            "بور بي": 10,
        },
        "supporting": {
            "dashboard": 4,
            "dashboards": 4,
            "interactive dashboard": 5,
            "measure": 3,
            "measures": 3,
            "calculated column": 3,
            "star schema": 6,
            "data model": 5,
            "data modeling": 5,
            "لوحة تحكم": 4,
            "لوحة معلومات": 4,
            "داشبورد": 4,
        },
    },

    "excel": {
        "core": {
            "excel": 8,
            "exel": 8,
            "اكسل": 8,
            "إكسل": 8,
            "اكسيل": 8,
            "pivot table": 8,
            "vlookup": 8,
            "xlookup": 8,
            "index match": 8,
            "excel workbook": 6,
            "excel sheet": 6,
            "excel file": 6,
            "شيت اكسل": 6,
            "شيت إكسل": 6,
            "ملف اكسل": 6,
            "ملف إكسل": 6,
            "ملف اكسيل": 6,
            "اكسيل شيت": 6,
            "اكسل شيت": 6,
        },
        "supporting": {
            "spreadsheet": 3,
            "workbook": 2,
            "worksheet": 2,
            "sheet": 2,
            "sheets": 2,
            "xlsx": 3,
            "xls": 3,
            "شيت": 3,
            "شيتات": 3,
            "pivot": 2,
            "lookup": 2,
            "remove duplicates": 3,
            "conditional formatting": 3,
            "financial model": 6,
            "financial modeling": 6,
            "google sheets": 3,
        },
    },

    "sql": {
        "core": {
            "sql": 6,
            "sql server": 7,
            "mysql": 6,
            "postgresql": 6,
            "postgres": 6,
            "sqlite": 6,
            "oracle": 6,
            "stored procedure": 5,
            "قاعدة بيانات": 6,
        },
        "supporting": {
            "database": 2,
            "query": 2,
            "queries": 2,
            "استعلام": 3,
            "استعلامات": 3,
        },
    },

    "data_analysis": {
        "core": {
            "data analysis": 9,
            "data analyst": 9,
            "tableau": 9,
            "business analyst": 8,
            "business intelligence": 8,
            "financial analysis": 9,
            "financial analyst": 9,
            "looker": 7,
            "looker studio": 7,
            "qlik": 7,
            "تحليل بيانات": 9,
            "محلل بيانات": 9,
            "تحليل مالي": 9,
            "تابلو": 8,
        },
        "supporting": {
            "analytics": 3,
            "analysis": 2,
            "sales analysis": 5,
            "marketing analysis": 4,
            "market analysis": 4,
            "customer analysis": 4,
            "hr analysis": 4,
            "forecast": 3,
            "forecasting": 3,
            "budget analysis": 4,
            "kpi": 3,
            "kpis": 3,
            "metrics": 2,
            "visualization": 3,
            "data visualization": 4,
            "report": 1,
            "reports": 1,
            "reporting": 2,
            "etl": 3,
            "data cleaning": 4,
            "excel dashboard": 5,
            "executive dashboard": 4,
            "dashboard design": 3,
            "business metrics": 3,
            "data transformation": 4,
            "transform data": 3,
            "fact table": 4,
            "dimension table": 4,
            "snowflake schema": 4,
            "cohort analysis": 4,
            "customer segmentation": 4,
            "data validation": 3,
            "pivot chart": 3,
            "dataset": 2,
            "datasets": 2,
            "clean data": 4,
            "cleaning data": 4,
            "prepare data": 3,
            "data preparation": 3,
            "preprocessing": 3,
            "preprocess": 3,
            "clean dataset": 4,

            "تحليل المبيعات": 4,
            "تحليل السوق": 4,
            "تحليل الأعمال": 4,
            "ذكاء الأعمال": 4,
            "تقارير": 2,
            "تقرير": 1,
            "مؤشرات الأداء": 3,
            "مؤشر أداء": 3,
            "مؤشرات": 2,
            "تنظيف البيانات": 4,
            "التنبؤ": 3,
            "لوحة بيانات": 3,
            "لوحة مؤشرات": 3,
            "تحويل البيانات": 3,
            "معالجة البيانات": 3,
            "بيانات": 1,
            "البيانات": 1,
            "داتا": 2,
            "الداتا": 2,
            "فرز": 2,
            "فلترة": 2,
            "دمج البيانات": 2,
            "تنسيق البيانات": 2,
            "ترتيب البيانات": 2,
            "استخراج البيانات": 3,
            "ادخال بيانات": 2,
            "إدخال بيانات": 2,
            "تنظيف الداتا": 4,
            "تجهيز الداتا": 3,
            "تنظيف": 2,
            "تنضيف": 2,
            "ينظف": 2,
            "تنقية": 2,
            "تجهيز البيانات": 3,
            "تهيئة البيانات": 3,
            "تحضير البيانات": 3,
            "تجهيز": 1,
            "تهيئة": 1,
            "تحضير": 1,
            "تحليل": 1,
            "للتحليل": 2,
        },
    },

    "python": {
        "core": {
            "pandas": 8,
            "numpy": 6,
            "dataframe": 6,
            "polars": 6,
            "openpyxl": 6,
            "xlwings": 6,
        },
        "supporting": {
            # bare "python" is ambiguous (automation / backend scripts also
            # use it), so it only counts as supporting evidence.
            "python": 3,
            "matplotlib": 4,
            "plotly": 4,
            "seaborn": 4,
            "jupyter": 4,
            "notebook": 3,
            "etl": 3,
            "csv": 2,
            "json": 1,
            "data processing": 4,
            "data pipeline": 4,
            "web scraping": 2,
            "scraping": 1,
        },
    },
}


# ------------------------------------------------------------------
# NEGATIVE_KEYWORDS — evidence the job is general software engineering
# (web / mobile / backend / enterprise / no-code / etc.), i.e. NOT what
# we're looking for.
# ------------------------------------------------------------------

NEGATIVE_KEYWORDS = {

    "web": {
        "core": {
            "wordpress": 9,
            "woocommerce": 8,
            "shopify": 8,
            "webflow": 7,
            "wix": 7,
            "react": 8,
            "reactjs": 8,
            "next.js": 8,
            "nextjs": 8,
            "vue": 8,
            "angular": 8,
            "svelte": 8,
            "landing page": 8,
            "landing pages": 8,
            "landingpage": 8,
            "portfolio website": 7,
            "personal website": 7,
            "resume website": 7,
            "cv website": 7,
            "موقع إلكتروني": 7,
            "موقع الكتروني": 7,
            "متجر إلكتروني": 8,
            "متجر الكتروني": 8,
            "صفحة هبوط": 8,
            "لاندنج بيج": 8,
            "موقع شخصي": 7,
        },
        "supporting": {
            "javascript": 3,
            "typescript": 3,
            "bootstrap": 2,
            "tailwind": 2,
            "frontend": 4,
            "web application": 4,
            "framer": 4,
            "elementor": 4,
            "تطوير موقع": 4,
            "تطوير مواقع": 4,
            "مطور ويب": 4,
            "مطور مواقع": 4,
            "تطبيق ويب": 4,
            "بورتفوليو": 3,
            "معرض أعمال": 3,
        },
    },

    "backend": {
        "core": {
            "laravel": 8,
            "django": 7,
            "spring boot": 8,
            "spring": 7,
            ".net": 7,
            "asp.net": 7,
            "nestjs": 7,
            "full stack": 9,
            "fullstack": 9,
            "مطور تطبيقات": 7,
            "مهندس برمجيات": 7,
        },
        "supporting": {
            "flask": 3,
            "fastapi": 4,
            "node": 4,
            "nodejs": 4,
            "express": 4,
            "backend": 5,
            "rest api": 4,
            "graphql": 3,
            "crud": 2,
            "authentication": 2,
            "authorization": 2,
            "برمجة": 3,
            "مبرمج": 4,
            "مطور": 4,
            "واجهة برمجية": 4,
            "واجهات برمجية": 4,
            "تكامل api": 4,
            "ربط api": 4,
            "واجهة خلفية": 4,
            "تسجيل دخول": 2,
            "المصادقة": 2,
        },
    },

    "mobile": {
        "core": {
            "flutter": 8,
            "react native": 8,
            "swift": 6,
            "kotlin": 6,
            "تطبيق اندرويد": 7,
            "تطبيق أندرويد": 7,
            "تطبيق ايفون": 7,
            "تطبيق ios": 7,
        },
        "supporting": {
            "android": 4,
            "ios": 4,
            "mobile application": 4,
            "application development": 3,
            "تطبيق جوال": 4,
            "تطبيق موبايل": 4,
            "اندرويد": 3,
            "أندرويد": 3,
            "ايفون": 3,
        },
    },

    "design": {
        "core": {
            "ui designer": 6,
            "ux designer": 6,
            "figma": 5,
        },
        "supporting": {
            "تصميم واجهات": 3,
            "واجهة مستخدم": 3,
            "واجهة أمامية": 3,
        },
        # NOTE: bare "ui" / "ux" are deliberately excluded (see
        # NOISE_KEYWORDS) — they are far too ambiguous to score, and
        # were a direct source of false negatives on Excel/Power BI
        # dashboard jobs that legitimately discuss "the UI of the report".
    },

    "education": {
        "core": {
            "teach": 8,
            "teaching": 8,
            "teacher": 8,
            "tutor": 8,
            "tutoring": 8,
            "مدرب": 7,
            "تعليم": 7,
            "تعليمي": 7,
            "معلم": 7,
            "مدرس": 7,
        },
        "supporting": {
            "mentor": 4,
            "mentoring": 4,
            "training": 4,
            "coach": 3,
            "coaching": 3,
            "course": 4,
            "courses": 4,
            "curriculum": 4,
            "syllabus": 4,
            "lesson": 3,
            "lessons": 3,
            "learning": 3,
            "learn": 3,
            "student": 3,
            "students": 3,
            "assignment": 2,
            "assignments": 2,
            "تدريب": 4,
            "تعلم": 3,
            "شرح": 3,
            "دورة": 4,
            "دورات": 4,
            "كورس": 4,
            "طالب": 3,
            "طلاب": 3,
            "منصة تعليمية": 3,
        },
    },

    "enterprise": {
        "core": {
            "erp": 8,
            "crm": 8,
            "wms": 8,
            "saas": 6,
        },
        "supporting": {
            "mvp": 3,
            "software development": 4,
            "admin panel": 4,
            "control panel": 3,
            "admin dashboard": 4,
            "user management": 4,
            "hrm": 5,
            "pos": 4,
            "inventory": 3,
            "inventory management": 5,
            "checkout": 3,
            "payment gateway": 4,
            "shopping cart": 4,
            "product catalog": 3,
            "تطوير منصة": 4,
            "ساس": 4,
            "منصة إلكترونية": 4,
            "منصة الكترونية": 4,
            "منصة حجز": 4,
            "مشروع ناشئ": 3,
            "شركة ناشئة": 3,
            "نظام سحابي": 3,
            "لوحة تحكم إدارية": 4,
            "لوحة إدارة": 4,
            "لوحة تحكم للمشرف": 4,
            "لوحة المشرف": 4,
            "لوحة الأدمن": 4,
            "لوحة المدير": 3,
            "إدارة المستخدمين": 4,
        },
    },

    "nocode": {
        "core": {
            "base44": 8,
            "bubble.io": 7,
            "bubble": 6,
        },
        "supporting": {
            "no-code": 4,
            "nocode": 4,
            "low-code": 4,
            "glide": 3,
            "adalo": 3,
            "بدون كود": 4,
            "بدون برمجة": 4,
            "لو كود": 4,
            "منخفض الكود": 4,
            "نوكود": 4,
        },
    },

    "ai_apps": {
        "core": {},
        "supporting": {
            "llm": 2,
            "chatbot": 3,
            "rag": 2,
            "openai api": 2,
            "gemini api": 2,
        },
    },

    "devops": {
        "core": {},
        "supporting": {
            "docker": 2,
            "kubernetes": 3,
            "nginx": 2,
            "linux server": 2,
            "سيرفر": 2,
            "استضافة": 2,
            "رفع الموقع": 2,
        },
    },
}


# ------------------------------------------------------------------
# HARD_REJECT_KEYWORDS — unrelated to the tier model. If present AND
# there is no positive match at all, the job is rejected outright
# regardless of everything else (see filters.py).
# ------------------------------------------------------------------

HARD_REJECT_KEYWORDS = {
    "graphic design",
    "logo",
    "photoshop",
    "illustrator",
    "video editing",
    "motion graphics",
    "translation",
    "seo",
    "digital marketing",
    "internship",
    "intern",
    "volunteer",
    "unpaid",
    "تدريب صيفي",
    "متدرب",
    "متطوع",
    "بدون مقابل",
}


# ------------------------------------------------------------------
# NOISE_KEYWORDS — explicitly unscored. These words are too generic /
# overloaded to be evidence for either polarity. They must NEVER be
# added to POSITIVE_KEYWORDS or NEGATIVE_KEYWORDS; the assertion below
# enforces that at import time so this can't silently regress.
#
# This is the direct fix for the reported bug: legitimate Data Analysis
# jobs that mention "system", "platform", "dashboard app", "API", "users",
# or "UI" were being penalized as if they were software-engineering jobs.
# ------------------------------------------------------------------

NOISE_KEYWORDS = {
    "system", "systems",
    "platform", "platforms",
    "api",
    "app", "apps", "application", "applications",
    "website", "web app", "webapp",
    "user", "users",
    "ui", "ux",
    "login", "signup", "session",
    "oauth", "jwt",
    "roles", "permissions",
    "نظام", "أنظمة",
    "منصة", "منصات",
    "تطبيق", "تطبيقات", "برنامج", "برامج", "برمجية",
    "موقع", "مواقع",
    "متجر", "متاجر",
    "واجهة", "تكامل", "التوثيق",
    "جوال", "موبايل",
    "المستخدمين",
    "صلاحيات",
}


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
