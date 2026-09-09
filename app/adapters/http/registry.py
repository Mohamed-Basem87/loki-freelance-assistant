"""HTTP transport registry with lazy optional-client imports."""
import importlib
import os

_FACTORIES = {}
_PATHS = {"aiohttp": "app.adapters.http.aiohttp:AioHttpTransport"}

def register(backend_id, factory): _FACTORIES[backend_id.strip().lower()] = factory

def build(timeout=30):
    key = os.getenv("HTTP_BACKEND", "aiohttp").strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        path = _PATHS.get(key)
        if path is None: raise KeyError(f"Unknown HTTP_BACKEND: {key}")
        module, _, attr = path.partition(":")
        factory = getattr(importlib.import_module(module), attr)
        register(key, factory)
    return factory(timeout=timeout)
