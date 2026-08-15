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
            # Weight audit: bare "dashboard" (singular) was weak (7/32,
            # 22% precision) -- it fires just as often on admin/SaaS/CRM
            # dashboards as on BI dashboards. "dashboards" (plural) was
            # notably better (7/13, 54%) and left unchanged rather than
            # conflating the two. "لوحة تحكم" mirrored the singular
            # pattern (0/4) so it was lowered to match.
            "dashboard": 2,
            "dashboards": 4,
            "interactive dashboard": 5,
            "measure": 3,
            "measures": 3,
            "calculated column": 3,
            "star schema": 6,
            "data model": 5,
            "data modeling": 5,
            "لوحة تحكم": 2,
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
            "pivot tables": 8,
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
            # Weight audit (876-job batch, ground-truth-corrected):
            # workbook 95% precision (18/19), sheets 91% (10/11),
            # spreadsheet 83% (10/12), google sheets 86% (6/7) -- all were
            # sitting at generic-supporting weight (2-3) despite being
            # strong, reliable signals. Increased accordingly. "sheet"
            # (singular) was weaker (69%, 11/16) so only bumped modestly;
            # left noticeably below "sheets" (plural) given the gap.
            "spreadsheet": 4,
            "workbook": 4,
            "worksheet": 2,
            "sheet": 3,
            "sheets": 4,
            "xlsx": 4,
            "xls": 3,
            "شيت": 3,
            "شيتات": 3,
            "pivot": 2,
            "pivots": 2,
            "pivot views": 3,
            "lookup": 2,
            "remove duplicates": 3,
            "conditional formatting": 3,
            "financial model": 6,
            "financial modeling": 6,
            "google sheets": 4,
        },
    },

    "sql": {
        # Weight audit (876-job batch, ground-truth-corrected): every
        # specific DBMS product name here scored 0% precision as a lone
        # core-positive signal -- mysql 0/18, postgresql 0/18,
        # postgres 0/3, sqlite 0/3, oracle 0/2, "sql server" 0/4, all
        # false positives were DB-admin/backend/ERP jobs that happened to
        # name a database engine, not analysis work. Bare "sql" itself
        # was only slightly better (2/16, 12%). "Core" is supposed to
        # mean "unambiguous on its own" -- none of these meet that bar,
        # so all are demoted to supporting. The new negative-keyword
        # additions elsewhere in this file (DB recovery, access control,
        # netsuite, etc.) are the intended catch for the DBA/ERP cases;
        # this demotion is the complementary fix on the positive side.
        # "stored procedure" and "قاعدة بيانات" (Arabic "database") had
        # no comparable evidence of being unambiguous either -- "قاعدة
        # بيانات" was in fact 0/2 -- so both are demoted too, matching
        # how bare "database" was already (correctly) treated as
        # supporting rather than core.
        "core": {},
        "supporting": {
            "sql": 5,
            "sql server": 5,
            "mysql": 4,
            "postgresql": 4,
            "postgres": 4,
            "sqlite": 4,
            "oracle": 4,
            "stored procedure": 4,
            "قاعدة بيانات": 4,
            "database": 2,
            # "query" (singular) was a strong, underweighted signal
            # (9/11, 82% precision) -- increased. "queries" (plural) was
            # the opposite (1/7, 14%) -- left as-is rather than raised;
            # flagging the asymmetry rather than averaging the two away.
            "query": 4,
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
            # "analysis" (bare) was reasonably strong (17/24, 71%) and
            # underweighted -- increased. "analytics" was closer to a
            # coinflip (4/11, 36%) despite a higher weight -- decreased.
            "analytics": 2,
            "analysis": 3,
            "sales analysis": 5,
            "marketing analysis": 4,
            "market analysis": 4,
            "customer analysis": 4,
            "hr analysis": 4,
            "forecast": 3,
            "forecasting": 3,
            "budgets": 3,
            # Financial-reporting vocabulary. Repeated production evidence:
            # two genuine DA/BI-adjacent jobs ("AI Business & Finance
            # Automation" freelancer:40644207, daily audit 2026-08-13, and
            # "Custom Cash Flow and Budget Tool" freelancer:40628982) were
            # rejected at insufficient_signal with supporting-positive weight
            # 9, just under the 12 Gemini threshold, despite real budget/
            # forecast/reporting/KPI content. These phrases route such jobs
            # to Gemini for review. Full 3,506-job replay: only those two
            # decisions change (reject -> needs_gemini), no notification
            # changes, no regressions. Bare "budget"/"expenses" are NOT added
            # (too generic; "expenses" reintroduced the "Cross-Platform
            # Production Scheduler App" FP, and "budget" alone matched 172
            # non-DA jobs). No Arabic equivalent is added: no audited Arabic
            # posting used "الميزانيات"/"التدفق النقدي" in a data-analysis
            # context, so no production-supported Arabic terminology exists
            # to mirror these.
            "cash flow": 4,
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
            "data curation": 3,
            "data quality": 3,
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
            # "data entry" (Arabic equivalents) moved to the negative
            # "automation" category - six production data-entry jobs
            # auto-notified on a bare excel/dashboard mention; data
            # entry is clerical, not analysis (see NEGATIVE_KEYWORDS).
            "تنظيف الداتا": 4,
            "تجهيز الداتا": 3,
            "تنظيف": 2,
            "تنضيف": 2,
            "ينظف": 2,
            "تنقية": 2,
            "جودة البيانات": 3,
            "التحقق من صحة البيانات": 3,
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
            # "web scraping" / "scraping" were moved OUT of the positive
            # python list (see the automation category in NEGATIVE_KEYWORDS):
            # in production every scraping posting was a web-automation job,
            # not analysis work — treating scraping as positive evidence
            # helped false-positive notifications fire on scraper jobs.
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
            # DB administration/recovery: this bot's core-positive "sql"/
            # "mysql"/"postgresql" keywords are unavoidably ambiguous
            # between "analyze this data" and "administer this database".
            # These phrases only ever showed up (in the 876-job audit) on
            # DBA-flavored postings, e.g. "Repair & Restore MySQL Backup"
            # ("restore process keeps flagging the dump as corrupted") and
            # "PostgreSQL Database Development for QR Code ID System"
            # ("automatic backups, audit logging... access control
            # systems"). Deliberately core-tier (not supporting) so a job
            # with e.g. "mysql" + "backup restore" routes to
            # mixed_core_signals -> Gemini instead of blind auto-notify —
            # NOT an outright reject, since a job that's genuinely both
            # (rare, but possible) still deserves a human/LLM look.
            "database recovery": 6,
            "restore backup": 6,
            "backup restore": 6,
            "corrupted database": 6,
            "database dump": 5,
            "access control": 6,
            "audit logging": 6,
            "role-based access": 6,
            "encryption": 6,
            # Software bug-fixing / maintenance work. Both phrases verbatim
            # in مستقل job 46233 ("إصلاح أخطاء لوحة تحكم نظام تدريبي
            # (ربط إكسل وصلاحيات)" — an admin-panel bug-fix gig for a
            # training system) which auto-notified on a lone title "اكسل"
            # hit (title_core_positive) because no core-negative fired.
            # Deliberately core (not supporting): the title-positive rule
            # ignores supporting negatives entirely, so only a core
            # negative flips it to needs_gemini. A genuine DA job that
            # happens to mention the same words still carries its own core
            # positive, so it routes to mixed_core_signals -> Gemini rather
            # than an outright reject.
            "إصلاح أخطاء": 6,
            "مبرمج محترف": 7,
            # Custom-database application builds (multi-user data-entry
            # + report-generation software), not data analysis. "Custom
            # Sales Database Development" (freelancer:40648249, daily
            # audit 2026-08-15) auto-notified on a lone "excel" core hit
            # via core_positive_clean; the deliverable is a custom
            # database solution with user-friendly interfaces for
            # multiple users entering data and generating one-page
            # PDF/Excel/Label reports ("Experience in database design
            # and management"), i.e. a database-application build, not
            # analysis. The independent NotificationGuard LLM review
            # returned do_not_notify for the same job. "custom database"
            # and "database solution" are the verbatim production
            # phrases; both match exactly one job across the full
            # 4,084-job replay (40648249, notify_directly -> needs_gemini),
            # zero collateral on genuine DA/BI postings. The broader
            # class terms were checked and NOT added: "database design"
            # (29 jobs) and "database management" (15 jobs) already route
            # every other production occurrence to reject/needs_gemini,
            # but the phrases are generic enough to over-reach into
            # legitimate data-modeling/warehouse work, so only the two
            # precise verbatim phrases are used. No Arabic equivalent is
            # added: "قاعدة بيانات مخصصة"/"تطوير قاعدة بيانات" have zero
            # production occurrences, and the observed Arabic database
            # vocabulary ("قواعد البيانات", 11 jobs) is already covered
            # by the existing backend/enterprise negatives and never
            # auto-notifies.
            "custom database": 6,
            "database solution": 6,
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
            # Supporting (not core) deliberately: "user registration" /
            # "authentication" flows are common enough that a lone mention
            # shouldn't force a Gemini review by itself, only add to the
            # weight of evidence. Exact phrases below came verbatim from
            # "PHP E-commerce User Registration" ("fully-functioning user
            # registration module") and "Flask Web App With Auth" ("user
            # authentication (login, registration, session management...)").
            "user registration": 3,
            "registration module": 4,
            "user authentication": 3,
            "نظام تسجيل": 3,
            "تسجيل مستخدمين": 3,
            "إنشاء حساب": 3,
            # Arabic mirrors of the DB-recovery / access-control core
            # terms above. Same caveat as the enterprise-category Arabic
            # additions: analogous translations, not directly observed in
            # an Arabic posting in this batch, so kept at supporting
            # weight rather than core.
            "استعادة نسخة احتياطية": 4,
            "استرجاع قاعدة البيانات": 4,
            "نسخ احتياطي": 3,
            "التحكم بالصلاحيات": 4,
            "التشفير": 4,
            "الامتثال": 3,
            "سجل التدقيق": 4,
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
            # Weight audit: "learning" fired as a false alarm half the
            # time (4/8 were still genuinely good jobs, 50%) and "learn"
            # similarly (2/3, thin but consistent) -- almost certainly
            # matching inside "machine learning" / "deep learning" on
            # real ML/data-science postings, not education gigs. Lowered
            # rather than removed, since the education-tutoring reading
            # is still sometimes correct.
            "learning": 1,
            "learn": 1,
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
            # "Recruitment ORCLE NetSuite techno-functional" and "Oracle
            # Integration Cloud (OIC) Engineer" both matched only on the
            # sql-category "oracle" core-positive keyword; these are ERP
            # engineering roles, not database/analysis work. Core-tier so
            # "oracle" + "netsuite" routes to mixed_core_signals -> Gemini
            # rather than blind auto-notify.
            "netsuite": 7,
            "suitescript": 7,
            "suiteflow": 7,
            "erp integration": 6,
            # "Web Payroll & Android Tracker" ("end-to-end payroll
            # platform... automated salary calculation, tax filing") and
            # "Motorcycle Touring Billing Software Development" ("tour
            # billing and data entry software... replace my Excel
            # sheets") both matched only on a lone "excel" core-positive.
            "payroll platform": 6,
            "payroll system": 6,
            "billing software": 6,
            "billing system": 6,
            # Microsoft Dynamics 365 Business Central ERP customisation.
            # "Business Central Custom Reports & UI"
            # (freelancer:40642473, daily audit 2026-08-12) auto-notified on
            # a lone "power bi" core hit via core_positive_clean; the job is
            # Dynamics 365 Business Central consulting (compiled AL
            # extensions for custom RDLC/Power BI reports plus UI/UX page
            # tweaks), not data analysis. The independent NotificationGuard
            # LLM review returned do_not_notify for the same job. Core (not
            # supporting) so "power bi"/"excel" + these route to
            # mixed_core_signals -> Gemini instead of a blind notification,
            # mirroring the existing "erp"/"netsuite"/"crm" treatment. No
            # Arabic equivalent is added: no Arabic production posting in
            # this or prior audited batches used "بيزنس سنترال"/"داينمكس"
            # (zero matches), so no production-supported Arabic terminology
            # exists to mirror these.
            "business central": 7,
            "dynamics 365": 7,
            # Dolibarr -- an open-source ERP product name, mirroring the
            # existing "netsuite"/"business central" ERP-name treatment.
            # "Dolibarr Product Entry (50)" (freelancer:40643125, daily
            # audit 2026-08-13) auto-notified on a lone "excel" core hit
            # via core_positive_clean; the job is importing product name/
            # description records into a Dolibarr ERP instance ("copying
            # the right fields into the right places"), i.e. ERP data
            # entry, not data analysis. The independent NotificationGuard
            # LLM review returned do_not_notify for the same job. Core so
            # "excel" + "dolibarr" routes to mixed_core_signals -> Gemini
            # instead of a blind notification. No Arabic equivalent is
            # added: no Arabic production posting used "دوليبار" (zero
            # matches in the audited batches), so no production-supported
            # Arabic terminology exists to mirror it.
            "dolibarr": 7,
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
            # Weight audit: bare "inventory" false-alarmed 43% of the
            # time (3/7) -- "inventory analysis"/"inventory forecasting"
            # dashboards are a genuinely common, legitimate BI topic, so
            # the word alone isn't strong evidence of inventory-*software*
            # being built. "inventory management" (the fuller phrase)
            # wasn't shown to have the same problem and is left as-is.
            "inventory": 1,
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
            # Arabic mirrors of the netsuite/billing/payroll terms above.
            # Flagged distinctly in the writeup: unlike the English
            # entries, these weren't observed verbatim in an Arabic
            # posting in this batch (the 5 false positives that motivated
            # this whole group were all English-source jobs) — they're
            # analogous translations, added at a lower supporting weight
            # rather than core to reflect that lower confidence.
            "تكامل الأنظمة": 4,
            "نظام رواتب": 4,
            "منصة رواتب": 4,
            "برنامج فوترة": 4,
            "نظام فوترة": 4,
        },
    },

    "nocode": {
        "core": {
            "base44": 8,
            "bubble.io": 7,
            "bubble": 6,
            # "power apps" / "power automate" -- Microsoft's low-code app /
            # workflow-automation platforms (part of the Power Platform).
            # "Microsoft Power Platform Developer Needed"
            # (freelancer:40639220, daily audit 2026-08-11) auto-notified via
            # core_positive_clean on "power bi" + "excel" body hits, but the
            # job is turning business processes into low-code Power Apps /
            # Power Automate solutions ("create the working app(s), flow(s)").
            # The only pre-existing negative ("low-code", supporting weight 4)
            # was far below the downgrade threshold. Core (not supporting) so
            # any positive hit alongside them routes to mixed_core_signals ->
            # Gemini instead of a blind notification. Deliberately NOT
            # adding "power platform": it is genuinely ambiguous (Power BI is
            # part of the Power Platform and legitimate BI work legitimately
            # uses that name); "power apps"/"power automate" are unambiguous
            # app/flow development signals. No Arabic equivalent is added: no
            # Arabic production posting used "باور ابس"/"باور اوتومات";
            # the Arabic low-code class is already covered by the supporting
            # negatives "بدون كود"/"لو كود"/"نوكود".
            "power apps": 7,
            "power automate": 7,
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
        # Messaging-platform bot development. Repeated production evidence:
        # every job in the audited batches that mentions WhatsApp is a
        # chatbot / automation / integration gig, never data analysis --
        # "Bot IA WhatsApp reservas con Sheets" (freelancer:40643442,
        # daily audit 2026-08-13) auto-notified via core_positive_clean on
        # a lone "excel" body hit ("llevar la información ... a Google
        # Sheets o Excel online") when the deliverable is a WhatsApp
        # Business booking bot (Dialogflow/NLP, webhooks, Apps Script) --
        # the independent NotificationGuard LLM review returned
        # do_not_notify for the same job. Also in the same window (all
        # non-DA, all currently rejected on other grounds):
        # "Integración y Duplicación de Bots API" (40643193), "Recuperar
        # Mensajes WhatsApp iOS" (40643676), "Urgente GoHighLevel,
        # WhatsApp, Reservo" (40643994), "Reconectar agente IA WhatsApp
        # Business" (40644011), and مستقل 46413 (legal-services store that
        # delivers contracts "عبر الواتساب"). Core so a positive hit
        # alongside "whatsapp" routes to mixed_core_signals -> Gemini
        # instead of a blind notification. "واتساب" is the verbatim
        # Arabic spelling observed in مستقل 46413.
        "core": {
            "whatsapp": 7,
            "واتساب": 7,
        },
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

    "automation": {
        # Software-automation / app-build vocabulary. Repeated
        # production false positives were auto-notifying on a lone
        # "excel"/"power bi" mention inside an otherwise
        # software-build posting: Capstone 40626489, Web Form
        # Auto-Filler 40626655, Digital Twin 40629830, Facebook
        # tool 40630010, Emergent Data-Entry 40630975. These words
        # route them to Gemini (heavy-supporting-negative) instead
        # of a blind notification.
        "core": {
            # "data entry" is clerical, not analysis. Six distinct
            # production jobs auto-notified on bare excel/dashboard
            # wording: Mixed Data Entry & Email Support 40630261,
            # Excel Data Entry from Online 40630761, Accurate
            # Numerical Data Entry Task 40631312, Weekly Inventory
            # Lockbox Data Entry 40627538, Emergent Data-Entry App
            # Build 40630975, Web Form Auto-Filler 40626655. Core so
            # "excel" + "data entry" -> mixed_core_signals -> Gemini.
            # Arabic equivalents moved here from the positive
            # data_analysis list where they were wrongly scored +2.
            "data entry": 6,
            "ادخال بيانات": 6,
            "إدخال بيانات": 6,
            # "address entry" -- the title-wording variant of the data-entry
            # class. "Excel Names & Address Entry" (freelancer:40643386,
            # daily audit 2026-08-13) auto-notified on title core hits
            # ("excel workbook"/"excel") via title_core_positive; the job
            # is manually transferring name/address records into an Excel
            # workbook "exactly as shown in the source documents, keep the
            # spelling and line breaks intact ... ready for mail-merge",
            # i.e. clerical data entry, not analysis. The independent
            # NotificationGuard LLM review returned do_not_notify for the
            # same job. Core so "excel" + "address entry" routes to
            # mixed_core_signals -> Gemini instead of a blind notification,
            # mirroring the "data entry" treatment. No Arabic equivalent is
            # added: no audited Arabic posting used "إدخال عناوين" (the
            # Arabic data-entry class is already covered verbatim by the
            # core negatives "ادخال بيانات"/"إدخال بيانات").
            "address entry": 6,
            # PDF / text transcription into Excel: repeated production
            # false positives auto-notified on a lone "excel" mention
            # inside what is actually manual data-entry / transcription
            # work. All six observed postings describe copying text or
            # numbers from PDFs into a spreadsheet "exactly as it
            # appears" (manual entry, transcription error rate < 1%,
            # "no macros or automation are required"): "Organized PDF
            # Data Transfer to Excel" (40633590), "PDF Table Data
            # Extraction" (40633712), "PDF-to-Excel Text Transfer"
            # (40634627) and three distinct "Extract PDF Text into
            # Excel" postings (40628260, 40634186, 40634618). Core (not
            # supporting) so a core-positive hit alongside them routes
            # to mixed_core_signals -> Gemini instead of a blind
            # notification, mirroring the "data entry" treatment above.
            # Arabic "نقل البيانات" / "نقل بيانات" (data transfer) is
            # the verbatim equivalent observed in مستقل 6796 ("نقل
            # البيانات"); no Arabic "text transfer" or "extract PDF
            # text" wording was observed in the audited Arabic postings.
            "data transfer": 6,
            "text transfer": 6,
            "extract pdf text": 6,
            "نقل البيانات": 6,
            "نقل بيانات": 6,
            # web scraping / scraper vocabulary. In production every
            # scraping posting was a web-automation job, not analysis:
            # "Weekly Daft.ie MyHome Scraper" (40627138) and "Las Vegas
            # Resume Scraper" (40633076) both auto-notified on a lone
            # excel/excel-workbook mention inside an otherwise
            # scraping-automation posting. Deliberately CORE (not
            # supporting) so "scraper" + a core positive routes to
            # mixed_core_signals -> Gemini instead of a blind
            # notification; previously these words were wrongly scored
            # as POSITIVE python evidence.
            "web scraping": 6,
            "scraping": 6,
            "scraper": 6,
            "scrapers": 6,
            # PDF-table / spreadsheet transcription into Excel. A recurring
            # false-positive class: postings asking to copy tables or data
            # verbatim into Excel ("reproduce them faithfully", "no
            # calculations or interpretation") auto-notified on a lone
            # "excel"/"power query" mention via title_core_positive because
            # no core-negative fired. Verified across 2086 workbook jobs:
            # "Convert PDF Tables to Excel" (40627821, 40629954, 40630095),
            # "Batch Extract PDF Tables to Excel" (40631754), "PDF Tables to
            # Excel" (40632337), "Extract PDF Tables to Excel" (40635795),
            # "PDF Tables to Styled Excel" (40636342) all auto-notified and
            # all describe clerical extraction ("copy the text exactly as it
            # appears", "Mirrors all values exactly", "spot-checked against
            # the original PDFs"). "data to excel" (verbatim in "Etihad PNR
            # Data to Excel" 40635768) is the manual-entry variant. Core so
            # "excel" + these route to mixed_core_signals -> Gemini (which
            # has rejected every transcription job it has seen: 40636313,
            # 40635970, 40636103, 40636173) instead of a blind notification.
            "extract pdf tables": 6,
            "pdf tables": 5,
            # "pdf table" (singular) -- fills the word-order gap in the
            # transcription class. "Accurate PDF Table Extraction"
            # (freelancer:40638101, daily audit 2026-08-11) auto-notified on
            # a lone "excel" core hit because every existing transcription
            # negative requires the plural ("pdf tables") or a different word
            # order ("extract pdf tables"); the job is format-preserving
            # PDF->Excel transcription ("nothing can be lost or rearranged",
            # "must match exactly"), the same class Gemini has rejected
            # repeatedly. Core so "excel" + "pdf table" routes to
            # mixed_core_signals -> Gemini instead of a blind notification.
            "pdf table": 5,
            "tabular data": 5,
            "data to excel": 6,
            # Browser-extension / browser-automation development. Arabic
            # مستقل 46320 ("بناء إضافة متصفح ... تُخرج تقارير Excel و PDF")
            # auto-notified on a lone title "excel" hit — the job is a
            # Chrome-extension build (Manifest V3, content scripts, JS/DOM),
            # not data analysis. English equivalents cover the recurring
            # English extension-dev postings (all previously rejected on
            # other grounds: "Fix Chrome Extension Error" 40628693,
            # "Government Slot Booking Chrome Extension" 40628205, "Chrome
            # Extension Tester" 40636397, etc.). Core so a positive hit
            # alongside them routes to Gemini rather than a blind
            # notification.
            "browser extension": 7,
            "chrome extension": 7,
            "إضافة متصفح": 7,
            "إضافات كروم": 7,
            # OCR / image-to-text extraction. "Automated JPEG to Excel
            # Converter" (freelancer:40637697, daily audit 2026-08-11)
            # auto-notified on a lone "excel" core hit via title_core_positive;
            # the job is building a desktop OCR app (Tesseract, OpenCV) that
            # watches a folder, extracts text/numbers from JPEGs into xlsx and
            # keeps a live trend chart -- an automation-tool build, not data
            # analysis. OCR work is never analysis (it is capture/transcription
            # infrastructure), so it is deliberately core: "excel" + "ocr"
            # routes to Gemini rather than a blind notification.
            "ocr": 6,
            # PDF->Excel / CSV->spreadsheet CONVERSION phrases. This is the
            # remaining gap in the transcription class above ("data to excel",
            # "extract pdf tables", "pdf tables"): postings whose title/body
            # literally says "PDF to Excel" / "PDF-to-Excel" (hyphen folds to
            # a space) or "CSV to spreadsheet" still auto-notified because
            # none of the existing negatives matched the verbatim phrase.
            # Verified in the 996-job full production replay, all still
            # notify_directly: "Mixed PDF to Excel Conversion" (40627137,
            # "extract every value, label, and note exactly as written ...
            # solid manual entry skills are fine"), "Accurate PDF-to-Excel
            # Conversion" (40629071, "faithful layout replication rather than
            # analysis or reporting"), "Software Developer ... convert PDF
            # Bank Statements to Excel" (40630408, a one-click PDF-to-Excel
            # converter tool), "Accurate PDF Table Conversion" (40630113,
            # "must match the original documents exactly") and "CSV to
            # Spreadsheet Automation" (40630203, "transferring data from a
            # CSV file to a spreadsheet"). Core so "excel" + these route to
            # mixed_core_signals -> Gemini (never a blind notification).
            # Arabic equivalents are already covered: the verbatim Arabic
            # transcription wording "نقل البيانات"/"نقل بيانات" (data
            # transfer) is a core negative above; no Arabic "PDF to Excel"
            # wording was observed in any audited Arabic posting.
            "pdf to excel": 6,
            "pdf to excel conversion": 6,
            "pdf table conversion": 5,
            "extract tables": 5,
            "csv to spreadsheet": 6,
            # "data extraction" phrasing — the remaining gap in the
            # transcription class above. "PDF Table Data Extraction"
            # (40633712) and the daily-audit false positive "PDF Numerical
            # Data Extraction" (40637391) both auto-notified on a lone
            # "excel" core hit because none of the verbatim phrases above
            # ("extract pdf tables", "pdf tables", "extract tables",
            # "extract pdf text", "data to excel") match the generic
            # "data extraction" wording — the jobs are clerical
            # PDF/spreadsheet transcription, not analysis. Core so
            # "excel" + "data extraction" routes to mixed_core_signals ->
            # Gemini (which has rejected every transcription job it has
            # seen) instead of a blind notification. No Arabic equivalent
            # is added: no Arabic production posting used "استخراج
            # بيانات"; the observed Arabic transcription class is
            # covered verbatim by the existing core negatives "نقل
            # البيانات"/"نقل بيانات" (مستقل 6796) and "ادخال بيانات"/
            # "إدخال بيانات".
            "data extraction": 6,
            # "transcribe" / "transcription" / "transcribed" -- the generic
            # wording of the manual-transcription class, which the classifier
            # had only ever caught through specific verbatim phrases ("data to
            # excel", "extract pdf tables", ...). "Convert Paper Forms to
            # Spreadsheet" (freelancer:40638054, daily audit 2026-08-11)
            # auto-notified on a lone "excel file"/"excel" core hit; the job
            # is transcribing 50-200 handwritten paper forms into an Excel
            # spreadsheet -- clerical data entry, not analysis. Core so a
            # positive hit alongside these routes to mixed_core_signals ->
            # Gemini (which has rejected every transcription job it has seen).
            # No Arabic equivalent is added: the observed Arabic
            # transcription/data-entry class is already covered verbatim by
            # the core negatives "نقل البيانات"/"نقل بيانات" and
            # "ادخال بيانات"/"إدخال بيانات".
            "transcribe": 6,
            "transcription": 6,
            "transcribed": 6,
            # "test automation" -- QA/test-automation engineering, never data
            # analysis. "Cloud VM Pricing Validation Platform"
            # (freelancer:40638944, daily audit 2026-08-11) auto-notified on a
            # lone "excel" core hit (body "CSV/Excel summaries"); the deliverable
            # is an end-to-end Python test-automation framework (Selenium +
            # PyTest POM, API validation, CI/CD) where the CSV/Excel reports and
            # dashboards are test outputs. The only pre-existing negative
            # ("selenium", supporting weight 4) was far below the downgrade
            # threshold, so no review fired. Core so "excel" + "test automation"
            # routes to Gemini instead of a blind notification. No Arabic
            # equivalent is added: no Arabic production posting used
            # "اختبارات آلية"/"أتمتة الاختبارات"; the observed Arabic testing
            # work is app-testing gigs already caught by mobile/backend words.
            "test automation": 6,
            # Arabic "invoice template" creation is Excel document work, not
            # data analysis. مستقل job "إنشاء نموذج فاتورة على برنامج إكسل"
            # (daily audit 2026-08-11) auto-notified on a lone title "اكسل"
            # core hit (title_core_positive); the gig is building a $25-50
            # one-day Excel invoice template with a few extra formulas. The
            # independent NotificationGuard LLM review returned do_not_notify
            # for the same job. Core so "اكسل"/"excel" + "نموذج فاتورة"
            # routes to mixed_core_signals -> Gemini (which has rejected
            # every template/transcription job it has seen) instead of a
            # blind notification. No English equivalent is added: no audited
            # English posting asked to build an Excel invoice template (the
            # English invoice/billing jobs observed are covered verbatim by
            # the existing "billing software"/"billing system" and PDF->Excel
            # conversion negatives), and genuine Arabic analysis jobs
            # self-describe with "تحليل الفواتير" (invoice analysis), which
            # does not match this phrase.
            "نموذج فاتورة": 6,
            # Microsoft Word document work (formatting / automation /
            # transcription) that merely mentions Excel. A recurring false-
            # positive class observed across three production windows; every
            # posting auto-notified on a lone "excel"/"excel sheet" core hit
            # via title_core_positive or core_positive_clean because no
            # core-negative fired, while the actual deliverable is a Word
            # document, not analysis:
            #   - "Letter Formatting with Mail Merge" (freelancer:40644542,
            #     daily audit 2026-08-13): formatting a Word letter that
            #     pulls data from an Excel sheet via mail merge; the
            #     independent NotificationGuard LLM review returned
            #     do_not_notify.
            #   - "Automated Word & Excel Data Feed" (freelancer:40643062,
            #     prior-window guard review): builds a Word/Excel data-feed
            #     template set (VBA macros, linked tables, form fields) so
            #     Word reports and Excel sheets populate; guard returned
            #     do_not_notify.
            #   - "Word Paragraphs to Excel Rows" (freelancer:40637597):
            #     transcribes standalone paragraphs out of Word documents
            #     into an Excel workbook, one per row (VBA / Power Query /
            #     python-docx + openpyxl) -- transcription, not analysis.
            # Core (not supporting) so "excel" + these route to
            # mixed_core_signals -> Gemini instead of a blind notification.
            # Full 3605-job replay: "mail merge" matches 2 jobs (the single
            # notify_directly hit is 40644542), "microsoft word" matches 6
            # (the only notify_directly hits are 40644542/40637597),
            # "word documents" matches 3 (the only notify_directly hits are
            # 40637597/40643062) -- zero genuine DA/BI postings affected.
            # No Arabic equivalent is added: the observed Arabic Word-
            # document gig ("تنسيق احترافي لملفات Microsoft Word", مستقل
            # 6784) spells the tool as the Latin phrase "Microsoft Word",
            # already matched above; no audited Arabic posting used
            # "وورد"/"مستندات وورد".
            "mail merge": 6,
            "microsoft word": 6,
            "word documents": 6,
            # Online-survey building/deployment -- market-research operations
            # work, not data analysis. "Online Survey Creation & Deployment"
            # (freelancer:40644573, daily audit 2026-08-13) auto-notified on
            # a lone "excel" body core hit via core_positive_clean; the job
            # is building and deploying a survey on Google Forms / SurveyMonkey
            # / Typeform (configuring logic, required fields, branding,
            # generating the public link, monitoring response quality) and
            # only exporting the results to Excel at the end. The independent
            # NotificationGuard LLM review returned do_not_notify for the same
            # job. Core so "excel" + these route to mixed_core_signals ->
            # Gemini instead of a blind notification. Full 3605-job replay:
            # "online survey"/"survey creation" match only 40644573,
            # "typeform"/"google forms" each match 4 jobs of which the only
            # notify_directly hit is 40644573 (the rest already reject) --
            # zero genuine DA/BI postings affected. No Arabic equivalent is
            # added: the only Arabic survey postings observed are already
            # rejected and are not the survey-creation class ("برنامج تسجيل
            # بيانات استبيان ونموذج للطباعة" 46209 is an app build,
            # "توزيع استبيان علمي" 46274 is questionnaire distribution);
            # no audited Arabic posting described building an online survey.
            "online survey": 6,
            "survey creation": 6,
            "typeform": 6,
            "google forms": 6,
            # "excel troubleshooting" -- Excel software support requests, not
            # data analysis. "Excel 365 Cell Color Issue" (freelancer:38666479,
            # daily audit 2026-08-13) auto-notified via title_core_positive
            # on a lone title "excel" core hit; the job is a one-off tech-
            # support ticket ("I cannot get it to fill-in a cell with a
            # different color using the Fill Color tool ... someone who is
            # skilled in Excel troubleshooting"). The independent
            # NotificationGuard LLM review returned do_not_notify for the same
            # job. Deliberately the specific phrase "excel troubleshooting"
            # rather than bare "troubleshooting", which appears on 65 jobs
            # including genuine DA/BI work (e.g. "E-Commerce Sales Data
            # Collection" 40644481). Core so the title "excel" hit + this
            # body phrase route to title_positive_but_body_core_negative ->
            # Gemini instead of a blind notification. Full 3605-job replay:
            # "excel troubleshooting" matches exactly one job, 38666479.
            # No Arabic equivalent is added: no audited Arabic posting used
            # Excel-support wording ("دعم اكسل"/"مشكلة في اكسل"/"إصلاح
            # اكسل" all have zero production occurrences).
            "excel troubleshooting": 6,
            # Quantitative / algorithmic trading-system builds. "AI-Driven
            # Automated Trading System Development"
            # (freelancer:40641402, daily audit 2026-08-12) auto-notified on
            # a lone "data analysis" core hit via core_positive_clean; the
            # job is building a production automated-trading system (broker
            # APIs, cloud infrastructure, backtesting, AI/LLM-assisted
            # development), not data analysis. The independent
            # NotificationGuard LLM review returned do_not_notify for the
            # same job. Core (not supporting) so a positive hit alongside
            # these routes to mixed_core_signals -> Gemini instead of a
            # blind notification. The three sibling trading jobs in the same
            # window were already correctly rejected (Multi-Strategy Trading
            # App 40642754, Enhance & Integrate Trading App 40642803, Stock
            # Trading Web App Upgrade 40642845) and are unaffected by this
            # addition. No Arabic equivalent is added: zero Arabic postings
            # in the audited production data contain trading vocabulary
            # ("تداول"/"تداول آلي"/"خوارزمي"), so no production-supported
            # Arabic terminology exists to mirror these.
            "automated trading": 6,
            "algorithmic trading": 6,
            "quantitative trading": 6,
            # "selenium" -- promoted from supporting (weight 4) to core
            # (daily audit 2026-08-12). Selenium is unambiguous web-
            # automation/test vocabulary: in production it appears only on
            # QA/test-automation/scraping jobs (Azure Pricing Validation
            # Automation 40641595, Remote Web App QA Tester 40641615, AI
            # Agent for FMX Backup 40641659, Erase Amazon Order History
            # 40641411), never on a genuine DA/BI posting. 40641595
            # auto-notified on a lone "excel" core hit (CSV/Excel summary
            # deliverable) via core_positive_clean -- Selenium UI automation
            # (POM), API validation, PyTest, HTML/CSV/Excel test reports;
            # the pre-existing supporting weight 4 was far below the 14
            # downgrade threshold. The independent NotificationGuard LLM
            # review returned do_not_notify for the same job. Core so
            # "excel"/"sql" + "selenium" route to mixed_core_signals ->
            # Gemini instead of a blind notification, matching the "test
            # automation" core already added for the sibling class (Cloud VM
            # Pricing Validation Platform 40638944). No Arabic equivalent is
            # added: no audited Arabic posting used "سيلينيوم", so no
            # production-supported Arabic terminology exists to mirror it.
            "selenium": 6,
            # "image to excel" -- the verbatim gap in the PDF->Excel
            # transcription class. "PDF/Image to Excel Conversion"
            # (freelancer:40645760, daily audit 2026-08-14) auto-notified
            # via title_core_positive on a lone title "excel" hit; the job
            # is transcribing PDFs and scanned images into a clean Excel
            # workbook ("every element--headings, paragraphs, and rows--
            # captured with 99-100% accuracy", "no custom templates
            # required"). None of the existing transcription negatives
            # matched: "pdf to excel" cannot fire because the title reads
            # "pdf/image to excel" (the "/" folds to a space), the body
            # says "scanned images" rather than "ocr", and there is no
            # "data to excel"/"pdf tables" wording. The independent
            # NotificationGuard LLM review returned do_not_notify for the
            # same job. Core so "excel" + "image to excel" routes to
            # mixed_core_signals -> Gemini instead of a blind notification,
            # mirroring the "pdf to excel"/"data to excel" treatment.
            # Full 3,889-job replay: matches exactly one job, 40645760,
            # notify_directly -> needs_gemini, zero collateral. No Arabic
            # equivalent is added: no audited Arabic posting used "صورة إلى
            # إكسل"/"تحويل صورة" (zero matches); the observed Arabic
            # transcription class is covered verbatim by the core negatives
            # "نقل البيانات"/"نقل بيانات" and "ادخال بيانات"/"إدخال بيانات".
            "image to excel": 6,
            # "skills assessment" -- employee skill-test creation, not data
            # analysis. "Excel Skills Assessment Test" (freelancer:40645850,
            # daily audit 2026-08-14) auto-notified via title_core_positive
            # on a lone title "excel" hit; the job is writing a 10-minute
            # multiple-choice test to evaluate basic Excel proficiency, not
            # analyzing data. The independent NotificationGuard LLM review
            # returned do_not_notify for the same job. Core so "excel" +
            # "skills assessment" routes to title_positive_but_body_core_
            # negative -> Gemini instead of a blind notification. Full
            # 3,889-job replay: matches exactly one job, 40645850,
            # notify_directly -> needs_gemini, zero collateral. No Arabic
            # equivalent is added: no audited Arabic posting used "تقييم
            # المهارات"/"اختبار مهارات" (zero matches), so no
            # production-supported Arabic terminology exists to mirror it.
            "skills assessment": 6,
            # "copy and paste" -- the verbatim wording of manual copy-paste
            # data-transfer work, the clerical class distinct from analysis.
            # "Map PIM Data to Specific Excel Files Fields"
            # (freelancer:40646120, daily audit 2026-08-14) auto-notified
            # via title_core_positive on a lone title "excel" hit; the job
            # is copying SKU/product fields from PIM CSV/XLS exports into
            # customer Excel templates ("a pure copy-and-paste exercise
            # rather than creative writing or rewriting"). None of the
            # existing negatives matched ("data entry", "data transfer",
            # "data to excel"). The independent NotificationGuard LLM
            # review returned do_not_notify for the same job. Core so
            # "excel" + "copy and paste" routes to mixed_core_signals ->
            # Gemini instead of a blind notification. Full 3,889-job replay:
            # matches 6 jobs, of which the only notify_directly hit is
            # 40646120 (the rest already reject or need Gemini); zero
            # genuine DA/BI postings affected. The broad "copy paste"
            # spelling is deliberately NOT used -- it would also hit "Excel
            # Data Analysis Support" 40640051, a guard-confirmed genuine DA
            # job. No Arabic equivalent is added: no audited Arabic posting
            # used "نسخ ولصق" (zero matches); the observed Arabic data-entry
            # class is covered verbatim by the core negatives "ادخال بيانات"/
            # "إدخال بيانات".
            "copy and paste": 6,
        },
        "supporting": {
            # web-form / browser automation: verbatim in Web Form
            # Auto-Filler 40626655 and the Facebook tool 40630010.
            "browser automation": 4,
            "automation tool": 3,
            "desktop application": 4,
            "playwright": 4,
            # QA / software-testing role vocabulary. "QA Specialist for
            # Android E-commerce App" (freelancer:40639807, daily audit
            # 2026-08-11) auto-notified on a lone "excel" core hit -- the
            # deliverable was "Complete QA report in Excel/Google Sheets",
            # but the job is manual QA testing of a mobile e-commerce app,
            # not data analysis. The independent NotificationGuard LLM
            # review also returned do_not_notify for that job. The only
            # pre-existing negative ("selenium", 4) left the supporting-
            # negative total at 11, just below the 14 downgrade threshold,
            # so no review fired. Supporting (not core) so a lone "qa"
            # mention in a genuine data job (e.g. data-quality checks)
            # only adds weight toward a Gemini review instead of forcing
            # one; "tester"/"qa" pushed the offending job to 18 >= 14 ->
            # core_positive_but_heavy_supporting_negative -> Gemini.
            # No Arabic equivalent is added: no audited Arabic production
            # posting used QA/testing vocabulary (the Arabic jobs in this
            # and prior audited batches are web/mobile/ERP/marketing /
            # customer-service gigs), so no production-supported Arabic
            # terminology exists to mirror these.
            "qa": 4,
            "tester": 3,
            "electron": 3,
            # digital-twin / building-engineering terms: verbatim in
            # Digital Twin Energy Optimization 40629830.
            "digital twin": 4,
            "revit": 3,
            "bim": 3,
            "hvac": 3,
            "energyplus": 4,
            # generative-AI RAG system vocabulary: verbatim in
            # Capstone Web Design Project 40626489.
            "generative ai": 4,
            "semantic search": 4,
            "document ingestion": 4,
            "vector database": 4,
            "retrieval augmented generation": 5,
            "ai agent": 4,
            "ai agents": 4,
            # no-code app platform in Emergent Data-Entry App Build
            # 40630975.
            "emergent": 4,
        },
    },

    "cad": {
        # Mechanical / CAD / 3D-modelling engineering work. A recurring
        # production class where the deliverable is an Inventor/SolidWorks/
        # AutoCAD model or CNC shop drawing, not data analysis. The most
        # damaging case: "Lump Crusher Inventor 3D Modelling" (40637153,
        # daily audit 2026-08-10) auto-notified on a lone "excel" core hit
        # because "Structured BOM exported to Excel" was just a delivery
        # format; the rest of the body is "Autodesk Inventor entirely from
        # scratch ... native Inventor format ... Inventor assembly".
        # Every term below is verified against the 996-job production
        # replay: jobs containing them were NEVER notified (e.g. 19
        # solidworks jobs, 23 autocad jobs, 41 "3d" jobs, 12 fusion 360
        # jobs, 6 inventor jobs, 5 cnc jobs, 3 shop-drawings jobs — all
        # reject or needs_gemini). Core (not supporting) so "excel" +
        # any of these routes to mixed_core_signals -> Gemini instead of
        # a blind notification, mirroring the building-engineering terms
        # in the automation category. No Arabic equivalents are added: the
        # 111 Arabic postings in the audited dataset contain no CAD/3D
        # vocabulary (zero matches for ثلاثي الأبعاد / أوتوكاد /
        # سوليدوركس / ميكانيكي / نمذجة etc.) — Arabic production jobs
        # observed are web/mobile/ERP/marketing, so no production-supported
        # Arabic terminology exists to mirror these.
        "core": {
            "3d modelling": 7,
            "3d modeling": 7,
            "3d model": 6,
            "3d models": 6,
            "autodesk inventor": 8,
            "inventor assembly": 8,
            "inventor format": 8,
            "solidworks": 8,
            "autocad": 8,
            "fusion 360": 8,
            "cnc": 6,
            "shop drawings": 7,
            # Tekla (Structures) -- structural steel detailing software, in
            # the same unambiguous class as solidworks/autocad. "Senior
            # Tekla Detailer Needed Monthly" (freelancer:40646275, daily
            # audit 2026-08-14) auto-notified via core_positive_clean on a
            # lone body "excel" core hit ("BOM and bolt lists in Excel",
            # "single-part sheets", "cash flow") when the deliverable is
            # Tekla steel detailing (GA drawings, NC/DSTV files, bolt and
            # material lists). The independent NotificationGuard LLM review
            # returned do_not_notify for the same job. Core so "excel" +
            # "tekla" routes to mixed_core_signals -> Gemini instead of a
            # blind notification, mirroring the rest of the cad category.
            # Full 3,889-job replay: "tekla" matches 8 jobs; the only
            # notify_directly hit is 40646275 (the other steel-plan jobs
            # already route to Gemini), zero genuine DA/BI postings
            # affected. No Arabic equivalent is added: no audited Arabic
            # posting used "تيكلا" (zero matches), so no production-supported
            # Arabic terminology exists to mirror it.
            "tekla": 8,
        },
        "supporting": {},
    },

    "marketing": {
        # Influencer / content-creator lead generation. Building a
        # database of "content creators", "influencers" or "followers"
        # (with contact info) is lead-gen / data-collection work, not
        # data analysis. Auto-notified false positive: مستقل 6787
        # "قاعدة بيانات لصناع محتوى عرب في مجال الطعام والمطاعم" (a
        # content-creator contact database delivered as an Excel file)
        # fired on a lone "excel" core mention. Core so "excel" +
        # "صانع محتوى" routes to mixed_core_signals -> Gemini rather
        # than a blind notification. English equivalents are the direct
        # translations of the Arabic terms; both were observed only in
        # non-analysis production postings (content creators/influencer
        # partnerships, follower-growth gigs, etc.).
        "core": {
            "content creator": 6,
            "content creators": 6,
            "influencer": 6,
            "influencers": 6,
            "followers": 6,
            "صانع محتوى": 6,
            "صناع محتوى": 6,
            "صناع المحتوى": 6,
            "متابعين": 6,
        },
        "supporting": {},
    },

    "writing": {
        # Academic / scientific manuscript writing. A separate domain from
        # analysis: these postings ask for a finished article/paper, not for
        # data work. "Scientific Article: Essential-Oil Insecticides"
        # (freelancer:40640761, daily audit 2026-08-12) auto-notified on a
        # lone "data analysis" core hit (the phrase appears in the body
        # describing how to present the results) via core_positive_clean; the
        # deliverable is a submission-ready peer-reviewed scientific
        # manuscript (abstract, methods, results, discussion) written up from
        # research notes. The independent NotificationGuard LLM review
        # returned do_not_notify for the same job. Core (not supporting) so a
        # positive hit alongside these routes to mixed_core_signals -> Gemini
        # (which rejected the job) instead of a blind notification. "research
        # paper"/"academic paper" are deliberately supporting: they are less
        # unambiguous (a genuine data-analysis-for-research gig could mention
        # them), so a lone mention only adds weight toward a Gemini review.
        # No Arabic equivalent is added: no audited Arabic posting used
        # "كتابة أكاديمية"/"مقالة علمية" (zero matches; the only Arabic
        # "مقالة" posting was a logo-design article, already hard-rejected),
        # so no production-supported Arabic terminology exists to mirror these.
        "core": {
            "scientific article": 7,
            "academic writing": 7,
            "manuscript": 6,
            "peer reviewed": 6,
        },
        "supporting": {
            "research paper": 4,
            "academic paper": 4,
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
