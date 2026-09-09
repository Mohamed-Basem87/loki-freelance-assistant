"""Persistence adapter registry; concrete backends are imported lazily.

This registry is the wiring layer for persistence: it resolves the concrete
backing store (the DBLogger singleton for SQLite) and injects it into the
selected adapter, so concrete repository adapters depend only on the port plus
their injected dependency rather than reaching into legacy application modules
themselves.
"""
import importlib
import inspect
import os

_FACTORIES = {}

def register(backend_id, factory):
    _FACTORIES[backend_id.strip().lower()] = factory

def _load_default(backend_id):
    paths = {
        "sqlite": "app.adapters.repositories.sqlite:SQLiteRepository",
    }
    path = paths.get(backend_id)
    if path is None:
        raise KeyError(f"Unknown DATABASE_BACKEND: {backend_id}")
    module_name, _, attr = path.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    register(backend_id, factory)
    return factory

def _resolve_backend(factory):
    """Resolve the backing store a factory declares and inject it.

    Concrete adapters take the store they wrap as a constructor parameter; the
    registry is the single place that binds it, so the wiring lives in the
    composition layer instead of inside the adapter module."""
    if "db" not in set(inspect.signature(factory).parameters):
        return {}
    from app.logger import logger as _db
    return {"db": _db}

def build():
    key = os.getenv("DATABASE_BACKEND", "sqlite").strip().lower()
    factory = _FACTORIES.get(key) or _load_default(key)
    return factory(**_resolve_backend(factory))
