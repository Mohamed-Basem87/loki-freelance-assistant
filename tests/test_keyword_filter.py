"""
Classifier regression suite.

This file intentionally imports ONLY app.filters (and, transitively,
app.keywords/app.normalize) -- none of which touch app.config -- so
it can be collected and run with a bare `pytest`, with no .env file
and no Telegram/Gemini/Groq/FreeHub credentials of any kind. A
previous version of this file additionally imported
app.llm.gemini.evaluate_job (unused by this file's own test cases,
apparently a leftover from before the file was split), which
transitively imports app.config and made the single most important
regression suite in the repository impossible to even collect without
a full production .env. See tests/test_llm_gemini.py for the
(properly isolated, mocked) tests that actually exercise the Gemini
call path.
"""

import pytest

from app.filters import keyword_filter


# ------------------------------------------------------------------
# Smoke cases: no fixed "expected decision" (several of these are
# deliberately mixed/ambiguous), used to check keyword_filter() always
# returns a well-formed, internally consistent result for a broad
# variety of real-world-shaped postings, plus one explicit regression
# assertion for the hard-reject bypass bug (see below).
# ------------------------------------------------------------------

SMOKE_CASES = [
    {
        "name": "Excel Data Cleaning",
        "title": "Excel Data Cleaning Needed",
        "text": """
Need someone to clean an Excel sheet.

Tasks:
- Remove duplicates
- Organize the spreadsheet
- Prepare the data for analysis
""",
    },
    {
        "name": "Power BI Dashboard",
        "title": "Power BI Dashboard Needed",
        "text": """
Need a Power BI dashboard built from sales data.

Requirements:
- KPIs
- DAX measures
- Drillthrough
- Executive dashboard
- Sales analysis
""",
    },
    {
        "name": "Portfolio Website",
        "title": "Personal Portfolio Website",
        "text": """
Need a personal portfolio website.

HTML
CSS
Bootstrap

Responsive.

5 pages.
""",
    },
    {
        "name": "Landing Page (HTML/CSS)",
        "title": "Responsive Landing Page",
        "text": """
Need a responsive landing page.

HTML
CSS
Bootstrap

Fast delivery.
""",
    },
    {
        "name": "Landing Page (React)",
        "title": "Responsive Landing Page - React",
        "text": """
Need a responsive landing page.

React
Next.js
Tailwind
Authentication
""",
    },
    {
        "name": "ERP System",
        "title": "Complete ERP System",
        "text": """
Need a complete ERP system.

Authentication
Inventory
Warehouse
Invoices
Customers
Reports
Dashboard

Python backend.
""",
    },
    {
        "name": "Flutter Mobile App",
        "title": "Flutter Application",
        "text": """
Need a Flutter application.

Authentication
Firebase
Android
iOS
""",
    },
    {
        "name": "Logo Design",
        "title": "Modern Company Logo",
        "text": """
Need a modern company logo.

Adobe Illustrator.
Photoshop.
Brand identity.
""",
    },
    {
        "name": "Arabic Excel Cleaning",
        "title": "تنظيف بيانات اكسل",
        "text": """
عندي ملف اكسل فيه داتا.

محتاج تنظيف البيانات
وترتيب البيانات
وتجهيزها للتحليل.
""",
    },
]


@pytest.mark.parametrize(
    "case", SMOKE_CASES, ids=[c["name"] for c in SMOKE_CASES]
)
def test_keyword_filter_returns_well_formed_result(case):
    """
    Broad smoke coverage: whatever the classifier decides for these
    (deliberately varied, some ambiguous) postings, the result dict
    must always be internally consistent -- this is real regression
    value beyond "it didn't crash", since it directly encodes the
    decision-table invariants documented in app/filters.py.
    """
    result = keyword_filter(case["text"], title=case["title"])

    assert result["decision"] in {"reject", "notify_directly", "needs_gemini"}
    assert result["notify_directly"] == (result["decision"] == "notify_directly")
    assert result["needs_gemini"] == (result["decision"] == "needs_gemini")

    # `matched` only asserts "some positive evidence existed somewhere"
    # -- it does not imply the decision was in the job's favor (see
    # app/job_processor.py's fallthrough branch) -- but a
    # notify_directly decision must always have some positive
    # evidence backing it.
    if result["notify_directly"]:
        assert result["matched"] is True

    # Hard reject always wins, regardless of any positive evidence.
    if result["hard_reject"]:
        assert result["decision"] == "reject"


def test_hard_reject_wins_over_positive_keywords():
    """
    Regression case for the hard-reject bypass bug: a hard-reject
    keyword ("unpaid"/"internship") alongside genuine positive
    keywords used to silently disable the reject entirely.
    """
    result = keyword_filter(
        """
Unpaid internship opportunity.

Help us build Power BI dashboards and Excel reports.
No pay, but great learning experience!
""",
        title="Unpaid Internship - Power BI",
    )

    assert result["hard_reject"] is True, (
        "Hard reject must fire even when positive keywords are also present"
    )
    assert result["decision"] == "reject"


