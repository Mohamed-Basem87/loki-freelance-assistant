"""
app.notification_guard.provider / orchestration tests.

These exercise the provider abstraction introduced to mirror
app.llm.provider / app.llm.manager: the guard iterates every
registered GuardProvider in order (first success wins), exactly like
manager._EVALUATE_PROVIDERS, and adding another provider is purely a
registry entry -- no wrapper, config, or caller change. The wrapper
must expose the winning provider's id/model for logging, never a
hardcoded backend name.

No guard provider makes a real API call here: the registry table is
tested with fake GuardProvider implementations and with the real
GroqNotificationGuard (constructed but never invoked).
"""

import asyncio

import pytest

from app.notification_guard import config as guard_config
from app.notification_guard import guard as guard_module
from app.notification_guard import provider as provider_module
from app.notification_guard.groq import GroqNotificationGuard

FAKE_ID = "fake"
FAKE_MODEL = "fake-model"


class FakeGuard(provider_module.GuardProvider):
    id = FAKE_ID
    model = FAKE_MODEL

    def __init__(self, allow=True, reclassify_to=None, raise_exc=None):
        self.allow = allow
        self.reclassify_to = reclassify_to
        self.raise_exc = raise_exc
        self.evaluate_calls = 0
        self.evaluate_with_category_calls = 0

    def evaluate(self, title, description, system_prompt, deadline=None):
        self.evaluate_calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.allow

    def evaluate_with_category(
        self, title, description, system_prompt, original_category_id, deadline=None
    ):
        self.evaluate_with_category_calls += 1
        if self.raise_exc:
            raise self.raise_exc
        resolved = self.reclassify_to or original_category_id
        return self.allow, resolved


class _FailingGuard(provider_module.GuardProvider):
    id = "failing"
    model = "failing-model"

    def __init__(self):
        self.raise_exc = RuntimeError("provider down")

    def evaluate(self, title, description, system_prompt, deadline=None):
        raise self.raise_exc

    def evaluate_with_category(
        self, title, description, system_prompt, original_category_id, deadline=None
    ):
        raise self.raise_exc


def _is_registered(id_to_check, providers):
    return id_to_check in [provider_id for provider_id, _ in providers]


def test_registry_lists_groq_as_an_implemented_provider():
    providers = guard_module.get_guard_providers()
    assert _is_registered("groq", providers)


def test_registered_provider_factory_yields_a_guard_provider():
    providers = guard_module.get_guard_providers()
    providers = [
        factory()
        for _, factory in providers
    ]
    assert all(
        isinstance(p, provider_module.GuardProvider)
        for p in providers
    )
    assert all(isinstance(p.id, str) and isinstance(p.model, str) for p in providers)


def test_evaluate_uses_first_successful_provider(monkeypatch):
    fake_primary = FakeGuard()
    fake_secondary = FakeGuard(allow=False)

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            (FAKE_ID, lambda: fake_primary),
            ("secondary", lambda: fake_secondary),
        ],
    )

    allowed, provider_id, model = guard_module._evaluate_guard("t", "d", "p")
    assert allowed is True
    assert provider_id == FAKE_ID
    assert model == FAKE_MODEL
    assert fake_primary.evaluate_calls == 1
    assert fake_secondary.evaluate_calls == 0


def test_evaluate_falls_back_to_next_provider_on_failure(monkeypatch):
    fake_secondary = FakeGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            ("primary", lambda: _FailingGuard()),
            (FAKE_ID, lambda: fake_secondary),
        ],
    )

    allowed, provider_id, model = guard_module._evaluate_guard("t", "d", "p")
    assert allowed is True
    assert provider_id == FAKE_ID
    assert model == FAKE_MODEL
    assert fake_secondary.evaluate_calls == 1


def test_evaluate_raises_when_all_providers_fail(monkeypatch):
    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            ("primary", lambda: _FailingGuard()),
            ("secondary", lambda: _FailingGuard()),
        ],
    )

    with pytest.raises(RuntimeError) as exc_info:
        guard_module._evaluate_guard("t", "d", "p")

    assert "primary" in str(exc_info.value)
    assert "secondary" in str(exc_info.value)


