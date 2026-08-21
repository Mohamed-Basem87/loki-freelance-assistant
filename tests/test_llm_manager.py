"""
app.llm.manager tests.

Importing this module transitively imports app.llm.gemini and
app.llm.groq, which transitively import app.config -- so this file
relies on tests/conftest.py's environment defaults to import cleanly
without a real .env (see conftest.py's module docstring). It does NOT
make any real Gemini/Groq API call: app.llm.manager.gemini_evaluate
and app.llm.manager.groq_evaluate are monkeypatched in every test
below, so this is a genuine offline unit test of the fallback
orchestration logic itself, not a smoke test that happens to succeed
or fail depending on network/credentials (which is what this file
used to be).
"""

import pytest

from app.filters import keyword_filter
from app.categories.data_analysis.profile import PROFILE
from app.llm import manager


TEXT = """
Need a Power BI dashboard built from an Excel sales dataset.
The dashboard should include KPIs, charts, slicers,
and DAX measures.
"""

# Derived from the real keyword_filter() output, same as the previous
# version of this file -- hand-building this dict against a stale
# schema was the root cause of a real production bug (see
# app.llm.utils.build_prompt).
FILTER_RESULT = keyword_filter(TEXT, title="Power BI Dashboard Needed", profile=PROFILE)



def test_arbitration_builds_system_prompt_from_candidate_category_prompts(monkeypatch):
    captured = {}

    def fake_gemini(text, candidates, system_prompt):
        captured["system_prompt"] = system_prompt
        return {
            "selected_category": "data_analysis",
            "confidence": 90,
            "reason": "The primary deliverable is analytics.",
        }

    monkeypatch.setattr(manager, "gemini_arbitrate", fake_gemini)
    monkeypatch.setattr(manager, "groq_arbitrate", lambda *args: pytest.fail("Groq should not run"))

    candidates = [
        {
            "id": "data_analysis",
            "name": "Data Analysis",
            "description": "Analytics and BI.",
            "arbitration_context": "Primary deliverable is analysis.",
            "result": {"reason": "mixed", "categories": [], "negative_categories": []},
        },
        {
            "id": "backend",
            "name": "Backend Development",
            "description": "Backend services.",
            "arbitration_context": "Primary deliverable is backend work.",
            "result": {"reason": "mixed", "categories": [], "negative_categories": []},
        },
    ]

    result = manager.arbitrate_category("Build an analytics API", candidates)

    assert result["selected_category"] == "data_analysis"
    prompt = captured["system_prompt"]
    assert "CATEGORY: Data Analysis (data_analysis)" in prompt
    assert "CATEGORY: Backend Development (backend)" in prompt
    assert "Only accept projects that are genuinely centered on Data Analysis" in prompt
    assert "Only accept projects that are genuinely centered on Backend Development" in prompt


def test_gemini_success_short_circuits_groq(monkeypatch):
    calls = {"gemini": 0, "groq": 0}

    def fake_gemini(text, filter_result, system_prompt):
        calls["gemini"] += 1
        return {"decision": "accept", "reason": "looks like real BI work"}

    def fake_groq(text, filter_result, system_prompt):
        calls["groq"] += 1
        raise AssertionError("Groq must not be called when Gemini succeeds")

    monkeypatch.setattr(manager, "gemini_evaluate", fake_gemini)
    monkeypatch.setattr(manager, "groq_evaluate", fake_groq)

    result = manager.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "accept"
    assert calls == {"gemini": 1, "groq": 0}


def test_gemini_failure_falls_back_to_groq(monkeypatch):
    calls = {"gemini": 0, "groq": 0}

    def failing_gemini(text, filter_result, system_prompt):
        calls["gemini"] += 1
        raise RuntimeError("gemini exploded")

    def fake_groq(text, filter_result, system_prompt):
        calls["groq"] += 1
        return {"decision": "reject", "reason": "not actually BI work"}

    monkeypatch.setattr(manager, "gemini_evaluate", failing_gemini)
    monkeypatch.setattr(manager, "groq_evaluate", fake_groq)

    result = manager.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "reject"
    assert calls == {"gemini": 1, "groq": 1}


def test_both_providers_failing_raises_runtime_error_with_both_details(monkeypatch):
    def failing_gemini(text, filter_result, system_prompt):
        raise RuntimeError("gemini: quota exceeded")

    def failing_groq(text, filter_result, system_prompt):
        raise RuntimeError("groq: also down")

    monkeypatch.setattr(manager, "gemini_evaluate", failing_gemini)
    monkeypatch.setattr(manager, "groq_evaluate", failing_groq)

    with pytest.raises(RuntimeError) as exc_info:
        manager.evaluate_job(TEXT, FILTER_RESULT)

    # Both providers' failure details must survive into the final
    # error (this is what job_processor.py logs to the Errors sheet
    # under the "LLM" category) -- fail-closed with full context, not
    # a swallowed/generic error.
    message = str(exc_info.value)
    assert "gemini: quota exceeded" in message
    assert "groq: also down" in message
