from app.filters import keyword_filter
from app.llm.groq import evaluate_job

text = """
Need a Power BI dashboard built from an Excel sales dataset.
The dashboard should include KPIs, charts, slicers,
and DAX measures.
"""

# Derived from the real keyword_filter() output -- see
# tests/test_llm_gemini.py for why hand-building this dict against a
# stale schema was the root cause of a production bug.
filter_result = keyword_filter(text, title="Power BI Dashboard Needed")

try:
    result = evaluate_job(text, filter_result)
    print(result)

except Exception as e:
    # No working Groq credentials in this environment.
    print(f"(skipped: no working LLM credentials -- {e})")