def test_evaluate_with_category_returns_winning_provider_info(monkeypatch):
    fake_primary = FakeGuard(reclassify_to="full_stack")

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            (FAKE_ID, lambda: fake_primary),
        ],
    )

    result = guard_module._evaluate_guard_with_category(
        "t", "d", "p", "data_analysis"
    )
    allowed, resolved_id, provider_id, model = result
    assert allowed is True
    assert resolved_id == "full_stack"
    assert provider_id == FAKE_ID
    assert model == FAKE_MODEL


def test_evaluate_with_category_falls_back_on_failure(monkeypatch):
    fake_secondary = FakeGuard(reclassify_to="full_stack")

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            ("primary", lambda: _FailingGuard()),
            (FAKE_ID, lambda: fake_secondary),
        ],
    )

    allowed, resolved_id, provider_id, model = (
        guard_module._evaluate_guard_with_category(
            "t", "d", "p", "data_analysis"
        )
    )
    assert allowed is True
    assert resolved_id == "full_stack"
    assert provider_id == FAKE_ID
    assert model == FAKE_MODEL


_JOB = {
    "job_uuid": "job-1",
    "title": "Power BI dashboard",
    "description": "Build a sales dashboard from Excel data.",
}


def _disable_db_logging(monkeypatch):
    async def _noop(self, **kwargs):
        return None

    monkeypatch.setattr(
        guard_module.NotificationGuard,
        "_log_guard_decision",
        _noop,
    )


def test_notification_guard_wrapper_uses_orchestration_when_enabled(monkeypatch):
    fake = FakeGuard()
    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [(FAKE_ID, lambda: fake)],
    )
    monkeypatch.setattr(guard_config, "NOTIFICATION_GUARD_ENABLED", True)
    _disable_db_logging(monkeypatch)

    wrapper = guard_module.NotificationGuard()
    assert wrapper.enabled is True

    result = asyncio.run(
        wrapper.decide(
            _JOB,
            category_id="data_analysis",
            original_decision="Accepted",
        )
    )
    assert result["allowed"] is True
    assert fake.evaluate_with_category_calls == 1


def test_notification_guard_wrapper_is_none_when_disabled(monkeypatch):
    monkeypatch.setattr(guard_config, "NOTIFICATION_GUARD_ENABLED", False)
    _disable_db_logging(monkeypatch)

    wrapper = guard_module.NotificationGuard()
    assert wrapper.enabled is False

    result = asyncio.run(
        wrapper.decide(
            _JOB,
            category_id="data_analysis",
            original_decision="Accepted",
        )
    )
    assert result["allowed"] is True


class _CapturingGuard(FakeGuard):

    def __init__(self):
        super().__init__()
        self.last_title = None
        self.last_description = None

    def evaluate(self, title, description, system_prompt, deadline=None):
        self.last_title = title
        self.last_description = description
        return super().evaluate(title, description, system_prompt, deadline=deadline)

    def evaluate_with_category(
        self, title, description, system_prompt, original_category_id, deadline=None
    ):
        self.last_title = title
        self.last_description = description
        return super().evaluate_with_category(
            title, description, system_prompt, original_category_id, deadline=deadline
        )


def test_guard_bound_defaults_fit_groq_per_minute_budgets():
    """Pin the default budget so the FULL request stays under Groq's
    on-demand per-minute buckets (measured in production: gpt-oss 8,000
    TPM / qwen 7,000 ITPM, rejected deterministically with HTTP 413
    "Request too large"). The description cap is computed dynamically
    from the actual composed system prompt, so the total-estimate check
    uses the largest measured combined prompt (data_analysis+full_stack,
    ~21.8K chars). A regression here reintroduces an unrecoverable 413
    storm.
    """
    assert guard_config.MAX_GUARD_TOTAL_TOKENS < 7000, (
        "default MAX_GUARD_TOTAL_TOKENS must sit below the tightest "
        "per-minute budget (qwen 7,000 ITPM)"
    )
    rate = guard_config.GUARD_ESTIMATED_TOKENS_PER_CHAR
    assert 0.25 <= rate <= 0.35

    worst_sys_chars = 21_806
    _, bounded_desc = guard_module._bound_guard_input(
        "t" * 1_000,
        "x" * 200_000,
        "x" * worst_sys_chars,
    )
    est = lambda s: int(len(s) * rate)
    estimate = (
        est("x" * worst_sys_chars) + est(bounded_desc) + est("t" * 1_000) + 40
    )
    assert estimate <= guard_config.MAX_GUARD_TOTAL_TOKENS, (
        f"worst-case guard request estimated at {estimate} tokens "
        f"> budget {guard_config.MAX_GUARD_TOTAL_TOKENS}"
    )
    assert len(bounded_desc) >= guard_config.GUARD_MIN_TEXT_CHARS


