"""State composition boundary. Default remains JSON/StateManager."""
from app.adapters.state.registry import build
store = build()
