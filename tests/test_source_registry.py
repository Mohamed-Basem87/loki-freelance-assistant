import asyncio
import json
from pathlib import Path

import pytest

from app.adapters.sources.registry import active_ids, build, register
from app.adapters.sources.freehub import FreeHubJobSource


def test_build_uses_composition_registered_factory():
    source = object()
    register("test_source", lambda **kw: source)
    assert build("test_source") is source


def test_build_without_registered_factory_raises():
    with pytest.raises(KeyError, match="no registered factory"):
        build("unregistered_source")


def test_source_normalization_preserves_single_identity():
    source = FreeHubJobSource(
        poller=lambda: [],
        marker=lambda job: None,
        http_client=object(),
    )
    project = {
        "uid": "abc",
        "_poll_source": "kafiil",
        "title": "T",
        "description": "D",
        "platform": "Kafiil",
        "price": "$1",
        "project_link": "https://example.invalid/abc",
    }
    normalized = source.normalize(project)
    assert normalized["job_id"] == "abc"
    assert normalized["identity_source"] == "kafiil"


def test_job_sources_env_selector_is_authoritative(monkeypatch):
    import importlib
    import app.runtime_config as rc
    monkeypatch.setenv("JOB_SOURCES", "freehub")
    reloaded = importlib.reload(rc)
    try:
        assert tuple(cfg.id for cfg in reloaded.JOB_SOURCES) == ("freehub",)
        assert active_ids() == ("freehub",)
    finally:
        monkeypatch.delenv("JOB_SOURCES", raising=False)
        importlib.reload(rc)


def test_job_sources_env_selector_rejects_unknown_source(monkeypatch):
    import importlib
    import app.runtime_config as rc
    monkeypatch.setenv("JOB_SOURCES", "does_not_exist")
    with pytest.raises(ValueError, match="JOB_SOURCES"):
        importlib.reload(rc)
    monkeypatch.delenv("JOB_SOURCES", raising=False)
    importlib.reload(rc)


def test_freehub_adapter_id_is_not_an_upstream_source():
    from app.runtime_config import SOURCES
    assert "freehub" not in {profile.id for profile in SOURCES}