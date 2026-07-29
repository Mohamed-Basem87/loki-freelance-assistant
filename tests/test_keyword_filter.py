from dotenv import load_dotenv

load_dotenv()

from app.filters import keyword_filter
from app.gemini import evaluate_job


TEST_CASES = [
    {
        "name": "Excel Data Cleaning",
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
        "text": """
Need a modern company logo.

Adobe Illustrator.
Photoshop.
Brand identity.
""",
    },
    {
        "name": "Arabic Excel Cleaning",
        "text": """
عندي ملف اكسل فيه داتا.

محتاج تنظيف البيانات
وترتيب البيانات
وتجهيزها للتحليل.
""",
    },
]


for test in TEST_CASES:

    print("=" * 80)
    print(test["name"])
    print("=" * 80)

    filter_result = keyword_filter(test["text"])

    print(f"Score               : {filter_result['score']}")
    print(f"Categories          : {filter_result['categories']}")
    print(f"Soft Penalty        : {filter_result['total_soft_penalty']}")
    print(f"Dangerous Tech      : {filter_result['has_dangerous_tech']}")
    print(f"Hard Reject         : {filter_result['hard_reject']}")
    print(f"Notify Directly     : {filter_result['notify_directly']}")
    print(f"Needs Gemini        : {filter_result['needs_gemini']}")

    print("\nPositive Matches")

    if filter_result["positive_matches"]:
        for match in filter_result["positive_matches"]:
            print(
                f"  +{match['weight']:>2} "
                f"{match['keyword']} "
                f"({match['category']})"
            )
    else:
        print("  None")

    print("\nSoft Negatives")

    if filter_result["soft_negative_matches"]:
        for match in filter_result["soft_negative_matches"]:
            print(
                f"  {match['weight']:>3} "
                f"{match['keyword']}"
            )
    else:
        print("  None")

    if filter_result["needs_gemini"]:

        print("\nRunning Gemini...")
        print("-" * 80)

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

    print()