# ------------------------------------------------------------------
# Regression checks for the automation / data-entry vocabulary patch
# (daily audit 2026-08-07). All of these previously auto-notified
# (`notify_directly` / core_positive_clean); they must now route to
# Gemini, never to a blind notification.
# ------------------------------------------------------------------
AUTOMATION_CASES = [
    {
        "name": "Data entry job (mixed core)",
        "title": "Excel Data Entry from Online",
        "text": (
            "I need someone to do data entry from online sources into an "
            "Excel file. Accuracy matters."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Browser automation tool",
        "title": "Web Form Auto-Filler Tool",
        "text": (
            "I need a browser automation tool using Playwright or Selenium "
            "that reads an Excel file (xlsx) and fills web forms."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Digital twin energy build",
        "title": "Building Digital Twin Energy Optimization",
        "text": (
            "I need a digital twin connected to BIM (Revit) files, running "
            "EnergyPlus simulations, with a Power BI dashboard of KPIs."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Generative AI RAG build",
        "title": "Capstone Web Design Project",
        "text": (
            "Build a Generative AI system with semantic search over a vector "
            "database and document ingestion from Excel files."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Lone-core scheduler app (threshold 4 -> 5)",
        "title": "Cross-Platform Production Scheduler App",
        "text": (
            "I run a production business and I need a dedicated desktop "
            "application in MS Excel that lets us schedule production. "
            "Produce simple status reports and a dashboard that I can "
            "export to PDF or Excel."
        ),
        "expected": "needs_gemini",
    },
]

# ------------------------------------------------------------------
# Regression checks for the scraping / content-creator patch
# (daily audit 2026-08-07). These auto-notified on a lone excel /
# excel-workbook core hit inside an otherwise scraping or lead-gen
# posting; the automation-core "scraper/scraping/web scraping" words
# and the new marketing-core content-creator/follower words must route
# them to Gemini (mixed core signals), never to a blind notification.
# ------------------------------------------------------------------
SCRAPE_LEADGEN_CASES = [
    {
        "name": "Resume scraper (real job 40633076)",
        "title": "Las Vegas Resume Scraper",
        "text": (
            # Mirrors production filter_text: the title is repeated at the
            # top of the body, so the "scraper" core negative is visible to
            # the body-level match and flips the lone "excel workbook" core
            # positive into mixed_core_signals.
            "Las Vegas Resume Scraper. "
            "I need a robust script that can automatically collect every "
            "publicly available resume for Las Vegas candidates on Indeed, "
            "LinkedIn, Glassdoor. For each profile the scrape should capture "
            "name, phone, email. Deliverables: a clean CSV file and an "
            "identical Excel workbook containing all scraped records."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Property scraper (real job 40627138)",
        "title": "Weekly Daft.ie MyHome Scraper",
        "text": (
            "Weekly scraping of Daft.ie and MyHome listings into an Excel "
            "file using Selenium."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Arabic content-creator database (real job 6787)",
        "title": "قاعدة بيانات لصناع محتوى عرب في مجال الطعام والمطاعم",
        "text": (
            "مطلوب بناء قاعدة بيانات منظمة تضم 500 صانع محتوى عربي على "
            "الأقل في مجال المأكولات والمطاعم. عدد المتابعين أكثر من 100000 "
            "متابع. طريقة التسليم ملف Excel منظم وقابل للفرز والتصفية."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Excel PDF job must still notify",
        "title": "Batch Extract PDF Tables to Excel",
        "text": (
            "I have a PDF packed with tables of numerical data and I need "
            "all of them moved into a single tidy Excel workbook ready for "
            "pivot tables and calculations. Values preserved as numbers for "
            "formulas."
        ),
        "expected": "notify_directly",
    },
]

# ------------------------------------------------------------------
# Regression checks for the Arabic software bug-fixing / maintenance
# patch (daily audit 2026-08-07). مستقل job 46233 ("إصلاح أخطاء لوحة
# تحكم نظام تدريبي (ربط إكسل وصلاحيات)") auto-notified on a lone title
# "اكسل" hit (title_core_positive) even though the gig is an
# admin-panel bug-fix for a training system. Core negatives
# "إصلاح أخطاء" / "مبرمج محترف" now route it to Gemini.
# ------------------------------------------------------------------
ARABIC_MAINTENANCE_CASES = [
    {
        "name": "Arabic admin-panel bug fix (real job 46233)",
        "title": "إصلاح أخطاء لوحة تحكم نظام تدريبي (ربط إكسل وصلاحيات)",
        "text": (
            # Mirrors production filter_text: title is repeated at the top
            # of the body, so has_core_positive stays True (اكسل) and the
            # mixed core signals route to Gemini instead of a bare reject.
            "إصلاح أخطاء لوحة تحكم نظام تدريبي (ربط إكسل وصلاحيات). "
            "لدي نظام إلكتروني قائم ومبني لإدارة البرامج التدريبية، وأحتاج إلى "
            "مبرمج محترف لعمل جلسة أونلاين (معاينة شاشة) وحل بعض المشاكل التقنية. "
            "وسأزودك بملف الكود الأخير لتبدأ منه مباشرة وتصلح الأخطاء مع "
            "الحفاظ على استقرار البيانات."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Arabic Excel job must not be lost",
        "title": "محلل بيانات اكسل وباور بي",
        "text": (
            "محلل بيانات اكسل وباور بي. "
            "مطلوب محلل بيانات محترف لتنظيف البيانات وإنشاء داشبورد باور بي "
            "وتقارير مبيعات من ملفات اكسل."
        ),
        "expected": "notify_directly",
    },
]

# ------------------------------------------------------------------
# Regression checks for the "data extraction" transcription patch
# (daily audit 2026-08-10). "PDF Numerical Data Extraction"
# (freelancer:40637391) and "PDF Table Data Extraction"
# (freelancer:40633712) both auto-notified on a lone "excel" core hit
# because the existing verbatim phrases ("extract pdf tables",
# "pdf tables", "extract tables", "extract pdf text", "data to excel")
# never matched the generic "data extraction" wording. The automation-
# core "data extraction" keyword now routes them to Gemini (mixed core
# signals) instead of a blind notification. No Arabic equivalent was
# added: the observed Arabic transcription class is already covered by
# the core negatives "نقل البيانات"/"نقل بيانات" (مستقل 6796) and
# "ادخال بيانات"/"إدخال بيانات".
# ------------------------------------------------------------------
DATA_EXTRACTION_CASES = [
    {
        "name": "PDF Numerical Data Extraction (real job 40637391)",
        "title": "PDF Numerical Data Extraction",
        "text": (
            # Mirrors production filter_text: title repeated at the top of
            # the body, so the "data extraction" core negative is visible to
            # the body-level match and flips the lone "excel" core positive
            # into mixed_core_signals.
            "PDF Numerical Data Extraction. "
            "I have a collection of PDF documents that contain numbers "
            "scattered across tables, embedded charts, and interactive form "
            "fields. I need every one of those figures captured accurately "
            "and transferred into a structured spreadsheet or database of "
            "your choice, Excel or Google Sheets is fine."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "PDF Table Data Extraction (real job 40633712)",
        "title": "PDF Table Data Extraction",
        "text": (
            "PDF Table Data Extraction. "
            "I need the tables inside a set of PDFs extracted into a clean "
            "Excel spreadsheet, matching the original layout exactly."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine analysis job without data-extraction wording must still notify",
        "title": "Sales Data Dashboard & Tutorial",
        "text": (
            "Raw sales data in CSV and Excel files. Consolidate every source "
            "in Excel with Power Query, publish the cleaned table to Power BI "
            "and build an interactive dashboard with DAX measures and "
            "customer segmentation."
        ),
        "expected": "notify_directly",
    },
]

# ------------------------------------------------------------------
# Regression checks for the 2026-08-11 daily window (OCR / generic
# transcription / PDF-table-singular / test-automation / Power Apps
# vocabulary). Five production false positives auto-notified in the
# window; the new automation/nocode core negatives route each to Gemini
# (mixed core signals or title_positive_but_body_core_negative) instead
# of a blind notification. Genuine DA jobs are protected from overreach.
# ------------------------------------------------------------------
WINDOW_2026_08_11_CASES = [
    {
        "name": "OCR-to-Excel app build (real job 40637697)",
        "title": "Automated JPEG to Excel Converter",
        "text": (
            "Automated JPEG to Excel Converter. "
            "I need a lightweight desktop app (Python + Tesseract OCR, "
            "OpenCV) that watches a directory, extracts the visible text "
            "and numbers from each JPEG, and writes them to an .xlsx file "
            "in real time with a live trend chart."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Paper forms transcription (real job 40638054)",
        "title": "Convert Paper Forms to Spreadsheet",
        "text": (
            "Convert Paper Forms to Spreadsheet. "
            "I have 50 to 200 paper forms that must be transcribed into a "
            "clean, well-structured Excel spreadsheet, one row per form."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "PDF table extraction singular (real job 40638101)",
        "title": "Accurate PDF Table Extraction",
        "text": (
            "Accurate PDF Table Extraction. "
            "I have PDFs filled with mixed text-and-number tables that I "
            "need moved into Excel. Every heading, font style and merged "
            "cell must look the same as the source; nothing can be lost "
            "or rearranged."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Test-automation framework (real job 40638944)",
        "title": "Cloud VM Pricing Validation Platform",
        "text": (
            "Cloud Pricing Validation & Test Automation Framework. "
            "End-to-end Python automation framework to scrape and validate "
            "cloud pricing data. Selenium UI automation (POM, PyTest), API "
            "validation, SQL-based checks, CSV/Excel summaries and "
            "dashboards to monitor pricing changes and regression results."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Power Apps / Power Automate low-code dev (real job 40639220)",
        "title": "Microsoft Power Platform Developer Needed",
        "text": (
            "Microsoft Power Platform Developer Needed. "
            "I need an experienced developer to turn manual business "
            "processes into streamlined low-code solutions -- Power Apps, "
            "Power Automate, Power BI, or any blend -- building and "
            "deploying the apps and flows end-to-end."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Power BI dashboard must still notify",
        "title": "Power BI Attrition Dashboard Automation",
        "text": (
            "I have a single Excel/CSV master file of every employee "
            "movement and I need a dynamic Power BI dashboard around it. "
            "PBIX with a tidy data model, DAX measures for headcount, "
            "attrition rate and hires/exits, automated refresh from a "
            "fixed folder path."
        ),
        "expected": "notify_directly",
    },
    {
        "name": "Genuine Excel cleanup job must still notify",
        "title": "Excel Expert Needed for Bulk Data Processing, Formatting, and Cleanup",
        "text": (
            "I have several very large spreadsheets that need to be "
            "audited, cleaned and uniformly formatted. Thousands of rows, "
            "using VLOOKUP and XLOOKUP to reconcile records, highlight "
            "mismatches and verify totals."
        ),
        "expected": "notify_directly",
    },
]

# ------------------------------------------------------------------
# QA / test-role regression checks (daily audit 2026-08-11).
# ------------------------------------------------------------------
QA_2026_08_11_CASES = [
    {
        "name": "Manual QA tester job misusing 'excel' deliverable (real job 40639807)",
        "title": "QA Specialist for Android E-commerce App",
        "text": (
            "We are looking for an experienced individual QA Tester to "
            "comprehensively test our Android e-commerce application. "
            "Functional testing, UI/UX testing, navigation and user flow "
            "testing, Login/Signup & OTP testing, Checkout & Payment flow "
            "testing. Deliverables: Complete QA report in Excel/Google "
            "Sheets. Automation testing experience with Appium, Espresso, "
            "Selenium, or similar tools is a plus."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine data pipeline mentioning QA must still notify",
        "title": "Sports Data Pipeline Extension - NFL, MLB & College Football",
        "text": (
            "Extend an existing sports-data pipeline for NFL, MLB and "
            "College Football. Existing validation, source-ledger, "
            "grading and packaging logic. Validation / QA scripts. "
            "Output updated Excel and CSV files. Python-based data "
            "pipelines, historical datasets and reproducible datasets."
        ),
        "expected": "notify_directly",
    },
]


# ------------------------------------------------------------------
# Regression checks for the 2026-08-12 daily window (trading-system
# builds / academic-scientific writing / Selenium test automation /
# Dynamics 365 Business Central ERP). Four production false positives
# auto-notified in the window, each independently confirmed by the
# NotificationGuard LLM (do_not_notify); the new core negatives route
# each to Gemini (mixed core signals) instead of a blind notification.
# Genuine DA jobs are protected from overreach.
# ------------------------------------------------------------------
WINDOW_2026_08_12_CASES = [
    {
        "name": "Automated trading system build (real job 40641402)",
        "title": "AI-Driven Automated Trading System Development",
        "text": (
            # Mirrors production filter_text: title repeated at the top of
            # the body, so the trading core negatives are visible to the
            # body-level match and flip the lone "data analysis" core
            # positive into mixed_core_signals.
            "AI-Driven Automated Trading System Development. "
            "AI Trading Engineer / Algorithmic Trading Developer for "
            "Automated Financial Markets System. Build and continuously "
            "improve an AI-powered automated trading system for financial "
            "markets. Algorithmic and quantitative trading, risk and money "
            "management, Python and software development, APIs and broker "
            "integrations, AI/LLMs, backtesting and strategy optimization, "
            "cloud infrastructure. Perform data analysis on market data "
            "and identify potential opportunities."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Scientific manuscript writing (real job 40640761)",
        "title": "Scientific Article: Essential-Oil Insecticides",
        "text": (
            # "data analysis" (core positive) appears alongside the new
            # writing-core negatives (scientific article / manuscript /
            # peer reviewed) -> mixed_core_signals -> Gemini.
            "Scientific Article: Essential-Oil Insecticides. "
            "I have completed a study and now need it shaped into a "
            "publishable scientific article. Craft a full manuscript and "
            "deliver the finished work as a clean, submission-ready PDF. "
            "The article must read like a peer-reviewed paper: abstract, "
            "introduction, methods, results and discussion. Methodology, "
            "experimental results and data analysis developed in depth so "
            "reviewers can replicate and critique the work."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Selenium test-automation framework (real job 40641595)",
        "title": "Azure Pricing Validation Automation",
        "text": (
            # Selenium promoted to core: "excel" core positive + selenium
            # core negative -> mixed_core_signals -> Gemini.
            "Azure Pricing Validation Automation. "
            "Self-contained Python framework that automatically scrapes, "
            "validates, and continuously monitors Azure pricing. Selenium "
            "(Page Object Model) drives the browser, REST pricing endpoint "
            "comparisons, SQLite storage, tests run under PyTest, finish "
            "with an HTML report and a CSV/Excel summary for finance."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Dynamics 365 Business Central ERP (real job 40642473)",
        "title": "Business Central Custom Reports & UI",
        "text": (
            # "power bi" core positive + business central / dynamics 365
            # core negatives -> mixed_core_signals -> Gemini.
            "Business Central Custom Reports & UI. "
            "Microsoft Dynamics 365 Business Central technical consultant. "
            "Custom reporting layouts (AL/RDLC or Power BI) plus UI "
            "enhancements, compiled and signed AL extensions, deployment "
            "notes and rollback instructions."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Power BI dashboard must still notify",
        "title": "Uber Ride Booking & Revenue Analysis – Power BI",
        "text": (
            # No core negative present; title-level Power BI/DAX hits keep
            # this as an automatic notification (real job 40641589).
            "Interactive Power BI dashboard analyzing ride-booking "
            "performance, customer behavior and revenue trends. Power Query "
            "data cleaning, DAX measures and KPIs, slicers, an overview "
            "page of completed bookings and revenue, business intelligence "
            "visualizations and comparative analysis."
        ),
        "expected": "notify_directly",
    },
]


# ------------------------------------------------------------------
# Regression checks for the 2026-08-13 daily window (ERP data-entry /
# address-entry / WhatsApp-bot vocabulary). Three production false
# positives auto-notified in the window, each independently confirmed
# by the NotificationGuard LLM (do_not_notify); the new core negatives
# ("dolibarr" enterprise, "address entry" automation, "whatsapp"/
# "واتساب" ai_apps) route each to Gemini (mixed core signals) instead
# of a blind notification. A WhatsApp-bot job with no core positive now
# rejects directly (previously wasted a Gemini call only to be
# rejected). Genuine DA jobs are protected from overreach.
# ------------------------------------------------------------------
WINDOW_2026_08_13_CASES = [
    {
        "name": "Dolibarr ERP product entry (real job 40643125)",
        "title": "Dolibarr Product Entry (50)",
        "text": (
            # Mirrors production filter_text: title repeated at the top of
            # the body, so the "dolibarr" enterprise core negative is visible
            # to the body-level match and flips the lone "excel" core
            # positive into mixed_core_signals.
            "Dolibarr Product Entry (50). "
            "I need a detail-oriented freelancer to import between one and "
            "fifty new products into my Dolibarr instance. All product "
            "data--names and descriptive text only--are ready in tidy "
            "Excel/CSV files, so the task is mainly about copying the right "
            "fields into the right places and double-checking that each "
            "entry looks clean inside Dolibarr."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Excel Names & Address Entry (real job 40643386)",
        "title": "Excel Names & Address Entry",
        "text": (
            # Title core hits ("excel workbook"/"excel") + the "address
            # entry" automation core negative -> mixed_core_signals -> Gemini.
            "Excel Names & Address Entry. "
            "I need a tidy Microsoft Excel workbook with up to five "
            "separate sheets, each populated with text records that I will "
            "supply--every record includes a person or company name and a "
            "full mailing address. Transfer the information exactly as "
            "shown in the source documents, keep the spelling and line "
            "breaks intact, and place each field in its proper column so "
            "the file is instantly searchable and ready for mail-merge use."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "WhatsApp booking bot (real job 40643442)",
        "title": "Bot IA WhatsApp reservas con Sheets",
        "text": (
            # Lone "excel" body core positive + "whatsapp" ai_apps core
            # negative -> mixed_core_signals -> Gemini.
            "Bot IA WhatsApp reservas con Sheets. "
            "Busco implementar un flujo inteligente que permita tomar "
            "reservas via WhatsApp Business y llevar la informacion "
            "directamente a una hoja de calculo (Google Sheets o Excel "
            "online) en tiempo real. El bot debe reconocer la intencion del "
            "usuario, hacer las preguntas necesarias y registrar "
            "automaticamente nombre, fecha y numero de personas."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "WhatsApp info bot, no core positive (real job 40636356)",
        "title": "WhatsApp Food & Beverage Info Bot",
        "text": (
            # "whatsapp" core negative with no core positive ->
            # core_negative_no_core_positive -> reject. Production outcome
            # was already Rejected (Gemini rejected it); now no wasted LLM
            # call.
            "WhatsApp Food & Beverage Info Bot. "
            "I run a food-and-beverage store and want customers to receive "
            "instant answers on WhatsApp, day or night. 24/7 WhatsApp "
            "Business (or Twilio API) chatbot. Product data should flow in "
            "from my existing database. MySQL or PostgreSQL preferred."
        ),
        "expected": "reject",
    },
    {
        "name": "Genuine WhatsApp-adjacent analysis must not be lost",
        "title": "WhatsApp Marketing Data Analysis",
        "text": (
            # Core positive ("data analysis"/"excel"/"pivot table") +
            # "whatsapp" core negative -> mixed_core_signals -> Gemini, never
            # an outright reject.
            "WhatsApp Marketing Data Analysis. "
            "Export our WhatsApp Business chat history and build an Excel "
            "dashboard with pivot tables and DAX measures to analyze "
            "customer response times, conversion trends and sales analysis "
            "by region."
        ),
        "expected": "needs_gemini",
    },
]


WINDOW_2026_08_13_B_CASES = [
    {
        "name": "Custom Cash Flow & Budget Tool (real job 40628982)",
        "title": "Custom Cash Flow and Budget Tool",
        "text": (
            # supporting_positive_only: "cash flow"(4) + "budgets"(3) +
            # "forecasting"(3) + "dashboard"(2) => 12 >=
            # SUPPORTING_POSITIVE_MIN_FOR_GEMINI. Before the audit only 5
            # ("forecasting"+"dashboard") -> reject/insufficient_signal.
            "Custom Cash Flow and Budget Tool\n"
            "I'm seeking an experienced developer to create a comprehensive "
            "cash flow and budget tool tailored for both household and business "
            "use. I would like a dashboard/cover sheet that shows everything in "
            "a snapshot. Then separate tabs for each of our business entities "
            "and our household. \n\nEssential Features:\nHousehold:\n- Expense "
            "Tracking: Monitor and categorize household expenses.\n- Budget "
            "Planning: Set and manage budgets for various household "
            "categories.\n\nBusiness:\n- Expense Tracking: Record and categorize "
            "all business expenses.\n- Income Tracking: Monitor all business "
            "income sources.\n- Budget Forecasting: Predict future budgets based "
            "on income and expenses.\n\nIdeal Skills:\n- Proficiency in financial "
            "software development\n- Strong background in budgeting and financial "
            "planning\n- Experience with user-friendly interface design\n- "
            "Excellent problem-solving skills\n\nLooking for a professional who "
            "can deliver a reliable, intuitive, and efficient tool within budget. "
            "Please provide examples of similar work done."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "AI Business & Finance Automation (real job 40644207)",
        "title": "AI Business & Finance Automation",
        "text": (
            # supporting_positive_only: "budgets"(3) + "dashboard"(2) +
            # "reports"(1) + "python"(3) + "kpis"(3) => 12 >=
            # SUPPORTING_POSITIVE_MIN_FOR_GEMINI. Before the audit only 9 ->
            # reject/insufficient_signal.
            "AI Business & Finance Automation\n"
            "I need a single, robust AI solution that can step in as an "
            "all-round operator—running routine business management, enforcing "
            "solid financial control, and keeping my inbox under control. The "
            "most urgent pressure is on the business and finance side, so "
            "accurate numbers and smart insights have to come first, but the "
            "same system should also clear, sort, and respond to email with "
            "minimal oversight from me. \n\nYour job is to map my current "
            "workflows, suggest (or build) the right stack—whether that's "
            "OpenAI, LangChain, custom Python pipelines, Zapier integrations, "
            "or another toolset—and then deliver an integrated assistant able "
            "to: \n\u2022 Maintain rolling budgets and forecasts, updating them "
            "automatically as new data arrives\n\u2022 Track expenses in real time, "
            "spot variances, and feed reliable numbers straight into my "
            "books\n\u2022 Generate clear monthly and ad-hoc financial reports ready "
            "for investors and tax filing\n\u2022 Monitor the inbox, classify "
            "messages, draft replies in my tone of voice, and surface only the "
            "items that truly need my approval\n\u2022 Display the core business "
            "KPIs on one live dashboard and proactively recommend operational "
            "tweaks when trends shift \n\nThe solution is complete when I can "
            "open a single interface, see up-to-date KPIs, download an accurate "
            "P&L, and watch at least 80 % of incoming mail handled without "
            "manual edits. If you have shipped comparable autonomous agents "
            "before, outline the models, frameworks, and roll-out plan you'd "
            "use to get us from prototype to production."
        ),
        "expected": "needs_gemini",
    },
]


# ------------------------------------------------------------------
# Regression checks for the 2026-08-13 C daily window (Microsoft Word
# document work / online-survey deployment / Excel software support).
# Five production false positives auto-notified, each independently
# confirmed by the NotificationGuard LLM (do_not_notify); the new core
# negatives ("mail merge"/"microsoft word"/"word documents" automation,
# "online survey"/"survey creation"/"typeform"/"google forms" automation,
# "excel troubleshooting" automation) route each to Gemini (mixed core
# signals or title_positive_but_body_core_negative) instead of a blind
# notification. Genuine Excel DA/BI jobs are protected from overreach.
# ------------------------------------------------------------------
WINDOW_2026_08_13_C_CASES = [
    {
        "name": "Word mail-merge letter formatting (real job 40644542)",
        "title": "Letter Formatting with Mail Merge",
        "text": (
            # Title "mail merge" core negative + body "excel sheet"/"excel"
            # core positives -> mixed_core_signals -> Gemini.
            "Letter Formatting with Mail Merge. "
            "I have a Word document that pulls data from an Excel sheet and I "
            "need it professionally formatted. The letter uses a mail merge to "
            "pull client details from the Excel sheet into a Word template. I "
            "need the layout, fonts and spacing fixed before the final print."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Word & Excel data feed automation (real job 40643062)",
        "title": "Automated Word & Excel Data Feed",
        "text": (
            # Title "excel" core positive + body "word documents" core negative
            # -> title_positive_but_body_core_negative -> Gemini.
            "Automated Word & Excel Data Feed. "
            "I need a simple data-feed or template set so I can enter data once "
            "and have it flow automatically into Word documents and Excel "
            "workbooks. VBA macros, linked tables, or form fields are welcome. "
            "Word reports and Excel sheets should populate instantly."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Word paragraphs to Excel rows (real job 40637597)",
        "title": "Word Paragraphs to Excel Rows",
        "text": (
            # Title "excel workbook"/"excel" core positives + body
            # "word documents" core negative -> Gemini (transcription, not
            # analysis).
            "Word Paragraphs to Excel Rows. "
            "I have Microsoft Word documents and need every standalone "
            "paragraph placed into an Excel workbook, one paragraph per row. "
            "Plain text only. You may use VBA, Power Query, or a small Python "
            "script with python-docx and openpyxl."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Online survey creation & deployment (real job 40644573)",
        "title": "Online Survey Creation & Deployment",
        "text": (
            # Body "excel" core positive + "online survey"/"survey creation"/
            # "typeform"/"google forms" core negatives -> mixed_core_signals
            # -> Gemini.
            "Online Survey Creation & Deployment. "
            "I have a survey that needs to be taken fully online. Build the "
            "survey in Google Forms, SurveyMonkey, or Typeform, configure logic "
            "and branding, generate the public link, monitor response quality, "
            "and export the results to Excel or Google Sheets."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Excel software support ticket (real job 38666479)",
        "title": "Excel 365 Cell Color Issue",
        "text": (
            # Title "excel" core positive + body "excel troubleshooting" core
            # negative -> title_positive_but_body_core_negative -> Gemini.
            "Excel 365 Cell Color Issue. "
            "I'm having trouble with my Excel 365. I cannot get it to fill-in a "
            "cell with a different color using the Fill Color tool. I need "
            "someone who is skilled in Excel troubleshooting and can help me "
            "resolve this issue."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Excel data analysis support still notifies (40640051)",
        "title": "Excel Data Analysis Support",
        "text": (
            # No new core negative present; title/body data-analysis core hits
            # keep this as an automatic notification (real job 40640051).
            "Excel Data Analysis Support. "
            "Several operational datasets must be turned into decision-ready "
            "insights. Data processing with strong command of formulas, pivot "
            "tables, charts, Power Query, or macros. Import and merge CSV/XLSX "
            "files, remove duplicates, validate entries, build dynamic pivot "
            "tables and a refreshable dashboard."
        ),
        "expected": "notify_directly",
    },
    {
        "name": "Genuine Excel data cleanup still notifies (40644216)",
        "title": "Excel Data Cleanup & Organization",
        "text": (
            # Title "excel" core positive with no new negative -> still a
            # direct notification (real job 40644216).
            "Excel Data Cleanup & Organization. "
            "I need an experienced data analyst to clean and organize my Excel "
            "datasets: remove duplicate rows, standardize dates and formats, "
            "fix inconsistencies, and prepare the data for reporting and "
            "analysis."
        ),
        "expected": "notify_directly",
    },
    {
        "name": "Genuine e-commerce sales data collection still notifies (40644481)",
        "title": "E-Commerce Sales Data Collection",
        "text": (
            # Contains bare "troubleshooting" wording but NOT the precise
            # "excel troubleshooting" core negative, so the job still
            # notifies directly (real job 40644481).
            "E-Commerce Sales Data Collection. "
            "I need to collect and organize sales data from multiple "
            "e-commerce platforms into a single Excel workbook. Compile sales "
            "reports, join order exports, handle discrepancies, and prepare "
            "the dataset for sales analysis and forecasting. Some "
            "troubleshooting of the data pipeline is expected."
        ),
        "expected": "notify_directly",
    },
]


WINDOW_2026_08_14_CASES = [
    {
        "name": "PDF/Image to Excel conversion (real job 40645760)",
        "title": "PDF/Image to Excel Conversion",
        "text": (
            # Title "excel" core positive + body "image to excel" core
            # negative -> mixed_core_signals -> Gemini (transcription, not
            # analysis). "pdf to excel" alone cannot fire because the title
            # reads "pdf/image to excel" -- the "/" folds to a space.
            "PDF/Image to Excel Conversion. "
            "I need the contents of several PDFs and scanned images "
            "transferred into a clean Excel workbook. Each source contains a "
            "mix of narrative text and structured tables, and I want every "
            "element--headings, paragraphs, and rows--captured with 99-100% "
            "accuracy. No custom templates are required."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Excel skills assessment test creation (real job 40645850)",
        "title": "Excel Skills Assessment Test",
        "text": (
            # Title "excel" core positive + body "skills assessment" core
            # negative -> title_positive_but_body_core_negative -> Gemini
            # (test-writing gig, not analysis).
            "Excel Skills Assessment Test. "
            "I'm looking for a 10-minute Excel test to evaluate basic Excel "
            "proficiency for work. The test should consist solely of "
            "multiple-choice questions, focusing on basic formulas and "
            "functions. Ideal skills: proficiency in Microsoft Excel, "
            "knowledge of basic formulas and functions."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Map PIM data to Excel fields copy-paste (real job 40646120)",
        "title": "Map PIM Data to Specific Excel Files Fields",
        "text": (
            # Title "excel" core positive + body "copy and paste" core
            # negative -> title_positive_but_body_core_negative -> Gemini
            # (manual data transfer, not analysis).
            "Map PIM Data to Specific Excel Files Fields. "
            "I have a set of customer-supplied Excel templates that must be "
            "populated with the full breadth of information already stored in "
            "our PIM exports (regular CSV / XLS files). The job is "
            "straightforward: open the existing PIM flat files, locate the "
            "matching SKU, and copy every available specification exactly as "
            "it appears. This is a pure copy-and-paste exercise rather than "
            "creative writing or rewriting."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Senior Tekla detailer steel detailing (real job 40646275)",
        "title": "Senior Tekla Detailer Needed Monthly",
        "text": (
            # Body "excel" core positive + "tekla" cad core negative ->
            # mixed_core_signals -> Gemini (steel detailing, not analysis).
            "Senior Tekla Detailer Needed Monthly. "
            "I need a seasoned Tekla Structures professional who can own the "
            "full detailing workflow on a series of industrial projects. "
            "Produce GA drawings, assembly drawings, single-part sheets, and "
            "NC/DSTV files ready for the shop. Generate bolt and material "
            "lists plus BOM and bolt lists in Excel. We'll agree on a "
            "percentage release each milestone so cash flow stays predictable."
        ),
        "expected": "needs_gemini",
    },
    {
        "name": "Genuine Excel analysis with 'copy paste' wording still notifies (40640051)",
        "title": "Excel Data Analysis Support",
        "text": (
            # The added negative is precisely "copy and paste". The broader
            # "copy paste" spelling is intentionally NOT used because it would
            # also hit this guard-confirmed genuine DA job (40640051); it must
            # therefore keep notifying.
            "Excel Data Analysis Support. "
            "Several operational datasets must be turned into decision-ready "
            "insights. Data processing with strong command of formulas, pivot "
            "tables, charts, Power Query, or macros. Import and merge CSV/XLSX "
            "files, remove duplicates, validate entries, copy paste key fields "
            "between sheets, build dynamic pivot tables and a refreshable "
            "dashboard."
        ),
        "expected": "notify_directly",
    },
]


ALL_REGRESSION_CASES = (
    AUTOMATION_CASES
    + SCRAPE_LEADGEN_CASES
    + ARABIC_MAINTENANCE_CASES
    + DATA_EXTRACTION_CASES
    + WINDOW_2026_08_11_CASES
    + QA_2026_08_11_CASES
    + WINDOW_2026_08_12_CASES
    + WINDOW_2026_08_13_CASES
    + WINDOW_2026_08_13_B_CASES
    + WINDOW_2026_08_13_C_CASES
    + WINDOW_2026_08_14_CASES
)


@pytest.mark.parametrize(
    "case",
    ALL_REGRESSION_CASES,
    ids=[c["name"] for c in ALL_REGRESSION_CASES],
)
def test_classifier_regression_cases(case):
    result = keyword_filter(case["text"], title=case["title"])

    assert result["decision"] == case["expected"], (
        f"{case['name']}: expected {case['expected']}, "
        f"got {result['decision']} ({result['reason']})"
    )
