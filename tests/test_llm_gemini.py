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
from app.categories.data_analysis.profile import PROFILE
from app.llm import gemini
from app.llm.utils import GENERIC_SYSTEM_PROMPT


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
    assert call["config"].system_instruction == GENERIC_SYSTEM_PROMPT
    assert call["config"].response_mime_type == "application/json"

    # The system prompt must NOT be concatenated into contents.
    assert GENERIC_SYSTEM_PROMPT not in call["contents"]
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

    with pytest.raises(RuntimeError, match="No Gemini candidates are configured"):
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


_VALID_ARBITRATION_RESPONSE_JSON = json.dumps(
    {
        "selected_category": "data_analysis",
        "confidence": 88,
        "reason": "Primary deliverable is a BI dashboard.",
    }
)

_ARBITRATION_CANDIDATES = [
    {
        "id": "data_analysis",
        "name": "Data Analysis",
        "description": "Analytics and BI.",
        "arbitration_context": "Primary deliverable is analysis or BI.",
        "result": {"reason": "mixed signals", "categories": ["power_bi"], "negative_categories": []},
    },
]


def test_gemini_module_level_arbitration_initializes_provider_lazily(monkeypatch):
    """Direct regression test for the P0 defect: the module-level
    evaluate_category_arbitration() compatibility function called
    `_provider.evaluate_category_arbitration(...)` directly while
    `_provider` was still `None` at module load time (only
    evaluate_job() went through the lazy `_get_provider()` accessor).
    Since app.llm.manager.arbitrate_category() calls this exact
    module-level function -- and nothing calls evaluate_job() first to
    incidentally initialize `_provider` -- every real arbitration
    request raised AttributeError: 'NoneType' object has no attribute
    'evaluate_category_arbitration' before ever reaching the SDK. This
    test resets `_provider` to None (its true module-load state) and
    asserts the fake SDK client actually receives the arbitration
    request instead of the call blowing up on a None dereference.
    """
    monkeypatch.setattr(gemini, "_provider", None)
    fake_client = _FakeClient(response_text=_VALID_ARBITRATION_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])

    result = gemini.evaluate_category_arbitration(
        TEXT, _ARBITRATION_CANDIDATES, "system prompt"
    )

    assert result["selected_category"] == "data_analysis"
    assert len(fake_client.models.calls) == 1
    assert gemini._provider is not None


def test_gemini_arbitration_applies_configured_output_token_cap(monkeypatch):
    """Regression test for P2-7: config/project.json's
    arbitration_max_output_tokens for Gemini must actually reach
    GenerateContentConfig.max_output_tokens, not just exist as unused
    configuration.
    """
    monkeypatch.setattr(gemini, "_provider", None)
    fake_client = _FakeClient(response_text=_VALID_ARBITRATION_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])

    gemini.evaluate_category_arbitration(TEXT, _ARBITRATION_CANDIDATES, "system prompt")

    call = fake_client.models.calls[0]
    expected = next(
        p.arbitration_max_output_tokens for p in gemini.LLM_PROVIDERS if p.provider_id == "gemini"
    )
    assert call["config"].max_output_tokens == expected


def test_gemini_evaluate_job_applies_configured_output_token_cap(monkeypatch):
    fake_client = _FakeClient(response_text=_VALID_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])

    gemini.evaluate_job(TEXT, FILTER_RESULT)

    call = fake_client.models.calls[0]
    expected = next(
        p.max_output_tokens for p in gemini.LLM_PROVIDERS if p.provider_id == "gemini"
    )
    assert call["config"].max_output_tokens == expected


def test_gemini_rotates_across_configured_models(monkeypatch):
    fake_client = _FakeClient(response_text=_VALID_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])
    monkeypatch.setattr(
        gemini,
        "LLM_PROVIDERS",
        [
            type("Cfg", (), {
                "provider_id": "gemini",
                "models": ("model-a", "model-b"),
                "retry": type("Retry", (), {"max_attempts": 1, "wait_seconds": 0})(),
                "max_output_tokens": 600,
                "arbitration_max_output_tokens": 600,
            })()
        ],
    )
    provider = gemini.GeminiProvider()
    candidates = provider._candidates(lambda client, model: (lambda: model))
    assert [label for _, label, _ in candidates] == [
        "key #1, model: model-a",
        "key #1, model: model-b",
    ]
