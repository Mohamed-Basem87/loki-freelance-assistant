"""Configuration-driven notification guard provider registry."""
import importlib
from app.notification_guard import config as guard_config

_FACTORIES = {}

def register(provider_id, factory):
    _FACTORIES[provider_id.strip().lower()] = factory

def _load(provider_id):
    paths = {
        # Default adapter; future providers can register a class here without
        # changing the guard orchestration contract.
        "groq": "app.notification_guard.groq:GroqNotificationGuard",
    }
    path = paths.get(provider_id)
    if path is None:
        raise KeyError(f"Unknown notification guard provider: {provider_id}")
    module_name, _, attr = path.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    register(provider_id, factory)
    return factory

def factory(provider_id):
    key = provider_id.strip().lower()
    return _FACTORIES.get(key) or _load(key)

def build(ids=None):
    configured = tuple(ids) if ids is not None else guard_config.NOTIFICATION_GUARD_PROVIDERS
    return [(pid, factory(pid)) for pid in configured]
