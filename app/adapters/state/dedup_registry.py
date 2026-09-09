"""Independent dedup adapter registry."""
import os

def build(state_backend=None):
    if state_backend is None or isinstance(state_backend, str):
        key = (state_backend or os.getenv("STATE_BACKEND", "json")).strip().lower()
        if key == "json":
            from app.state import state
            state_backend = state
        else:
            raise KeyError(f"Unknown STATE_BACKEND for dedup: {key}")
    from app.adapters.state.dedup import StateDedupStore
    return StateDedupStore(state_backend)
