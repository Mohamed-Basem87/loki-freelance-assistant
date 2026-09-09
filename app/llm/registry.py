"""Configuration-driven LLM provider registry.

Provider modules own their prompts, candidate rotation and rate limiting. The
registry only resolves the selected adapter class, so adding a provider does
not require editing the orchestration layer.
"""
import importlib
from app.runtime_config import LLM_PROVIDERS

_FACTORIES = {}

def register(provider_id, factory):
    _FACTORIES[provider_id.strip().lower()] = factory

def _load(config):
    path = config.adapter
    if not path:
        raise ValueError(f"LLM provider {config.provider_id!r} has no adapter path")
    module_name, sep, attr = path.partition(":")
    if not sep:
        raise ValueError(f"Invalid LLM adapter path: {path!r}; expected module:Class")
    factory = getattr(importlib.import_module(module_name), attr)
    register(config.provider_id, factory)
    return factory

def build(provider_id):
    key = provider_id.strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        config = next((p for p in LLM_PROVIDERS if p.provider_id.lower() == key), None)
        if config is None:
            raise KeyError(f"Unknown LLM provider: {provider_id}")
        factory = _load(config)
    return factory()

def configured_ids():
    return tuple(p.provider_id for p in LLM_PROVIDERS)
