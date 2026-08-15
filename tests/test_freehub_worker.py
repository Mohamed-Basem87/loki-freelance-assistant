import asyncio

import pytest

import app.freehub_worker as worker


def _project(uid="p1"):
    return {
        "uid": uid,
        "_poll_source": "kafiil",
        "title": "Power BI job",
        "description": "Analyze sales data.",
        "platform": "kafiil",
        "price": "$100",
        "project_link": "https://example.invalid/p1",
    }


def test_worker_does_not_mark_failed_project_seen(monkeypatch):
    calls = {"process": 0, "mark": 0}

    async def fake_poll_once():
        return [_project()]

    async def fake_process_job(**kwargs):
        calls["process"] += 1
        raise RuntimeError("temporary processing failure")

    async def fake_mark_project_seen(project):
        calls["mark"] += 1

    async def fake_logger_run(*args, **kwargs):
        return None

    async def stop_after_one_poll(_):
        raise StopAsyncIteration

    monkeypatch.setattr(worker, "poll_once", fake_poll_once)
    monkeypatch.setattr(worker, "process_job", fake_process_job)
    monkeypatch.setattr(worker, "mark_project_seen", fake_mark_project_seen)
    monkeypatch.setattr(worker.logger, "run", fake_logger_run)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_one_poll)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(worker.freehub_worker())

    assert calls == {"process": 1, "mark": 0}


def test_worker_marks_project_seen_after_success(monkeypatch):
    calls = {"process": 0, "mark": 0}

    async def fake_poll_once():
        return [_project()]

    async def fake_process_job(**kwargs):
        calls["process"] += 1

    async def fake_mark_project_seen(project):
        calls["mark"] += 1

    async def stop_after_one_poll(_):
        raise StopAsyncIteration

    monkeypatch.setattr(worker, "poll_once", fake_poll_once)
    monkeypatch.setattr(worker, "process_job", fake_process_job)
    monkeypatch.setattr(worker, "mark_project_seen", fake_mark_project_seen)
    monkeypatch.setattr(worker.asyncio, "sleep", stop_after_one_poll)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(worker.freehub_worker())

    assert calls == {"process": 1, "mark": 1}
