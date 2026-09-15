"""Cross-source identity/dedup boundary tests (Business Node).

Restores the audit's subsystem-boundary coverage that existed before the
refactor (E:\\freelance-assistant\\tests\\test_cross_source_identity.py):
whether the SAME underlying project, arriving through different call
shapes -- a second Telegram message, a second FreeHub poll, or the same
project surfacing through Telegram AND FreeHub -- is recognized as a
duplicate end-to-end through app.services.job_processor.process_job(),
not just at the _make_job_uuid() unit level.

The durable job rows live in the in-memory MemRepository (Postgres
adapter semantics) while the cross-source claim uses a real
StateManager / JsonStateStore over an isolated state.json -- the same
two real mechanisms production relies on. `project_link` uses the
supported <platform-domain>/project/<numeric-id> shape
_extract_project_id() actually recognizes.
"""
import asyncio

import pytest

import app.services.job_processor as jp
import app.services.state as state_module
from app.adapters.state.json import JsonStateStore
from app.services.state import StateManager

from tests._business_node_fakes import MemRepository, RecordingPublisher, allow_all, noop_resolver, PassThroughShortener

REJECT_TEXT = "Need someone to create SQL queries for a reporting system."


@pytest.fixture()
def isolated_dedup(tmp_path, monkeypatch):
    """Point the shared state module at a throwaway state.json and bind
    a fresh StateManager-backed DedupStore, so cross-source claims from
    one test can never leak into the next."""
    fake_state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_FILE", fake_state_file)
    manager = StateManager()
    manager.load()
    return JsonStateStore(manager)


@pytest.fixture()
def pipeline(isolated_dedup, monkeypatch):
    """Fresh in-memory repository + no-op side effects, wired onto the
    job_processor module the way the composition root wires production."""
    repo = MemRepository()
    publisher = RecordingPublisher()

    async def _allow_all(payload):
        return True

    monkeypatch.setattr(jp, "logger", repo)
    monkeypatch.setattr(jp, "dedup", isolated_dedup)
    monkeypatch.setattr(jp, "stream_publisher", publisher)
    monkeypatch.setattr(jp, "resolver", noop_resolver)
    monkeypatch.setattr(jp, "guard_allow", _allow_all)
    monkeypatch.setattr(jp, "url_shortener", PassThroughShortener())

    return repo, publisher


def _telegram_job(url="", title=REJECT_TEXT):
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": "Test Telegram Channel",
        "url": url,
        "budget": "",
    }


def _freehub_job(url="", title=REJECT_TEXT, platform="mostaql"):
    return {
        "title": title,
        "description": "",
        "raw_text": title,
        "source": platform,
        "url": url,
        "budget": "",
    }


def _run(job, job_id, identity_source):
    asyncio.run(
        jp.process_job(job=job, job_id=job_id, identity_source=identity_source)
    )


def test_same_telegram_message_processed_twice_is_deduplicated(pipeline):
    repo, _ = pipeline
    job = _telegram_job()

    _run(job, "55501", "-100701")
    _run(job, "55501", "-100701")

    assert len(repo.rows) == 1
    assert repo.rows[jp._make_job_uuid("-100701", "55501")] is not None


def test_same_freehub_project_polled_twice_is_deduplicated(pipeline):
    repo, _ = pipeline
    job = _freehub_job(url="https://mostaql.com/project/40001")

    _run(job, "uid-40001", "mostaql")
    _run(job, "uid-40001", "mostaql")

    assert len(repo.rows) == 1
    assert repo.rows[jp._make_job_uuid("mostaql", "uid-40001")] is not None


def test_same_project_from_telegram_and_freehub_cross_source_dedup(pipeline):
    """A project posted to a monitored channel AND polled from FreeHub
    has two different job_uuids (different identity_source/job_id pairs);
    the numeric project id claimed atomically via StateManager is the
    only thing that ties them together."""
    repo, _ = pipeline

    project_url = "https://mostaql.com/project/778899"
    telegram_uuid = jp._make_job_uuid("-100702", "66601")
    freehub_uuid = jp._make_job_uuid("mostaql", "uid-778899")

    _run(_telegram_job(url=project_url), "66601", "-100702")
    _run(_freehub_job(url=project_url), "uid-778899", "mostaql")

    assert set(repo.rows) == {telegram_uuid, freehub_uuid}, (
        "two distinct identities must produce two distinct durable rows"
    )
    assert repo.rows[freehub_uuid]["Final Decision"] == "Rejected"
    assert (
        repo.rows[freehub_uuid]["Decision Reason"]
        == "Duplicate project from another source"
    )
    # The first job must not have been touched by the duplicate.
    assert (
        repo.rows[telegram_uuid]["Decision Reason"]
        != "Duplicate project from another source"
    )


def test_same_project_reversed_order_freehub_then_telegram(pipeline):
    """Same scenario with the arrival order reversed, to confirm the
    claim is not order- or source-dependent."""
    repo, _ = pipeline

    project_url = "https://mostaql.com/project/990011"
    telegram_uuid = jp._make_job_uuid("-100703", "77701")
    freehub_uuid = jp._make_job_uuid("mostaql", "uid-990011")

    _run(_freehub_job(url=project_url), "uid-990011", "mostaql")
    _run(_telegram_job(url=project_url), "77701", "-100703")

    assert set(repo.rows) == {telegram_uuid, freehub_uuid}
    assert repo.rows[telegram_uuid]["Final Decision"] == "Rejected"
    assert (
        repo.rows[telegram_uuid]["Decision Reason"]
        == "Duplicate project from another source"
    )


def test_accepted_first_job_survives_subsequent_duplicate(pipeline):
    """The cross-source claim is on the numeric project id only: an
    accepted first arrival keeps its full workflow and is marked
    Complete even when a duplicate from the other source arrives
    afterwards."""
    repo, publisher = pipeline

    project_url = "https://mostaql.com/project/314159"
    telegram_uuid = jp._make_job_uuid("-100704", "88801")
    freehub_uuid = jp._make_job_uuid("mostaql", "uid-314159")

    title = "Power BI Dashboard Needed"
    description = "Need a Power BI dashboard built from sales data."
    _run(
        {
            "title": title,
            "description": description,
            "raw_text": f"{title}\n\n{description}",
            "source": "Test Telegram Channel",
            "url": project_url,
            "budget": "",
        },
        "88801",
        "-100704",
    )
    _run(_freehub_job(url=project_url), "uid-314159", "mostaql")

    assert repo.rows[telegram_uuid]["Final Decision"] == "Accepted"
    assert repo.rows[telegram_uuid]["Notification Status"] == "Complete"
    assert len(publisher.events) == 1
    assert publisher.events[0]["Job UUID"] == telegram_uuid

    assert repo.rows[freehub_uuid]["Final Decision"] == "Rejected"
    assert len(publisher.events) == 1, "the duplicate must never be published"