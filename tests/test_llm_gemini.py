"""
app.llm.gemini tests.

Like test_llm_manager.py, importing this module needs app.config to
be importable -- see tests/conftest.py. No real Gemini call is made
in the default (offline) test suite: app.llm.gemini.CLIENTS is
monkeypatched with a fake client that records how it was called and
returns a canned response, so these tests verify request *structure*
(model name, and -- directly protecting the Fix 10 change -- that the
system prompt is passed via GenerateContentConfig.system_instruction
rather than concatenated into `contents`) without ever touching the
network.

A separate, explicitly-opt-in live test at the bottom exercises the
real API end-to-end for someone who wants to manually verify actual
Gemini connectivity/credentials; it's skipped by default and does not
run as part of a normal `pytest` invocation.
"""

import json
import os

import pytest

from app.filters import keyword_filter
from app.llm import gemini


TEXT = """
Need a Power BI dashboard built from an Excel sales dataset.
The dashboard should include KPIs, charts, slicers,
and DAX measures.
"""

FILTER_RESULT = keyword_filter(TEXT, title="Power BI Dashboard Needed")

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


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, response_text=None, exception=None):
        self.response_text = response_text
        self.exception = exception
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.exception is not None:
            raise self.exception
        return _FakeResponse(self.response_text)


class _FakeClient:
    def __init__(self, response_text=None, exception=None):
        self.models = _FakeModels(response_text=response_text, exception=exception)


def test_gemini_uses_system_instruction_not_string_concatenation(monkeypatch):
    """
    Direct regression test for the Fix 10 change: the system prompt
    must be passed via GenerateContentConfig.system_instruction, and
    `contents` must carry only the untrusted job-containing user
    prompt -- matching the structural separation app.llm.groq already
    gets for free from its system/user message roles, instead of the
    previous SYSTEM_PROMPT + "\\n\\n" + prompt single-string join.
    """
    fake_client = _FakeClient(response_text=_VALID_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])

    result = gemini.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "accept"
    assert len(fake_client.models.calls) == 1

    call = fake_client.models.calls[0]
    assert call["model"] == "gemini-3.5-flash"

    assert call["config"] is not None
    assert call["config"].system_instruction == gemini.SYSTEM_PROMPT
    assert call["config"].response_mime_type == "application/json"

    # The system prompt must NOT be concatenated into contents.
    assert gemini.SYSTEM_PROMPT not in call["contents"]
    # contents must still carry the actual job text somewhere inside
    # the built prompt (build_prompt() wraps it in <JobDescription>).
    assert "Power BI dashboard" in call["contents"]
    assert "Excel sales dataset" in call["contents"]


def test_gemini_falls_back_across_keys_on_failure(monkeypatch):
    failing_client = _FakeClient(exception=RuntimeError("bad request"))
    working_client = _FakeClient(response_text=_VALID_RESPONSE_JSON)

    monkeypatch.setattr(gemini, "CLIENTS", [failing_client, working_client])

    result = gemini.evaluate_job(TEXT, FILTER_RESULT)

    assert result["decision"] == "accept"
    assert len(failing_client.models.calls) == 1
    assert len(working_client.models.calls) == 1


def test_gemini_raises_when_no_keys_configured(monkeypatch):
    monkeypatch.setattr(gemini, "CLIENTS", [])

    with pytest.raises(RuntimeError, match="No Gemini API keys"):
        gemini.evaluate_job(TEXT, FILTER_RESULT)


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM_TESTS") != "1",
    reason=(
        "Live-provider integration test -- makes a real Gemini API "
        "call. Opt in explicitly with RUN_LIVE_LLM_TESTS=1 and a real "
        "GEMINI_API_KEYS in .env; never runs as part of the default "
        "offline test suite."
    ),
)
def test_gemini_live_call_returns_a_valid_decision():
    result = gemini.evaluate_job(TEXT, FILTER_RESULT)
    assert result["decision"] in {"accept", "reject"}
