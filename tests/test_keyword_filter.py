from dotenv import load_dotenv

load_dotenv()

from app.filters import keyword_filter
from app.llm.gemini import evaluate_job


TEST_CASES = [
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
    {
        # Regression case for the hard-reject bypass bug: a hard-reject
        # keyword ("unpaid"/"internship") alongside genuine positive
        # keywords used to silently disable the reject entirely.
        "name": "Unpaid Internship (should hard reject)",
        "title": "Unpaid Internship - Power BI",
        "text": """
Unpaid internship opportunity.

Help us build Power BI dashboards and Excel reports.
No pay, but great learning experience!
""",
    },
]


for test in TEST_CASES:

    print("=" * 80)
    print(test["name"])
    print("=" * 80)

    filter_result = keyword_filter(test["text"], title=test["title"])

    print(f"Decision            : {filter_result['decision']}")
    print(f"Reason              : {filter_result['reason']}")
    print(f"Categories          : {filter_result['categories']}")
    print(f"Negative Categories : {filter_result['negative_categories']}")
    print(f"Hard Reject         : {filter_result['hard_reject']}")
    print(f"Notify Directly     : {filter_result['notify_directly']}")
    print(f"Needs Gemini        : {filter_result['needs_gemini']}")
    print(f"Title Core Positive : {filter_result['title_core_positive']}")
    print(f"Title Core Negative : {filter_result['title_core_negative']}")

    print("\nCore Positive Matches")

    if filter_result["positive_core_matches"]:
        for match in filter_result["positive_core_matches"]:
            print(
                f"  +{match['weight']:>2} "
                f"{match['keyword']} "
                f"({match['category']})"
            )
    else:
        print("  None")

    print("\nSupporting Positive Matches")

    if filter_result["positive_supporting_matches"]:
        for match in filter_result["positive_supporting_matches"]:
            print(
                f"  +{match['weight']:>2} "
                f"{match['keyword']} "
                f"({match['category']})"
            )
    else:
        print("  None")

    print("\nHard Reject Matches")
    print(f"  {filter_result['hard_reject_matches'] or 'None'}")

    if filter_result["needs_gemini"]:

        print("\nRunning Gemini...")
        print("-" * 80)

        try:
            result = evaluate_job(
                test["text"],
                filter_result,
            )

            print(f"Decision           : {result['decision']}")
            print(f"Confidence         : {result['confidence']}")
            print(f"Project Type       : {result['project_type']}")
            print(f"Primary Deliverable: {result['primary_deliverable']}")

            if result["skills_detected"]:
                print(
                    "Skills             : "
                    + ", ".join(result["skills_detected"])
                )

            print("\nReason")
            print("-" * 80)
            print(result["reason"])

        except Exception as e:
            # No real Gemini/Groq credentials in this environment --
            # that's fine for this script's purpose (exercising
            # keyword_filter()); just note it instead of crashing.
            print(f"(skipped: no working LLM credentials -- {e})")

    print()


# Explicit regression assertion for H2 (hard-reject bypass): a
# positive keyword match must never silently disable a hard reject.
_unpaid_case = next(
    t for t in TEST_CASES if t["name"] == "Unpaid Internship (should hard reject)"
)
_unpaid_result = keyword_filter(_unpaid_case["text"], title=_unpaid_case["title"])
assert _unpaid_result["hard_reject"] is True, (
    "Hard reject must fire even when positive keywords are also present"
)
assert _unpaid_result["decision"] == "reject"

# ------------------------------------------------------------------
# Regression checks for the automation / data-entry vocabulary patch
# (daily audit 2026-08-07). All of these previously auto-notified
# (`notify_directly` / core_positive_clean); they must now route to
# Gemini, never to a blind notification.
# ------------------------------------------------------------------
_auto_cases = [
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

for _case in _auto_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

# ------------------------------------------------------------------
# Regression checks for the scraping / content-creator patch
# (daily audit 2026-08-07). These auto-notified on a lone excel /
# excel-workbook core hit inside an otherwise scraping or lead-gen
# posting; the automation-core "scraper/scraping/web scraping" words
# and the new marketing-core content-creator/follower words must route
# them to Gemini (mixed core signals), never to a blind notification.
# ------------------------------------------------------------------
_scrape_leadgen_cases = [
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

for _case in _scrape_leadgen_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

print("All scraping/content-creator regression checks passed.")

# ------------------------------------------------------------------
# Regression checks for the Arabic software bug-fixing / maintenance
# patch (daily audit 2026-08-07). مستقل job 46233 ("إصلاح أخطاء لوحة
# تحكم نظام تدريبي (ربط إكسل وصلاحيات)") auto-notified on a lone title
# "اكسل" hit (title_core_positive) even though the gig is an
# admin-panel bug-fix for a training system. Core negatives
# "إصلاح أخطاء" / "مبرمج محترف" now route it to Gemini.
# ------------------------------------------------------------------
_arabic_maintenance_cases = [
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

for _case in _arabic_maintenance_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

print("All Arabic maintenance regression checks passed.")

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
_data_extraction_cases = [
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

for _case in _data_extraction_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

print("All data-extraction transcription regression checks passed.")

# ------------------------------------------------------------------
# Regression checks for the 2026-08-11 daily window (OCR / generic
# transcription / PDF-table-singular / test-automation / Power Apps
# vocabulary). Five production false positives auto-notified in the
# window; the new automation/nocode core negatives route each to Gemini
# (mixed core signals or title_positive_but_body_core_negative) instead
# of a blind notification. Genuine DA jobs are protected from overreach.
# ------------------------------------------------------------------
_win_2026_08_11_cases = [
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
_qa_2026_08_11_cases = [
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

for _case in _qa_2026_08_11_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

for _case in _win_2026_08_11_cases:
    _res = keyword_filter(_case["text"], title=_case["title"])
    assert _res["decision"] == _case["expected"], (
        f"{_case['name']}: expected {_case['expected']}, "
        f"got {_res['decision']} ({_res['reason']})"
    )

print("All QA/test-role regression checks passed.")
print("All 2026-08-11 daily-window regression checks passed.")
print("All automation/data-entry regression checks passed.")
print("All keyword_filter regression checks passed.")
