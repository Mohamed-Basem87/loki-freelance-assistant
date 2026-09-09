"""State adapter registry; concrete backends are imported lazily.

The same registry supports both the composition root and the legacy
``app.state_store`` facade. A caller may inject the state backend it already
assembled; otherwise the registry resolves the default JSON backend singleton.
"""
import os

def build(state_backend=None):
    key = os.getenv("STATE_BACKEND", "json").strip().lower()
    if key == "json":
        if state_backend is None:
            from app.state import state
            state_backend = state
        from app.adapters.state.json import JsonStateStore
        return JsonStateStore(state_backend)
    raise KeyError(f"Unknown STATE_BACKEND: {key}")
