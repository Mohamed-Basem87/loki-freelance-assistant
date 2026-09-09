"""
app.freehub tests: seen-state dedup and job-identity invariants.

Importing app.freehub pulls in app.config (for FREEHUB_USER_ID/
FREEHUB_PAGE_SIZE), so -- like test_pipeline.py etc. -- this relies on
tests/conftest.py's environment defaults to import without a real
.env. No real HTTP request is ever made: app.freehub.fetch_projects is
monkeypatched with a fake in-memory paginator, so poll_once()'s own
dedup/backfill/tagging logic is exercised directly and offline.
"""

import asyncio

import pytest

import app.freehub as freehub_module
import app.state as state_module
from app.state import StateManager


@pytest.fixture()
def isolated_freehub_state(tmp_path, monkeypatch):
    """
    Give app.freehub a clean, isolated slate for one test:
      - a throwaway STATE_FILE (via app.state.state, the singleton
        app.freehub actually reads/writes through)
      - a fresh in-memory _seen cache and _seeded_from_state flag,
        since both are module-level globals that would otherwise leak
        between tests (poll_once() only ever seeds once per process)
    """
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)

    state_module.state.data = {}
    state_module.state.load()

    from collections import deque

    monkeypatch.setattr(
        freehub_module,
        "_seen",
        {
            source: deque(maxlen=freehub_module._SEEN_MAXLEN)
            for source in freehub_module.SOURCES
        },
    )
    monkeypatch.setattr(freehub_module, "_seeded_from_state", False)

    return state_module.state


def _fake_project(uid, platform, title="Some project"):
    return {
        "uid": uid,
        "platform": platform,
        "title": title,
        "description": "A project description.",
        "price": "$100",
        "project_link": f"https://example.invalid/{uid}",
    }


def _install_fake_pages(monkeypatch, pages_by_source):
    """
    pages_by_source: {"kafiil": [page1_items, page2_items, ...], ...}
    fetch_projects(session, source, page=N) returns
    {"items": pages_by_source[source][N - 1]} (or empty if out of range).
    """

    async def fake_fetch_projects(session, source, page=1):
        pages = pages_by_source.get(source, [])
        if page - 1 < len(pages):
            return {"items": pages[page - 1]}
        return {"items": []}

    monkeypatch.setattr(freehub_module, "fetch_projects", fake_fetch_projects)


def test_first_ever_poll_seeds_cache_and_returns_nothing(
    isolated_freehub_state, monkeypatch
):
    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k1", "kafiil"), _fake_project("k2", "kafiil")]],
            "freelancer": [[_fake_project("f1", "freelancer")]],
        },
    )

    new_projects = asyncio.run(freehub_module.poll_once())

    assert new_projects == []
    assert set(freehub_module._seen["kafiil"]) == {"k1", "k2"}
    assert set(freehub_module._seen["freelancer"]) == {"f1"}

    # Seeded state must have actually been persisted, so a restart
    # doesn't lose it and re-treat these as brand new.
    assert set(isolated_freehub_state.get_freehub_seen("kafiil")) == {"k1", "k2"}


def test_second_poll_returns_only_new_projects(isolated_freehub_state, monkeypatch):
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project("k1", "kafiil")]], "freelancer": [[]]},
    )
    asyncio.run(freehub_module.poll_once())  # seeding poll

    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [
                [_fake_project("k2", "kafiil"), _fake_project("k1", "kafiil")]
            ],
            "freelancer": [[]],
        },
    )
    new_projects = asyncio.run(freehub_module.poll_once())

    uids = {p["uid"] for p in new_projects}
    assert uids == {"k2"}, "Only the genuinely new project should be returned"


def test_new_projects_are_tagged_with_the_fixed_poll_source(
    isolated_freehub_state, monkeypatch
):
    """
    Fix 7 regression test: identity must come from the fixed poll
    source ("kafiil"/"freelancer"), not the API's own (live, not
    guaranteed stable) "platform" field. poll_once() must tag every
    newly-returned project with "_poll_source" set to the source it
    was actually fetched under, regardless of what "platform" says.
    """
    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k0", "kafiil")]],
            "freelancer": [[]],
        },
    )
    asyncio.run(freehub_module.poll_once())  # seeding poll (seen = {k0})

    _install_fake_pages(
        monkeypatch,
        {
            # "platform" deliberately does NOT match the fetch source
            # here, simulating the live-field inconsistency the fix
            # protects against.
            "kafiil": [[_fake_project("k9", platform="SomeOtherPlatformName")]],
            "freelancer": [[]],
        },
    )
    new_projects = asyncio.run(freehub_module.poll_once())

    assert len(new_projects) == 1
    assert new_projects[0]["_poll_source"] == "kafiil"
    # The live "platform" field is preserved too (still used for
    # display in app.freehub_worker), just no longer used for identity.
    assert new_projects[0]["platform"] == "SomeOtherPlatformName"