def test_guard_bound_scales_with_system_prompt_size():
    """A bigger composed system prompt leaves less headroom for the
    description: the cap is 'just below the limit', not a fixed value."""
    small = guard_module._bound_guard_input(
        "t" * 50, "x" * 200_000, "small prompt"
    )[1]
    big = guard_module._bound_guard_input(
        "t" * 50, "x" * 200_000, "x" * 21_800
    )[1]
    rate = guard_config.GUARD_ESTIMATED_TOKENS_PER_CHAR
    est = lambda s: int(len(s) * rate)
    assert len(small) > len(big)
    for desc, sysp in ((small, "small prompt"), (big, "x" * 21_800)):
        total = est(sysp) + est(desc) + est("t" * 50) + 40
        assert total <= guard_config.MAX_GUARD_TOTAL_TOKENS


def test_evaluate_bounds_oversized_description_and_title(monkeypatch):
    """A description/title larger than every model's payload limit must
    be shrunk before it reaches the provider -- a 200K-char Wuzzuf
    description would otherwise produce an unrecoverable HTTP 413."""
    capturing = _CapturingGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [(FAKE_ID, lambda: capturing)],
    )

    allowed, provider_id, model = guard_module._evaluate_guard(
        "t" * 10_000,
        "x" * 200_000,
        "p",
    )

    assert allowed is True
    assert provider_id == FAKE_ID
    assert model == FAKE_MODEL
    assert len(capturing.last_description) == guard_config.MAX_GUARD_TEXT_CHARS
    assert len(capturing.last_title) == guard_config.MAX_GUARD_TITLE_CHARS


def test_evaluate_with_category_bounds_oversized_input(monkeypatch):
    capturing = _CapturingGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [(FAKE_ID, lambda: capturing)],
    )

    result = guard_module._evaluate_guard_with_category(
        "t" * 10_000,
        "x" * 200_000,
        "p",
        "data_analysis",
    )
    allowed, resolved_id, provider_id, model = result

    assert allowed is True
    assert resolved_id == "data_analysis"
    assert len(capturing.last_description) == guard_config.MAX_GUARD_TEXT_CHARS
    assert len(capturing.last_title) == guard_config.MAX_GUARD_TITLE_CHARS


def test_guard_input_small_inputs_pass_through_unchanged(monkeypatch):
    capturing = _CapturingGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [(FAKE_ID, lambda: capturing)],
    )

    title, description = "Power BI dashboard", "Build a sales dashboard."
    allowed, _, _ = guard_module._evaluate_guard(title, description, "p")

    assert allowed is True
    assert capturing.last_title == title
    assert capturing.last_description == description


def test_notification_guard_decision_bounds_input_end_to_end(monkeypatch):
    """The production path (resolve_category -> decide -> provider) with
    a 200K description must hand the provider a bounded payload instead
    of a deterministic 413. The cap is dynamic: computed from the real
    combined system prompt, so it must (a) be well under the raw input,
    (b) never exceed the absolute MAX_GUARD_TEXT_CHARS ceiling, and
    (c) never drop below the GUARD_MIN_TEXT_CHARS floor."""
    capturing = _CapturingGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [(FAKE_ID, lambda: capturing)],
    )
    monkeypatch.setattr(guard_config, "NOTIFICATION_GUARD_ENABLED", True)
    _disable_db_logging(monkeypatch)

    wrapper = guard_module.NotificationGuard()

    result = asyncio.run(
        wrapper.decide(
            {
                **_JOB,
                "description": "x" * 200_000,
            },
            category_id="data_analysis",
            original_decision="Accepted",
        )
    )

    assert result["allowed"] is True
    assert capturing.evaluate_with_category_calls == 1
    bounded = len(capturing.last_description)
    assert bounded < 200_000
    assert bounded <= guard_config.MAX_GUARD_TEXT_CHARS
    assert bounded >= guard_config.GUARD_MIN_TEXT_CHARS


