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

print("All keyword_filter regression checks passed.")