def test_backfill_walks_additional_pages_when_first_page_fully_unseen(
    isolated_freehub_state, monkeypatch
):
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project("k1", "kafiil")]], "freelancer": [[]]},
    )
    asyncio.run(freehub_module.poll_once())  # seeding poll, seen = {k1}

    # Simulate a burst of activity: two full pages of brand new
    # projects since the last poll, followed by a page whose newest
    # item is the previously-seen k1 (page ordering is newest-first
    # per page, so k1 belongs at the end of the oldest fetched page).
    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [
                [_fake_project("k4", "kafiil"), _fake_project("k3", "kafiil")],
                [_fake_project("k2", "kafiil")],
                [_fake_project("k1", "kafiil")],  # already seen -> stop here
            ],
            "freelancer": [[]],
        },
    )
    new_projects = asyncio.run(freehub_module.poll_once())

    uids = {p["uid"] for p in new_projects}
    assert uids == {"k2", "k3", "k4"}


def test_new_project_is_not_marked_seen_until_processing_succeeds(
    isolated_freehub_state, monkeypatch
):
    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k0", "kafiil")]],
            "freelancer": [[]],
        },
    )
    asyncio.run(freehub_module.poll_once())

    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k9", "kafiil")]],
            "freelancer": [[]],
        },
    )

    new_projects = asyncio.run(freehub_module.poll_once())

    assert [p["uid"] for p in new_projects] == ["k9"]
    assert "k9" not in freehub_module._seen["kafiil"]
    assert "k9" not in isolated_freehub_state.get_freehub_seen("kafiil")

    asyncio.run(freehub_module.mark_project_seen(new_projects[0]))

    assert "k9" in freehub_module._seen["kafiil"]
    assert "k9" in isolated_freehub_state.get_freehub_seen("kafiil")


def test_failed_freehub_processing_leaves_project_eligible_for_retry(
    isolated_freehub_state, monkeypatch
):
    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k0", "kafiil")]],
            "freelancer": [[]],
        },
    )
    asyncio.run(freehub_module.poll_once())

    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [[_fake_project("k9", "kafiil")]],
            "freelancer": [[]],
        },
    )

    first = asyncio.run(freehub_module.poll_once())
    assert [p["uid"] for p in first] == ["k9"]
    assert "k9" not in freehub_module._seen["kafiil"]

    second = asyncio.run(freehub_module.poll_once())
    assert [p["uid"] for p in second] == ["k9"]


def test_mixed_seen_page_does_not_starve_older_unseen_page(isolated_freehub_state, monkeypatch):
    """A seen item on page 1 must never terminate bounded recovery."""
    monkeypatch.setattr(freehub_module, "_MAX_BACKFILL_PAGES", 2)
    _install_fake_pages(monkeypatch, {
        "kafiil": [[_fake_project("seed", "kafiil")]],
        "freelancer": [[]],
    })
    asyncio.run(freehub_module.poll_once())

    _install_fake_pages(monkeypatch, {
        "kafiil": [
            [_fake_project("new", "kafiil"), _fake_project("seed", "kafiil")],
            [_fake_project("older", "kafiil")],
        ],
        "freelancer": [[]],
    })
    projects = asyncio.run(freehub_module.poll_once())
    assert {p["uid"] for p in projects} == {"new", "older"}


def test_backfill_continuation_survives_page_cap(isolated_freehub_state, monkeypatch):
    """Hitting the page cap persists a continuation for the next poll."""
    monkeypatch.setattr(freehub_module, "_MAX_BACKFILL_PAGES", 2)
    _install_fake_pages(monkeypatch, {
        "kafiil": [[_fake_project("seed", "kafiil")]], "freelancer": [[]]
    })
    asyncio.run(freehub_module.poll_once())

    _install_fake_pages(monkeypatch, {
        "kafiil": [
            [_fake_project("n1", "kafiil")],
            [_fake_project("n2", "kafiil")],
            [_fake_project("n3", "kafiil")],
        ],
        "freelancer": [[]],
    })
    first = asyncio.run(freehub_module.poll_once())
    assert {p["uid"] for p in first} == {"n1", "n2"}
    assert asyncio.run(freehub_module.dedup.get_backfill_page("kafiil")) == 3

    second = asyncio.run(freehub_module.poll_once())
    assert {p["uid"] for p in second} >= {"n3"}


def test_failed_discovery_remains_in_durable_pending_queue(isolated_freehub_state, monkeypatch):
    _install_fake_pages(monkeypatch, {
        "kafiil": [[_fake_project("seed", "kafiil")]], "freelancer": [[]]
    })
    asyncio.run(freehub_module.poll_once())
    _install_fake_pages(monkeypatch, {
        "kafiil": [[_fake_project("retry", "kafiil")]], "freelancer": [[]]
    })
    first = asyncio.run(freehub_module.poll_once())
    assert [p["uid"] for p in first] == ["retry"]
    # Simulate processing failure: no mark_project_seen call.
    second = asyncio.run(freehub_module.poll_once())
    assert [p["uid"] for p in second].count("retry") == 1
    assert any(p["uid"] == "retry" for p in asyncio.run(freehub_module.dedup.get_pending("kafiil")))


