"""Unit tests for app.url_shortener.UrlShortenerService.

The service depends only on the HttpTransport port contract (see
app.ports.HttpTransport / app.adapters.http.aiohttp.AioHttpTransport):
request() returns a response with an async .json(), or raises on a
non-2xx status -- exactly what aiohttp.ClientResponseError provides.
_FakeTransport below mirrors that contract without any real network I/O.
"""
import asyncio

import pytest

from app.url_shortener import (
    UrlShorteningError,
    UrlShortenerService,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeStatusError(Exception):
    """Mirrors aiohttp.ClientResponseError's `.status` attribute."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class _FakeTransport:
    """Records calls; `outcome` selects what request() does:
    - a dict -> succeeds with that dict as the JSON body
    - an Exception instance -> raised, mimicking a non-2xx/timeout
    """

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _FakeResponse(self.outcome)


def _service(transport, failure_mode="fail_open"):
    return UrlShortenerService(
        transport=transport,
        domain="http://shortener.test",
        endpoint="/links",
        failure_mode=failure_mode,
    )


def test_shorten_success_posts_job_id_and_link_and_returns_full_shortened_url():
    transport = _FakeTransport({"path": "/abc123"})
    service = _service(transport)

    result = asyncio.run(service.shorten("abc123", "https://example.com/project/555"))

    assert result == "http://shortener.test/abc123"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://shortener.test/links"
    assert call["json"] == {
        "jobId": "abc123",
        "job_link": "https://example.com/project/555",
    }


def test_domain_and_endpoint_slashes_are_normalized():
    transport = _FakeTransport({"path": "short"})
    service = UrlShortenerService(
        transport=transport,
        domain="http://shortener.test/",
        endpoint="links",
        failure_mode="fail_open",
    )

    result = asyncio.run(service.shorten("short", "https://example.com/x"))

    assert transport.calls[0]["url"] == "http://shortener.test/links"
    assert result == "http://shortener.test/short"


def test_repeated_job_id_returns_the_existing_path_without_error():
    """The shortener upserts by jobId -- a re-shorten of the same jobId
    is not a conflict, it just returns the same path again."""
    transport = _FakeTransport({"path": "/dupe"})
    service = _service(transport)

    first = asyncio.run(service.shorten("dupe", "https://example.com/dupe"))
    second = asyncio.run(service.shorten("dupe", "https://example.com/dupe"))

    assert first == second == "http://shortener.test/dupe"


def test_fail_open_returns_original_url_on_generic_failure():
    transport = _FakeTransport(_FakeStatusError(400))
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("job-1", "https://example.com/keep-me"))

    assert result == "https://example.com/keep-me"


def test_fail_closed_raises_on_generic_failure():
    transport = _FakeTransport(_FakeStatusError(400))
    service = _service(transport, failure_mode="fail_closed")

    with pytest.raises(UrlShorteningError):
        asyncio.run(service.shorten("job-1", "https://example.com/x"))


def test_fail_open_returns_original_url_on_timeout_style_exception():
    transport = _FakeTransport(TimeoutError("connect timed out"))
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("job-1", "https://example.com/timeout"))

    assert result == "https://example.com/timeout"


def test_fail_closed_raises_on_missing_path_field_in_response():
    transport = _FakeTransport({"unexpected": "shape"})
    service = _service(transport, failure_mode="fail_closed")

    with pytest.raises(UrlShorteningError):
        asyncio.run(service.shorten("job-1", "https://example.com/x"))


def test_fail_open_falls_back_on_missing_path_field_in_response():
    transport = _FakeTransport({"unexpected": "shape"})
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("job-1", "https://example.com/x"))

    assert result == "https://example.com/x"


def test_unknown_failure_mode_is_rejected_at_construction():
    with pytest.raises(ValueError):
        _service(_FakeTransport({"path": "x"}), failure_mode="retry_forever")
