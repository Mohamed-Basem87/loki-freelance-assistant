"""Regression tests for F-5 (audit finding: unused synchronous
get_freehub_seen() path bypassing the serialized StateManager
architecture), plus a closely-related composition-wiring bug found
while fixing it.

Two things are covered here:

1. StateStore (app.ports) / JsonStateStore (app.adapters.state.json) no
   longer expose a synchronous get_freehub_seen()/async_set_freehub_seen()
   pair that reads/writes StateManager state without going through its
   serialized executor (self._state.run(...)). The safe, already-used
   equivalents are DedupStore.get_seen()/set_seen().

2. app.wiring.composition.compose() was passing the JsonStateStore *port*
   facade into build_dedup(state_backend=state) instead of letting it
   resolve its own raw StateManager backend. StateDedupStore expects
   the raw backend (it calls `self._state.run(...)`), and JsonStateStore
   has no `run()` method -- so every real FreeHub dedup call
   (get_seen/set_seen/get_pending/set_pending) raised AttributeError
   the moment it was exercised through the actual composition root.
   This test constructs StateDedupStore/JsonStateStore the same way
   app.wiring.composition.compose() does, to prove dedup and state share
   the one serialized backend and dedup is actually usable.
"""
import asyncio

import pytest

from app.ports import StateStore, DedupStore


def test_state_store_port_no_longer_declares_the_sync_freehub_seen_bypass():
    assert not hasattr(StateStore, "get_freehub_seen"), (
        "StateStore must not require a synchronous get_freehub_seen() -- "
        "that bypasses the serialized StateManager executor. Use "
        "DedupStore.get_seen()/set_seen() instead."
    )
    assert not hasattr(StateStore, "async_set_freehub_seen")


def test_json_state_store_no_longer_implements_the_sync_freehub_seen_bypass():
    from app.adapters.state.json import JsonStateStore

    assert not hasattr(JsonStateStore, "get_freehub_seen")
    assert not hasattr(JsonStateStore, "async_set_freehub_seen")
    # The safe, serialized replacement must still be present and used
    # in production (app.adapters.state.dedup.StateDedupStore).
    assert hasattr(JsonStateStore, "get_seen")
    assert hasattr(JsonStateStore, "set_seen")


def test_build_dedup_as_used_by_composition_shares_the_serialized_backend():
    """Constructs StateDedupStore the same way app.wiring.composition.compose()
    does (wired to the raw StateManager singleton, not the JsonStateStore
    port facade), and confirms a get_seen/set_seen round-trip actually works
    end to end -- this would raise AttributeError before the fix,
    since the dedup adapter was wired to the JsonStateStore port facade
    (no `.run()`) instead of the raw serialized StateManager backend.
    """
    from app.services.state import state as raw_state_manager
    from app.adapters.state.json import JsonStateStore
    from app.adapters.state.dedup import StateDedupStore

    state_store = JsonStateStore(raw_state_manager)
    dedup = StateDedupStore(raw_state_manager)

    assert isinstance(dedup, StateDedupStore)
    # dedup must be wired to the same raw, serialized backend that
    # state_store itself wraps -- not to state_store's port facade.
    assert dedup._state is raw_state_manager
    assert dedup._state is state_store._state

    async def scenario():
        source = "__f5_regression_test_source__"
        # Snapshot the singleton's real on-disk state so this test
        # never leaves permanent pollution in the real state.json,
        # regardless of how the assertions below turn out.
        import copy
        original_data = copy.deepcopy(raw_state_manager.data)
        try:
            before = await dedup.get_seen(source)
            assert before == []
            await dedup.set_seen(source, ["a", "b"])
            after = await dedup.get_seen(source)
            assert after == ["a", "b"]
        finally:
            raw_state_manager.data = original_data
            raw_state_manager.save()

    asyncio.run(scenario())


def test_compose_wires_dedup_without_the_broken_state_backend_kwarg():
    """Guards against reintroducing `build_dedup(state_backend=state)`
    in the composition root, which passes the StateStore port facade
    (no `.run()`) instead of the raw backend build_dedup() resolves on
    its own."""
    import inspect

    import app.wiring.composition as composition_module

    source = inspect.getsource(composition_module.compose)
    assert "build_dedup(state_backend=state)" not in source, (
        "compose() must not pass the JsonStateStore port facade as "
        "dedup's state_backend -- StateDedupStore requires the raw, "
        "serialized StateManager backend (which build_dedup() resolves "
        "on its own when called with no arguments)."
    )