def test_steady_state_poll_does_not_scan_full_backfill_cap(isolated_freehub_state, monkeypatch):
    """Regression test for audit finding P1-1: when nothing new has
    been posted since the last poll (page 1 is already entirely
    seen), poll_once() must not walk forward to _MAX_BACKFILL_PAGES on
    every single cycle -- that was the root cause of the reported
    ~57,600 unnecessary FreeHub requests/day. Only page 1 should be
    fetched per source in the steady state.
    """
    monkeypatch.setattr(freehub_module, "_MAX_BACKFILL_PAGES", 10)
    fetch_calls = []

    def _install_counting_pages(monkeypatch, pages_by_source):
        async def fake_fetch_projects(session, source, page=1):
            fetch_calls.append((source, page))
            pages = pages_by_source.get(source, [])
            if page - 1 < len(pages):
                return {"items": pages[page - 1]}
            return {"items": []}

        monkeypatch.setattr(freehub_module, "fetch_projects", fake_fetch_projects)

    _install_counting_pages(
        monkeypatch,
        {"kafiil": [[_fake_project("k1", "kafiil")]], "freelancer": [[]]},
    )
    asyncio.run(freehub_module.poll_once())  # seeding poll
    fetch_calls.clear()

    # Second poll: nothing new. If a real backend genuinely has 10+
    # pages of (all already-seen) history, the old code would still
    # fetch all 10 pages here on every cycle.
    _install_counting_pages(
        monkeypatch,
        {"kafiil": [[_fake_project("k1", "kafiil")]], "freelancer": [[]]},
    )
    new_projects = asyncio.run(freehub_module.poll_once())

    assert new_projects == []
    kafiil_calls = [page for source, page in fetch_calls if source == "kafiil"]
    assert kafiil_calls == [1], (
        f"expected exactly one FreeHub request (page 1) when nothing is "
        f"new, got {kafiil_calls}"
    )


def test_mixed_seen_page_does_not_stop_after_page_one_if_partially_new(
    isolated_freehub_state, monkeypatch
):
    """A page that mixes new and already-seen projects must not be
    mistaken for a fully-caught-up page -- only a page with zero new
    items should stop pagination (see _page_fully_seen).
    """
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project("k1", "kafiil")]], "freelancer": [[]]},
    )
    asyncio.run(freehub_module.poll_once())  # seed: seen = {k1}

    _install_fake_pages(
        monkeypatch,
        {
            "kafiil": [
                [_fake_project("k2", "kafiil"), _fake_project("k1", "kafiil")],
                [_fake_project("k0", "kafiil")],
            ],
            "freelancer": [[]],
        },
    )
    new_projects = asyncio.run(freehub_module.poll_once())
    uids = {p["uid"] for p in new_projects}
    assert uids == {"k2", "k0"}


def test_integer_uids_are_normalized_to_strings_for_dedup(
    isolated_freehub_state, monkeypatch
):
    """
    Regression test for the uid-type mismatch: discovery, pending dedup
    and _page_fully_seen all normalize uids to str(), but seeding and
    mark_project_seen compared/inserted the raw value. A source that
    returns integer uids therefore never matched the seen set -- every
    project was re-discovered and re-queued on every poll, and the
    fully-seen pagination stop condition never fired. All uids must be
    normalized to str in every path.
    """
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project(777001, "kafiil")]], "freelancer": [[]]},
    )
    asyncio.run(freehub_module.poll_once())  # seeding poll
    assert set(freehub_module._seen["kafiil"]) == {"777001"}
    assert set(isolated_freehub_state.get_freehub_seen("kafiil")) == {"777001"}

    # The same integer uid comes back on the next poll: it must be
    # treated as already seen (not re-discovered), exactly like a str
    # uid would be.
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project(777001, "kafiil")]], "freelancer": [[]]},
    )
    new_projects = asyncio.run(freehub_module.poll_once())
    assert new_projects == [], (
        "an already-seen project whose uid comes back as an integer "
        "must not be re-discovered"
    )

    # A brand-new integer-uid project must be marked seen (and
    # persisted) under its str() form, never as a raw int.
    _install_fake_pages(
        monkeypatch,
        {"kafiil": [[_fake_project(777002, "kafiil")]], "freelancer": [[]]},
    )
    discovered = asyncio.run(freehub_module.poll_once())
    assert [p["uid"] for p in discovered] == [777002]
    assert "777002" not in freehub_module._seen["kafiil"]

    asyncio.run(freehub_module.mark_project_seen(discovered[0]))

    assert "777002" in freehub_module._seen["kafiil"]
    assert "777002" in isolated_freehub_state.get_freehub_seen("kafiil")
    assert 777002 not in freehub_module._seen["kafiil"], (
        "the raw integer form must not pollute the seen set"
    )


def test_missing_uid_project_is_skipped_by_mark_project_seen(
    isolated_freehub_state, monkeypatch
):
    """mark_project_seen must not KeyError (or store a bogus
    "None"/"" uid) when the API omits the uid -- discovery already
    skips such projects, so marking must be a harmless no-op for them.
    """
    asyncio.run(freehub_module.mark_project_seen({"_poll_source": "kafiil"}))
    assert list(freehub_module._seen["kafiil"]) == []
