from pprint import pprint

from app.filters import keyword_filter

tests = [

    "Power BI Dashboard with Excel",

    "Looking for Data Analyst SQL",

    "Portfolio Website using WordPress",

    "Flutter Android application",

    "Power BI dashboard using WordPress",

    "Graphic Design Logo",

    "Need Portfolio Website HTML CSS",

    "Power BI + Python Automation",

    "Laravel Dashboard",

]

for i, test in enumerate(tests, start=1):
    print("=" * 80)
    print(f"TEST {i}")
    print("=" * 80)
    print(test)
    print()

    pprint(keyword_filter(test))
    print()
