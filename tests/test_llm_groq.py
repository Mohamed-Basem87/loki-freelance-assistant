from app.llm.groq import evaluate_job

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

result = evaluate_job(text, filter_result)

print(result)