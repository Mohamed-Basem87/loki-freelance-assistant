from app.llm.manager import evaluate_job

filter_result = {
    "score": 95,
    "categories": ["power_bi"],
    "positive_matches": ["power bi", "dashboard", "excel"],
    "soft_negative_matches": [],
}

text = """
Need a Power BI dashboard built from an Excel sales dataset.
The dashboard should include KPIs, charts, slicers,
and DAX measures.
"""

try:
    result = evaluate_job(text, filter_result)

    print("\n=== RESULT ===")
    print(result)

except Exception as e:
    print(f"\nTest failed: {e}")