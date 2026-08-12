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


ALL_REGRESSION_CASES = (
    AUTOMATION_CASES
    + SCRAPE_LEADGEN_CASES
    + ARABIC_MAINTENANCE_CASES
    + DATA_EXTRACTION_CASES
    + WINDOW_2026_08_11_CASES
    + QA_2026_08_11_CASES
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
