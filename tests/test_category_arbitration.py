import json

import pytest

from app.llm import manager
from app.llm.utils import parse_arbitration_response


def _candidates():
    return [
        {
            "id": "data_analysis",
            "name": "Data Analysis",
            "description": "Analytics and BI.",
            "arbitration_context": "Primary deliverable is analysis or BI.",
            "result": {"reason": "mixed signals", "categories": ["power_bi"], "negative_categories": []},
        },
        {
            "id": "web_development",
            "name": "Web Development",
            "description": "Web applications.",
            "arbitration_context": "Primary deliverable is a web application.",
            "result": {"reason": "mixed signals", "categories": ["react"], "negative_categories": []},
        },
    ]


def test_arbitration_uses_one_provider_call(monkeypatch):
    calls = {"gemini": 0, "groq": 0}

    def fake_gemini(text, candidates, system_prompt):
        calls["gemini"] += 1
        assert {c["id"] for c in candidates} == {"data_analysis", "web_development"}
        return {"selected_category": "web_development", "confidence": 90, "reason": "The primary deliverable is a web application."}

    def fail_groq(*args):
        calls["groq"] += 1
        raise AssertionError("Groq should not be called after Gemini succeeds")

    monkeypatch.setattr(manager, "gemini_arbitrate", fake_gemini)
    monkeypatch.setattr(manager, "groq_arbitrate", fail_groq)

    result = manager.arbitrate_category("Build a React app", _candidates(), "system")

    assert result["selected_category"] == "web_development"
    assert calls == {"gemini": 1, "groq": 0}


def test_groq_fallback_gets_compact_prompt_and_truncated_text(monkeypatch):
    captured = {}

    def failing_gemini(text, candidates, system_prompt):
        raise RuntimeError("quota exceeded")

    def fake_groq(text, candidates, system_prompt):
        captured["text"] = text
        captured["system_prompt"] = system_prompt
        return {"selected_category": "none", "confidence": 60, "reason": "no fit"}

    monkeypatch.setattr(manager, "gemini_arbitrate", failing_gemini)
    monkeypatch.setattr(manager, "groq_arbitrate", fake_groq)

    # Real category ids: building the primary full-depth policy imports
    # each candidate's llm_prompt module before Gemini is even called.
    candidates = [
        {
            "id": "data_analysis",
            "name": "Data Analysis",
            "description": "Analytics and BI.",
            "arbitration_context": "Primary deliverable is analysis or BI.",
            "result": {"reason": "mixed signals", "categories": ["power_bi"], "negative_categories": []},
        },
        {
            "id": "backend",
            "name": "Backend Development",
            "description": "Backend services.",
            "arbitration_context": "Primary deliverable is backend work.",
            "result": {"reason": "mixed signals", "categories": ["api"], "negative_categories": []},
        },
    ]

    long_tail = " filler sentence." * 500
    result = manager.arbitrate_category(
        "Build a Power BI dashboard." + long_tail,
        candidates,
    )

    assert result["selected_category"] == "none"

    # The fallback system prompt must carry every candidate's short scope
    # summary and none of the full-depth llm_prompt policies -- Groq's
    # tokens-per-minute cap rejects oversized requests before inference.
    prompt = captured["system_prompt"]
    assert "SCOPE: Primary deliverable is analysis or BI." in prompt
    assert "SCOPE: Primary deliverable is backend work." in prompt
    assert "Only accept projects that are genuinely centered on" not in prompt

    # The fallback job text must be truncated with an explicit marker.
    assert captured["text"].startswith("Build a Power BI dashboard.")
    assert captured["text"].endswith("[Job description truncated]")
    assert len(captured["text"]) < len("Build a Power BI dashboard." + long_tail)


@pytest.mark.parametrize("selected", ["unknown", "data_analysis,web_development"])
def test_arbitration_rejects_invalid_or_multiple_category_values(selected):
    raw = json.dumps({
        "selected_category": selected,
        "confidence": 90,
        "reason": "test",
    })
    with pytest.raises(ValueError):
        parse_arbitration_response(raw, {"data_analysis", "web_development"})


def test_arbitration_accepts_none():
    raw = json.dumps({
        "selected_category": "none",
        "confidence": 70,
        "reason": "No candidate is the primary deliverable.",
    })
    result = parse_arbitration_response(raw, {"data_analysis", "web_development"})
    assert result["selected_category"] == "none"