def test_adding_a_fallback_provider_needs_no_wrapper_change(monkeypatch):
    """The exact scenario the user wants easy: registering a second
    provider is all it takes -- the wrapper and orchestration already
    handle the fallback with zero caller changes."""
    fake_primary = _FailingGuard()
    fake_secondary = FakeGuard()

    monkeypatch.setattr(
        guard_module,
        "_GUARD_PROVIDERS",
        [
            ("primary", lambda: fake_primary),
            (FAKE_ID, lambda: fake_secondary),
        ],
    )
    monkeypatch.setattr(guard_config, "NOTIFICATION_GUARD_ENABLED", True)
    _disable_db_logging(monkeypatch)

    wrapper = guard_module.NotificationGuard()
    result = asyncio.run(
        wrapper.decide(
            _JOB,
            category_id="data_analysis",
            original_decision="Accepted",
        )
    )
    assert result["allowed"] is True
    assert fake_secondary.evaluate_with_category_calls == 1

def test_groq_guard_construction_initializes_configured_clients(monkeypatch):
    fake_clients = [object(), object()]
    monkeypatch.setattr(
        "app.notification_guard.groq.CLIENTS", fake_clients, raising=True
    )
    provider = GroqNotificationGuard(models=("guard-model",))
    assert provider.clients == fake_clients
    assert provider.models == ["guard-model"]


def test_groq_guard_reports_winning_models_model_after_rotation(monkeypatch):
    """Regression test: provider.model must be the CONCRETE model that
    actually produced the decision after a rotate call -- not the first
    configured model. run_with_rotation returns the winning candidate id
    (groq-guard-key{n}-{model}) and the guard maps it back to the exact
    model. Model names containing dashes must not confuse the mapping
    (replicating the key-major/model-minor enumeration, not string-
    splitting)."""
    calls = []

    def fake_generate(client, model, title, description, system_prompt, max_tokens=None):
        calls.append(model)
        if model == "alpha":
            raise RuntimeError("boom")

        class _Message:
            content = '{"decision": "notify"}'

        class _Choice:
            finish_reason = "stop"
            message = _Message()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    monkeypatch.setattr("app.notification_guard.groq._generate_response", fake_generate)

    provider = GroqNotificationGuard(
        clients=[object(), object()],
        models=("alpha", "openai/gpt-oss-120b"),
    )
    assert provider.model == "alpha", "nominal default before any call"

    allowed = provider.evaluate("t", "d", "system")

    assert allowed is True
    assert provider.model == "openai/gpt-oss-120b", (
        "the guard must report the model that actually produced the "
        "decision, i.e. the second one (first failed), not the first "
        "configured model"
    )
    assert calls == ["alpha", "openai/gpt-oss-120b"]


def test_guard_provider_chain_is_not_built_at_import_time():
    """The provider chain must be resolved lazily on first use, not at
    module import time -- matching the app.llm.manager pattern.  A
    fresh import would show None before any evaluation call."""
    import importlib
    import app.notification_guard.guard as guard_mod
    saved = guard_mod._GUARD_PROVIDERS
    try:
        guard_mod._GUARD_PROVIDERS = None
        assert guard_mod._GUARD_PROVIDERS is None
        providers = guard_mod.get_guard_providers()
        assert providers is not None
        assert len(providers) > 0
        # Second call returns the cached same object.
        assert guard_mod.get_guard_providers() is providers
    finally:
        guard_mod._GUARD_PROVIDERS = saved


def test_evaluate_guard_uses_get_guard_providers_at_call_time(monkeypatch):
    """Replacing _GUARD_PROVIDERS after the initial cache build is
    respected because _evaluate_guard calls get_guard_providers() on
    every invocation -- the chain is read from the module attribute, not
    captured in a closure."""
    fake = FakeGuard()
    # First set the cache so get_guard_providers() returns this.
    monkeypatch.setattr(
        guard_module, "_GUARD_PROVIDERS",
        [("fake", lambda: fake)],
    )
    allowed, provider_id, model = guard_module._evaluate_guard("t", "d", "p")
    assert allowed is True
    assert provider_id == FAKE_ID
