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
