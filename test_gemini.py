from dotenv import load_dotenv

load_dotenv()

from app.gemini import evaluate_job


TEST_CASES = [
    {
        "name": "Data Collection",
        "score": 30,
        "categories": ["excel"],
        "positive_matches": [{"keyword": "excel"}],
        "soft_negative_matches": [],
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
        "score": 65,
        "categories": ["power_bi", "data_analysis"],
        "positive_matches": [
            {"keyword": "power bi"},
            {"keyword": "dashboard"},
            {"keyword": "dax"},
        ],
        "soft_negative_matches": [],
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
        "score": 40,
        "categories": ["portfolio"],
        "positive_matches": [{"keyword": "portfolio website"}],
        "soft_negative_matches": [],
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
        "score": 55,
        "categories": ["python"],
        "positive_matches": [
            {"keyword": "python"},
            {"keyword": "dashboard"},
        ],
        "soft_negative_matches": [
            {"keyword": "erp"},
        ],
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
        "score": 70,
        "categories": ["power_bi"],
        "positive_matches": [
            {"keyword": "power bi"},
            {"keyword": "dashboard"},
        ],
        "soft_negative_matches": [],
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

    result = evaluate_job(
        test["text"],
        {
            "score": test["score"],
            "categories": test["categories"],
            "positive_matches": test["positive_matches"],
            "soft_negative_matches": test["soft_negative_matches"],
        },
    )

    print(f"Decision           : {result['decision']}")
    print(f"Confidence         : {result['confidence']}")
    print(f"Project Type       : {result['project_type']}")
    print(f"Primary Deliverable: {result['primary_deliverable']}")
    print(f"Skills             : {', '.join(result['skills_detected'])}")
    print()
    print("Reason")
    print("-" * 70)
    print(result["reason"])
    print()

