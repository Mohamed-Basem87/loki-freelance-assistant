"""
app.state.StateManager tests. No app.config dependency (state.py only
imports asyncio/json/os/pathlib/concurrent.futures), so this runs
fully offline regardless of credentials, same as test_keyword_filter.py.

Covers the invariants the audit specifically called out:
  - processed messages advance the watermark, and the watermark
    round-trips correctly
  - a corrupted state.json is treated as "no state" rather than
    crashing (state.load()'s fallback)
  - saves are atomic (temp file + os.replace, no partial file ever
    left in place of the real one)
  - the async wrapper introduced in Fix 2 (async_set_last_message_id /
    async_set_freehub_seen) actually persists correctly, and many
    concurrent async writes never corrupt the file (the exact race the
    dedicated single-worker executor exists to prevent)
  - FreeHub's persisted "seen" list round-trips per source
"""

import asyncio
import json

import pytest

import app.state as state_module
from app.state import StateCorruptionError, StateManager


@pytest.fixture()
def isolated_state_file(tmp_path, monkeypatch):
    """
    Point app.state.STATE_FILE at a throwaway path for the duration of
    a test, instead of the real database/state.json.
    """
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)
    return fake_state_file


def test_watermark_round_trips_through_save_and_load(isolated_state_file):
    manager = StateManager()
    manager.load()

    assert manager.get_last_message_id(-100123) == 0

    manager.set_last_message_id(-100123, 5678)

    # A fresh StateManager reading the same file must see the same
    # value -- this is what protects against permanently missing or
    # re-processing a job across a restart.
    reloaded = StateManager()
    reloaded.load()

    assert reloaded.get_last_message_id(-100123) == 5678


def test_freehub_seen_round_trips_per_source(isolated_state_file):
    manager = StateManager()
    manager.load()

    assert manager.get_freehub_seen("kafiil") == []

    manager.set_freehub_seen("kafiil", ["a", "b", "c"])
    manager.set_freehub_seen("freelancer", ["x", "y"])

    reloaded = StateManager()
    reloaded.load()

    assert reloaded.get_freehub_seen("kafiil") == ["a", "b", "c"]
    assert reloaded.get_freehub_seen("freelancer") == ["x", "y"]
    # The two sources' seen-lists must not bleed into each other.
    assert reloaded.get_freehub_seen("kafiil") != reloaded.get_freehub_seen(
        "freelancer"
    )


def test_corrupted_state_file_with_no_backup_fails_loud(isolated_state_file):
    """
    P0-3 regression test. The old behavior silently treated a
    corrupted file as {} -- indistinguishable from a genuine first
    run -- which made every channel/source watermark look
    never-seen and silently skipped backfilling everything posted
    since the last good save. A corrupted file with no recoverable
    backup must now fail loudly instead of starting clean.
    """
    isolated_state_file.parent.mkdir(parents=True, exist_ok=True)
    isolated_state_file.write_text("{ this is not valid json ]!!")

    manager = StateManager()

    with pytest.raises(StateCorruptionError):
        manager.load()


def test_corrupted_state_file_recovers_from_backup(isolated_state_file):
    """
    A corrupted live file recovers from the .bak.json snapshot
    save() maintains, instead of resetting to an empty state.
    """
    manager = StateManager()
    manager.load()
    manager.set_last_message_id(-100123, 5678)

    # Corrupt the live file but leave the backup save() just wrote
    # intact.
    isolated_state_file.write_text("{ not valid json at all ]!!")
    backup_path = isolated_state_file.with_suffix(".bak.json")
    assert backup_path.exists()

    recovered = StateManager()
    recovered.load()  # must not raise

    assert recovered.get_last_message_id(-100123) == 5678


def test_save_is_atomic_no_leftover_temp_file(isolated_state_file):
    manager = StateManager()
    manager.load()

    manager.set_last_message_id(-100999, 42)

    temp_path = isolated_state_file.with_suffix(".tmp.json")
    assert not temp_path.exists(), "Temp file must be replaced onto the real path, not left behind"
    assert isolated_state_file.exists()

    on_disk = json.loads(isolated_state_file.read_text())
    assert on_disk["-100999"] == 42


def test_async_wrapper_persists_correctly(isolated_state_file):
    manager = StateManager()
    manager.load()

    asyncio.run(manager.async_set_last_message_id(-100555, 999))

    assert manager.get_last_message_id(-100555) == 999

    reloaded = StateManager()
    reloaded.load()
    assert reloaded.get_last_message_id(-100555) == 999


