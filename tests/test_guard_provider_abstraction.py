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

    def evaluate(self, title, description, system_prompt):
        self.evaluate_calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.allow

    def evaluate_with_category(
        self, title, description, system_prompt, original_category_id
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

    def evaluate(self, title, description, system_prompt):
        raise self.raise_exc

    def evaluate_with_category(
        self, title, description, system_prompt, original_category_id
    ):
        raise self.raise_exc


def _is_registered(id_to_check, providers):
    return id_to_check in [provider_id for provider_id, _ in providers]


def test_registry_lists_groq_as_an_implemented_provider():
    assert _is_registered("groq", guard_module._GUARD_PROVIDERS)


def test_registered_provider_factory_yields_a_guard_provider():
    providers = [
        factory()
        for _, factory in guard_module._GUARD_PROVIDERS
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
    async def _noop(**kwargs):
        return None

    monkeypatch.setattr(
        guard_module,
        "log_guard_decision",
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