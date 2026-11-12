"""Tests for FreeHub placeholder-description recovery.

FreeHub sometimes returns a Kafiil project's client name, price, title,
or an empty string in the ``description`` field.  The recovery adapter
must detect those values, consult the project-detail endpoint, and fall
back to the public Kafiil page before the job is persisted.
"""

import asyncio

import pytest

from app.adapters.sources.freehub import FreeHubApiClient, FreeHubJobSource
from app.adapters.sources.freehub_recovery import (
    FreeHubDescriptionRecovery,
    extract_kafiil_description,
    looks_suspicious,
)


REAL_DESCRIPTION = (
    "أبحث عن مستقل يجيد العربية والبحث المتقدم في منصة X، لجمع عينة "
    "تجريبية من 10 تغريدات أصلية منشورة بالفعل، لأغراض بحثية."
)


def _project(**overrides):
    project = {
        "uid": "kafiil:25279",
        "_poll_source": "kafiil",
        "title": "جمع وتوثيق تغريدات للشباب",
        "description": "Nouf Mohamed",
        "price": "$50 - $100",
        "project_link": "https://kafiil.com/project/25279",
    }
    project.update(overrides)
    return project


class FakeClient:
    def __init__(self, *, detail=None, page="", detail_error=None, page_error=None):
        self.detail = detail
        self.page = page
        self.detail_error = detail_error
        self.page_error = page_error
        self.detail_calls = []
        self.page_calls = []

    async def fetch_project(self, uid):
        self.detail_calls.append(uid)
        if self.detail_error is not None:
            raise self.detail_error
        return self.detail

    async def fetch_text(self, url, *, headers=None):
        self.page_calls.append(url)
        if self.page_error is not None:
            raise self.page_error
        return self.page


def _page_with_description(text):
    return (
        '<div class="block"><div class="block-head">'
        '<p class="title">تفاصيل المشروع</p></div>'
        f'<div class="has-padding"><p class="">{text}</p></div></div>'
    )


def test_looks_suspicious_flags_empty_names_prices_and_titles():
    assert looks_suspicious(_project(description=""))
    assert looks_suspicious(_project(description="Nouf Mohamed"))
    assert looks_suspicious(_project(description="$50 - $100"))
    assert looks_suspicious(_project(description="جمع وتوثيق تغريدات للشباب"))
    assert not looks_suspicious(_project(description=REAL_DESCRIPTION))


def test_extract_kafiil_description_converts_breaks_and_entities():
    page = _page_with_description("first line<br />second &amp; third")
    assert extract_kafiil_description(page) == "first line\nsecond & third"


def test_extract_kafiil_description_returns_empty_without_marker():
    assert extract_kafiil_description("<html><p>no description here</p></html>") == ""


def test_detail_endpoint_recovers_client_name_description():
    real = REAL_DESCRIPTION * 2  # long enough to skip the live-page request
    client = FakeClient(
        detail={
            "description": real,
            "client": {"name": "Nouf Mohamed"},
        }
    )

    project = _project()
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == real
    assert project["raw_text"] == f"{project['title']}\n\n{real}"
    assert project["_description_recovered_from"] == "api_detail"
    assert client.detail_calls == ["kafiil:25279"]
    assert client.page_calls == []


def test_live_page_recovers_when_detail_still_returns_client_name():
    client = FakeClient(
        detail={
            "description": "Nouf Mohamed",
            "client": {"name": "Nouf Mohamed"},
            "links": {"html": "https://kafiil.com/project/25279"},
        },
        page=_page_with_description(REAL_DESCRIPTION),
    )

    project = _project()
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == REAL_DESCRIPTION
    assert project["_description_recovered_from"] == "live_page"
    assert client.page_calls == ["https://kafiil.com/project/25279"]


def test_live_page_recovers_budget_placeholder():
    client = FakeClient(
        detail={
            "description": "$50 - $100",
            "budget": {"min": 50, "max": 100, "currency": "USD"},
            "client": {"name": "Someone"},
        },
        page=_page_with_description(REAL_DESCRIPTION),
    )

    project = _project(description="$50 - $100", price="غير محدد")
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == REAL_DESCRIPTION
    assert project["_description_recovered_from"] == "live_page"


