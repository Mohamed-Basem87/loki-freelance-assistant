"""
End-to-end regression test for the P0 category-arbitration defect
(see ENGINEERING_REMEDIATION_REPORT.md / the 2026-09-06 audit, finding
P0-1).

Every existing arbitration test before this file monkeypatched
app.llm.manager.gemini_arbitrate / groq_arbitrate directly -- which
means none of them ever executed the real
app.llm.gemini.evaluate_category_arbitration /
app.llm.groq.evaluate_category_arbitration module-level compatibility
functions that app.llm.manager actually calls in production. That is
exactly where the bug lived: those two functions dereferenced the
module-level `_provider` global directly (`_provider.evaluate_...`)
instead of going through the lazy `_get_provider()` accessor that
`evaluate_job()` correctly uses -- so on a fresh process, the first
real arbitration request (there is no preceding evaluate_job() call to
incidentally initialize `_provider`) raised an AttributeError on None
before any SDK request was made.

This test drives the real, unmocked call chain:

    app.llm.manager.arbitrate_category()
        -> manager.gemini_arbitrate() / manager.groq_arbitrate()
            -> app.llm.gemini.evaluate_category_arbitration()
               app.llm.groq.evaluate_category_arbitration()
                (the real module-level compatibility functions)
                -> GeminiProvider/GroqProvider.evaluate_category_arbitration()

with only the SDK clients faked out, and with both providers' module
singletons reset to None (their true state at process start) so a
regression of the P0 bug fails this test immediately.
"""

import json

from app.llm import gemini, groq, manager


_GEMINI_RESPONSE_JSON = json.dumps(
    {
        "selected_category": "data_analysis",
        "confidence": 91,
        "reason": "Primary deliverable is a BI dashboard with DAX measures.",
    }
)

_GROQ_RESPONSE_JSON = json.dumps(
    {
        "selected_category": "web_development",
        "confidence": 70,
        "reason": "Primary deliverable is a web application.",
    }
)

_CANDIDATES = [
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

TEXT = "Need a Power BI dashboard built from an Excel sales dataset."


def _fake_gemini_client(response_text):
    class _FakeResponse:
        def __init__(self, text):
            self.text = text

    class _FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, *, model, contents, config=None):
            self.calls.append({"model": model, "contents": contents, "config": config})
            return _FakeResponse(response_text)

    class _FakeClient:
        def __init__(self):
            self.models = _FakeModels()

    return _FakeClient()


def _fake_groq_client(response_text):
    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)
            self.finish_reason = "stop"

    class _FakeCompletionResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]

    class _FakeCompletions:
        def __init__(self):
            self.calls = []

        def create(self, *, model, messages, response_format=None, max_tokens=None):
            self.calls.append({"model": model, "messages": messages})
            return _FakeCompletionResponse(response_text)

    class _FakeChat:
        def __init__(self, completions):
            self.completions = completions

    class _FakeClient:
        def __init__(self):
            self.completions = _FakeCompletions()
            self.chat = _FakeChat(self.completions)

    return _FakeClient()


def test_manager_arbitration_reaches_real_gemini_wrapper_from_a_fresh_process(monkeypatch):
    """Regression test: with Gemini's module-level `_provider` reset to
    None (its real state before any evaluate_job() call has run), a
    manager-level arbitration request must still succeed by lazily
    instantiating the provider -- not raise on a None dereference.
    """
    monkeypatch.setattr(gemini, "_provider", None)
    fake_client = _fake_gemini_client(_GEMINI_RESPONSE_JSON)
    monkeypatch.setattr(gemini, "CLIENTS", [fake_client])

    # Explicit system_prompt bypasses build_category_arbitration_system_prompt's
    # need to import each candidate's real app.categories.<id>.llm_prompt
    # module, keeping this test focused on the provider-initialization
    # bug rather than category registry wiring.
    result = manager.arbitrate_category(TEXT, _CANDIDATES, system_prompt="system prompt")

    assert result["selected_category"] == "data_analysis"
    assert result["provider"] == "gemini"
    assert len(fake_client.models.calls) == 1
    assert gemini._provider is not None


def test_manager_arbitration_reaches_real_groq_wrapper_on_gemini_fallback(monkeypatch):
    """Same regression, exercised through the Groq fallback path: Gemini
    fails (e.g. real quota exhaustion), manager falls back to Groq's
    real module-level wrapper, which must also lazily initialize its
    provider rather than raising on a None `_provider`.
    """
    monkeypatch.setattr(gemini, "_provider", None)
    monkeypatch.setattr(gemini, "CLIENTS", [])  # forces "No candidates configured"

    monkeypatch.setattr(groq, "_provider", None)
    fake_groq_client = _fake_groq_client(_GROQ_RESPONSE_JSON)
    monkeypatch.setattr(groq, "CLIENT", fake_groq_client)

    result = manager.arbitrate_category(TEXT, _CANDIDATES, system_prompt="system prompt")

    assert result["selected_category"] == "web_development"
    assert result["provider"] == "groq"
    assert len(fake_groq_client.completions.calls) == 1
    assert groq._provider is not None