def test_many_concurrent_async_writes_never_corrupt_the_file(isolated_state_file):
    """
    Regression test for the Fix 2 concurrency concern: Telegram and
    FreeHub can both call the async setters around the same time under
    asyncio.gather. Firing a burst of concurrent async writes must
    never produce a torn/invalid state.json -- the dedicated
    single-worker executor (see app.state._EXECUTOR) is what
    guarantees this by serializing every write onto one thread.
    """
    manager = StateManager()
    manager.load()

    async def hammer():
        await asyncio.gather(
            *[
                manager.async_set_last_message_id(-100000 - i, i)
                for i in range(50)
            ]
        )

    asyncio.run(hammer())

    # The file must always be valid, complete JSON after the burst --
    # never a partially-written/corrupted file.
    on_disk = json.loads(isolated_state_file.read_text())

    for i in range(50):
        assert on_disk[str(-100000 - i)] == i


def test_cross_source_claim_is_idempotent_for_same_job(isolated_state_file):
    """
    A crash can occur after a job owns a cross-source claim but before
    classification finishes. Retrying that same job must keep its
    ownership, while a different job must still lose the claim.
    """
    manager = StateManager()
    manager.load()

    assert manager.claim_cross_source_project("123456", "job-a") is True
    assert manager.claim_cross_source_project("123456", "job-a") is True
    assert manager.claim_cross_source_project("123456", "job-b") is False


def test_cross_source_claims_expire_after_the_dedup_ttl(isolated_state_file):
    """
    Cross-source dedup records are bounded by _CROSS_SOURCE_TTL_SECONDS
    (30 days): once a claim is older than the window it is pruned, so the
    same project id can be claimed again. A recent claim must still be
    honored -- that is the whole point of the window.
    """
    import time

    manager = StateManager()
    manager.load()

    ttl = state_module._CROSS_SOURCE_TTL_SECONDS
    now = time.time()

    # Recent claim: survives and stays owned by its original job.
    manager.data.setdefault(state_module._CROSS_SOURCE_SEEN_KEY, {})["recent"] = {
        "job_uuid": "job-recent",
        "claimed_at": now,
    }
    assert manager.claim_cross_source_project("recent", "job-other") is False, (
        "a claim inside the TTL window must still dedupe"
    )

    # Expired claim (older than the TTL): pruned, so it is reclaimable.
    manager.data.setdefault(state_module._CROSS_SOURCE_SEEN_KEY, {})["expired"] = {
        "job_uuid": "job-expired",
        "claimed_at": now - ttl - 100,
    }
    assert manager.claim_cross_source_project("expired", "job-new") is True, (
        "a claim older than the TTL must be pruned and reclaimable"
    )

    # The expired record was pruned and immediately re-owned by the new
    # claimant, proving the old ownership expired rather than persisting.
    assert (
        manager.data[state_module._CROSS_SOURCE_SEEN_KEY]["expired"]["job_uuid"]
        == "job-new"
    )


def test_state_timeout_quarantines_shared_executor_instead_of_replacing_it(monkeypatch):
    """
    Regression test matching the DB-logger version in
    test_full_remediation_regressions.py: a state.run() call that never
    completes must poison the shared state backend (so every subsequent
    call fails closed) and must NOT install a fresh executor -- a second
    worker would mutate the same JSON state concurrently with the
    still-stuck worker thread (see the quarantine comment in run()).
    """
    original_executor = state_module._EXECUTOR
    original_timeout = state_module._STATE_TIMEOUT_SECONDS
    original_poisoned = state_module._EXECUTOR_POISONED
    try:
        class FakeLoop:
            def run_in_executor(self, executor, fn):
                async def never():
                    await asyncio.Event().wait()
                return never()

        monkeypatch.setattr(state_module, "_STATE_TIMEOUT_SECONDS", 0.001)

        async def scenario():
            class Executor:
                pass
            state_module._EXECUTOR = Executor()
            state_module._EXECUTOR_POISONED = False

            monkeypatch.setattr(
                asyncio,
                "get_running_loop",
                lambda: FakeLoop(),
            )
            manager = StateManager()

            # The first call times out and quarantines the shared
            # backend; the executor object is deliberately left in
            # place (no second writer installed).
            with pytest.raises(state_module.StuckExecutorError, match="quarantined"):
                await manager.run(manager.set_last_message_id, -100111, 5)
            assert state_module._EXECUTOR_POISONED is True
            assert isinstance(state_module._EXECUTOR, Executor)

            # Every later call fails closed immediately instead of
            # queuing behind the still-stuck worker -- restart restores.
            with pytest.raises(state_module.StuckExecutorError, match="quarantined"):
                await manager.run(manager.set_last_message_id, -100222, 7)

        asyncio.run(scenario())
    finally:
        state_module._EXECUTOR = original_executor
        state_module._STATE_TIMEOUT_SECONDS = original_timeout
        state_module._EXECUTOR_POISONED = original_poisoned
