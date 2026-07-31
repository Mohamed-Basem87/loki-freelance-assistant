from dotenv import load_dotenv

load_dotenv()

from app.filters import keyword_filter
from app.llm.manager import evaluate_job


# filter_result is now derived from the real keyword_filter() output
# instead of a hand-built dict -- hand-building it previously used a
# stale schema (score/positive_matches/soft_negative_matches) that
# doesn't match what keyword_filter() actually returns, which is
# exactly the kind of drift that broke build_prompt() in production
# (see app.llm.utils.build_prompt).
TEST_CASES = [
    {
        "name": "Data Collection",
        "title": "Data Collection - Real Estate Companies",
        "text": """
Collect data for 1000 Saudi real estate companies.

Deliver an Excel sheet containing:
- Company name
- Website
- Email
- Phone
- CEO
- Address

Manual work is acceptable but automation is preferred.
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
        "name": "SaaS Platform",
        "title": "SaaS Platform with Power BI Reporting",
        "text": """
Build a SaaS platform.

Includes:

- Authentication
- Payments
- Admin Panel
- User Management

Power BI dashboard for reporting.
""",
    },
]


for test in TEST_CASES:

    print("=" * 70)
    print(test["name"])
    print("=" * 70)

    filter_result = keyword_filter(test["text"], title=test["title"])

    print(f"(keyword_filter decision: {filter_result['decision']}, "
          f"needs_gemini={filter_result['needs_gemini']})")

    try:
        result = evaluate_job(test["text"], filter_result)

        print(f"Decision           : {result['decision']}")
        print(f"Confidence         : {result['confidence']}")
        print(f"Project Type       : {result['project_type']}")
        print(f"Primary Deliverable: {result['primary_deliverable']}")
        print(f"Skills             : {', '.join(result['skills_detected'])}")
        print()
        print("Reason")
        print("-" * 70)
        print(result["reason"])

    except Exception as e:
        # No working Gemini/Groq credentials in this environment --
        # that's fine here, this script's purpose is to exercise
        # build_prompt()/keyword_filter() against the real schema,
        # not to reach a live model.
        print(f"(skipped: no working LLM credentials -- {e})")

    print()