def test_live_page_wins_when_detail_is_only_a_short_excerpt():
    client = FakeClient(
        detail={"description": "short excerpt", "client": {"name": "Someone"}},
        page=_page_with_description(REAL_DESCRIPTION),
    )

    project = _project(description="short excerpt")
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == REAL_DESCRIPTION
    assert project["_description_recovered_from"] == "live_page"


def test_recovery_failure_leaves_project_unchanged():
    client = FakeClient(
        detail_error=RuntimeError("detail down"),
        page_error=RuntimeError("page down"),
    )

    project = _project()
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == "Nouf Mohamed"
    assert "_description_recovered_from" not in project


def test_short_real_description_is_not_replaced_by_a_shorter_fallback():
    short_real = "مطلوب فديو اديتور للعمل على فديو 3 دقائق"
    client = FakeClient(
        detail={"description": "editing", "client": {"name": "Someone"}},
        page=_page_with_description("editing"),
    )

    project = _project(description=short_real)
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == short_real
    assert "_description_recovered_from" not in project


def test_live_page_recovers_when_detail_repeats_original_without_client_name():
    client = FakeClient(
        # No client object: the detail text is identical to the suspected
        # placeholder, so it must not be accepted as "real".
        detail={"description": "Nouf Mohamed"},
        page=_page_with_description(REAL_DESCRIPTION),
    )

    project = _project()
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == REAL_DESCRIPTION
    assert project["_description_recovered_from"] == "live_page"


def test_non_kafiil_sources_are_untouched():
    client = FakeClient(detail={"description": REAL_DESCRIPTION})

    project = _project(_poll_source="mostaql", platform="mostaql")
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == "Nouf Mohamed"
    assert client.detail_calls == []
    assert client.page_calls == []


def test_long_descriptions_do_not_trigger_extra_requests():
    client = FakeClient(detail={"description": "ignored"})

    project = _project(description=REAL_DESCRIPTION)
    asyncio.run(FreeHubDescriptionRecovery(client).enrich_all([project]))

    assert project["description"] == REAL_DESCRIPTION
    assert client.detail_calls == []
    assert client.page_calls == []


def test_source_poll_applies_recovery_before_normalize():
    class FakeRecovery:
        async def enrich_all(self, projects):
            projects[0]["description"] = REAL_DESCRIPTION
            return projects

    async def fake_poll():
        return [_project()]

    source = FreeHubJobSource(
        poller=fake_poll,
        marker=lambda project: None,
        http_client=object(),
        description_recovery=FakeRecovery(),
    )

    projects = asyncio.run(source.poll())
    normalized = source.normalize(projects[0])

    assert normalized["description"] == REAL_DESCRIPTION
    assert normalized["raw_text"] == f"{projects[0]['title']}\n\n{REAL_DESCRIPTION}"


def test_api_client_derives_project_detail_url_and_fetches_text():
    class FakeResponse:
        def __init__(self, text):
            self._text = text
            self.closed = False

        async def text(self):
            return self._text

        async def aclose(self):
            self.closed = True

    class FakeTransport:
        def __init__(self):
            self.urls = []
            self.response = FakeResponse("<html>ok</html>")

        async def get_json(self, url, *, timeout=None):
            self.urls.append(url)
            return {"item": {"uid": "kafiil:1"}}

        async def request(self, method, url, *, headers=None, json=None, timeout=None):
            self.urls.append(url)
            return self.response

    transport = FakeTransport()
    client = FreeHubApiClient(
        base_url="http://api.example/v1/users",
        user_id="42",
        transport=transport,
    )

    assert asyncio.run(client.fetch_project("kafiil:1")) == {
        "item": {"uid": "kafiil:1"}
    }
    assert transport.urls[-1] == "http://api.example/v1/projects/kafiil:1"

    text = asyncio.run(client.fetch_text("https://kafiil.com/project/1"))
    assert text == "<html>ok</html>"
    assert transport.response.closed is True
    assert transport.urls[-1] == "https://kafiil.com/project/1"
