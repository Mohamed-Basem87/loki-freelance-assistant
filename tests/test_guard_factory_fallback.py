"""Regression tests for the guard provider factory-fallback fix.

Every registered guard provider is part of the same attempt chain. If a
provider's *factory* raises (e.g. missing credentials or a broken import),
that must be treated exactly like an evaluation failure: the chain falls
through to the next provider instead of aborting and reporting the whole
guard as failed.

Also guards the "all providers fail" and "all providers on cooldown"
terminal behaviors, which must raise a clear RuntimeError rather than
silently allowing or blocking a notification.
"""
import pytest

from app.notification_guard import guard as guard_module


class _StubProvider:
    def __init__(self, provider_id, allowed, model="m"):
        self.id = provider_id
        self.model = model
        self.allowed = allowed

    def evaluate(self, title, description, system_prompt, deadline=None):
        return self.allowed

    def evaluate_with_category(self, title, description, system_prompt, original_category_id, deadline=None):
        return self.allowed, original_category_id


def _providers_factory(provider):
    def factory():
        return provider
    return factory


def test_factory_that_raises_falls_through_to_the_next_provider(monkeypatch):
    def broken_factory():
        raise RuntimeError("no credentials configured")

    good = _StubProvider("good", allowed=True)
    chain = [("broken", broken_factory), ("good", _providers_factory(good))]
    monkeypatch.setattr(guard_module, "get_guard_providers", lambda: chain)

    allowed, provider_id, model = guard_module._evaluate_guard("t", "d", "p")

    assert allowed is True
    assert provider_id == "good"
    assert model == "m"


def test_factory_that_raises_falls_through_with_category(monkeypatch):
    def broken_factory():
        raise ValueError("bad key")

    good = _StubProvider("good", allowed=False)
    chain = [("broken", broken_factory), ("good", _providers_factory(good))]
    monkeypatch.setattr(guard_module, "get_guard_providers", lambda: chain)

    allowed, resolved_category_id, provider_id, model = (
        guard_module._evaluate_guard_with_category("t", "d", "p", "data_analysis")
    )

    assert allowed is False
    assert resolved_category_id == "data_analysis"
    assert provider_id == "good"


def test_all_providers_fail_raises_clear_runtime_error(monkeypatch):
    def broken_factory():
        raise RuntimeError("down")

    chain = [("a", broken_factory), ("b", broken_factory)]
    monkeypatch.setattr(guard_module, "get_guard_providers", lambda: chain)

    with pytest.raises(RuntimeError, match="All guard providers failed"):
        guard_module._evaluate_guard("t", "d", "p")


def test_all_providers_on_cooldown_still_attempts_and_raises(monkeypatch):
    # Cooldown surfaces as an evaluation exception (run_with_rotation raises
    # when every candidate is cooling down), so it must fail the same way and
    # report every provider's participation.
    def raiser_factory():
        class _Raising(_StubProvider):
            def evaluate(self, title, description, system_prompt, deadline=None):
                raise RuntimeError("all candidates cooling down")

        return _Raising("x", allowed=True)

    # First provider raises (cooldown), second succeeds.
    mixed = [("a", raiser_factory), ("b", _providers_factory(_StubProvider("b", allowed=True)))]
    monkeypatch.setattr(guard_module, "get_guard_providers", lambda: mixed)

    allowed, provider_id, _ = guard_module._evaluate_guard("t", "d", "p")
    assert allowed is True
    assert provider_id == "b"

    # All on cooldown -> every provider raises -> clear error.
    monkeypatch.setattr(
        guard_module, "get_guard_providers", lambda: [("a", raiser_factory), ("b", raiser_factory)]
    )
    with pytest.raises(RuntimeError, match="All guard providers failed"):
        guard_module._evaluate_guard("t", "d", "p")
