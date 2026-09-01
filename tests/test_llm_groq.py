"""
app.llm.groq tests. Same pattern as test_llm_gemini.py: app.llm.groq.
CLIENT is monkeypatched with a fake object recording how it was
called, so the default (offline) test suite never makes a real Groq
API call.
"""

import json
import os

import pytest

from app.filters import keyword_filter
from app.categories.data_analysis.profile import PROFILE
from app.llm import groq
from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT


TEXT = """
Need a Power BI dashboard built from an Excel sales dataset.
The dashboard should include KPIs, charts, slicers,
and DAX measures.
"""

FILTER_RESULT = keyword_filter(TEXT, title="Power BI Dashboard Needed", profile=PROFILE)

_VALID_RESPONSE_JSON = json.dumps(
    {
        "decision": "accept",
        "confidence": 0.9,
        "project_type": "Business Intelligence",
        "primary_deliverable": "Power BI dashboard",
        "reason": "Primary deliverable is a BI dashboard with DAX measures.",
        "skills_detected": ["Power BI", "DAX"],
    }
)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, responses):
        # `responses` is a list of either a JSON string (success) or
        # an Exception instance (failure), consumed in call order --
        # models are tried in app.llm.groq.GROQ_MODELS order.
        self._responses = list(responses)
        self.calls = []

    def create(self, *, model, messages, response_format=None):
        self.calls.append(
            {"model": model, "messages": messages, "response_format": response_format}
        )
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeCompletionResponse(outcome)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = _FakeChat(self.completions)


def test_groq_request_has_system_and_user_roles(monkeypatch):
    fake_client = _FakeClient([_VALID_RESPONSE_JSON])
    monkeypatch.setattr(groq, "CLIENT", fake_client)

    result = groq.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "accept"
    assert len(fake_client.completions.calls) == 1

    call = fake_client.completions.calls[0]
    assert call["model"] == groq.GROQ_MODELS[0]

    roles = [m["role"] for m in call["messages"]]
    assert roles == ["system", "user"]
    assert call["messages"][0]["content"] == SYSTEM_PROMPT
    assert "Power BI dashboard" in call["messages"][1]["content"]


def test_groq_rotates_across_models_on_failure(monkeypatch):
    fake_client = _FakeClient(
        [
            RuntimeError("model 1 down"),
            RuntimeError("model 2 down"),
            _VALID_RESPONSE_JSON,
        ]
    )
    monkeypatch.setattr(groq, "CLIENT", fake_client)

    result = groq.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "accept"
    assert len(fake_client.completions.calls) == 3
    # Only the first 3 models are expected to have been tried -- the
    # 3rd call succeeds, so the loop stops there regardless of how
    # many more models GROQ_MODELS happens to have configured after
    # it (not hardcoding the full list length here, since that's an
    # operator config choice, not something this test should pin).
    assert (
        [c["model"] for c in fake_client.completions.calls]
        == groq.GROQ_MODELS[:3]
    )


def test_groq_raises_after_all_models_fail(monkeypatch):
    fake_client = _FakeClient(
        [RuntimeError(f"{m} down") for m in groq.GROQ_MODELS]
    )
    monkeypatch.setattr(groq, "CLIENT", fake_client)

    with pytest.raises(RuntimeError, match="down"):
        groq.evaluate_job(TEXT, FILTER_RESULT)

    assert len(fake_client.completions.calls) == len(groq.GROQ_MODELS)


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM_TESTS") != "1",
    reason=(
        "Live-provider integration test -- makes a real Groq API "
        "call. Opt in explicitly with RUN_LIVE_LLM_TESTS=1 and a real "
        "GROQ_API_KEY in .env; never runs as part of the default "
        "offline test suite."
    ),
)
def test_groq_live_call_returns_a_valid_decision():
    result = groq.evaluate_job(TEXT, FILTER_RESULT)
    assert result["decision"] in {"accept", "reject"}
