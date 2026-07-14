import json

from google import genai

from app.config import GEMINI_API_KEY


client = genai.Client(api_key=GEMINI_API_KEY)


SYSTEM_PROMPT = """
You are an expert freelance job evaluator.

You are evaluating projects for Mohamed Basem.

=========================
PROFILE
=========================

Education:
- AI Student

Skills:
- Power BI
- Microsoft Excel
- SQL
- Python
- Data Analysis
- Data Cleaning
- Data Visualization
- Dashboards
- DAX
- Power Query
- ETL
- Business Intelligence
- Reporting
- KPI Development

Can also do:
- Portfolio Websites
- HTML
- CSS
- Bootstrap

Not interested in:
- Flutter
- Android
- iOS
- Mobile Development
- Graphic Design
- Logo Design
- Video Editing
- Social Media Management
- SEO
- Marketing

=========================
IMPORTANT
=========================

The keyword filter has already removed obvious spam and obvious irrelevant jobs.

You are ONLY reviewing borderline jobs.

Reject only if the project is clearly unrelated.

=========================
CONFIDENCE
=========================

95-100
Perfect match.

80-94
Strong match.

60-79
Possible match.

0-59
Reject.

=========================
OUTPUT
=========================

Respond ONLY with valid JSON.

{
    "decision": "accept" or "reject",
    "confidence": integer,
    "reason": "One short sentence.",
    "skills_detected": [
        "Skill 1",
        "Skill 2"
    ]
}

Do not include markdown.

Do not include explanations.

Only output JSON.
"""


def evaluate_job(text: str, filter_result: dict):

    prompt = f"""
Keyword Filter Result

Score:
{filter_result["score"]}

Categories:
{filter_result["categories"]}

Positive Matches:
{filter_result["positive_matches"]}

Negative Matches:
{filter_result["soft_negative_matches"]}

Job Description:

{text}
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=SYSTEM_PROMPT + "\n\n" + prompt,
    )

    raw = response.text.strip()

    if raw.startswith("```"):
        raw = raw.replace("```json", "")
        raw = raw.replace("```", "")
        raw = raw.strip()

    try:
        result = json.loads(raw)

    except Exception:

        result = {
            "decision": "reject",
            "confidence": 0,
            "reason": "Invalid Gemini response",
            "skills_detected": [],
        }

    return result
