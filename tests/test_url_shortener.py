"""Unit tests for app.url_shortener.UrlShortenerService.

The service depends only on the HttpTransport port contract (see
app.ports.HttpTransport / app.adapters.http.aiohttp.AioHttpTransport):
request() returns a response with an async .json(), or raises on a
non-2xx status with a `.status` attribute set -- exactly what
aiohttp.ClientResponseError provides. _FakeTransport below mirrors that
contract without any real network I/O.
"""
import asyncio

import pytest

from app.url_shortener import (
    UrlAlreadyExistsError,
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


def _service(transport, failure_mode="fail_open", duplicate_status=409):
    return UrlShortenerService(
        transport=transport,
        domain="http://shortener.test",
        endpoint="/shorten",
        failure_mode=failure_mode,
        duplicate_status=duplicate_status,
    )


def test_shorten_success_posts_original_url_and_returns_shortened_url():
    transport = _FakeTransport({"url": "http://shortener.test/abc123"})
    service = _service(transport)

    result = asyncio.run(service.shorten("https://example.com/project/555"))

    assert result == "http://shortener.test/abc123"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://shortener.test/shorten"
    assert call["json"] == {"url": "https://example.com/project/555"}


def test_domain_and_endpoint_slashes_are_normalized():
    transport = _FakeTransport({"url": "short"})
    service = UrlShortenerService(
        transport=transport,
        domain="http://shortener.test/",
        endpoint="shorten",
        failure_mode="fail_open",
        duplicate_status=409,
    )

    asyncio.run(service.shorten("https://example.com/x"))

    assert transport.calls[0]["url"] == "http://shortener.test/shorten"


def test_duplicate_status_raises_regardless_of_failure_mode():
    for mode in ("fail_open", "fail_closed"):
        transport = _FakeTransport(_FakeStatusError(409))
        service = _service(transport, failure_mode=mode)

        with pytest.raises(UrlAlreadyExistsError) as excinfo:
            asyncio.run(service.shorten("https://example.com/dupe"))

        assert excinfo.value.original_url == "https://example.com/dupe"


def test_duplicate_status_is_configurable():
    transport = _FakeTransport(_FakeStatusError(410))
    service = _service(transport, duplicate_status=410)

    with pytest.raises(UrlAlreadyExistsError):
        asyncio.run(service.shorten("https://example.com/dupe"))


def test_fail_open_returns_original_url_on_generic_failure():
    transport = _FakeTransport(_FakeStatusError(500))
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("https://example.com/keep-me"))

    assert result == "https://example.com/keep-me"


def test_fail_closed_raises_on_generic_failure():
    transport = _FakeTransport(_FakeStatusError(500))
    service = _service(transport, failure_mode="fail_closed")

    with pytest.raises(UrlShorteningError):
        asyncio.run(service.shorten("https://example.com/x"))


def test_fail_open_returns_original_url_on_timeout_style_exception():
    """A timeout/connection error has no `.status` attribute at all --
    getattr(exc, "status", None) must not blow up, and it must not be
    mistaken for the duplicate signal."""
    transport = _FakeTransport(TimeoutError("connect timed out"))
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("https://example.com/timeout"))

    assert result == "https://example.com/timeout"


def test_fail_closed_raises_on_missing_url_field_in_response():
    transport = _FakeTransport({"unexpected": "shape"})
    service = _service(transport, failure_mode="fail_closed")

    with pytest.raises(UrlShorteningError):
        asyncio.run(service.shorten("https://example.com/x"))


def test_fail_open_falls_back_on_missing_url_field_in_response():
    transport = _FakeTransport({"unexpected": "shape"})
    service = _service(transport, failure_mode="fail_open")

    result = asyncio.run(service.shorten("https://example.com/x"))

    assert result == "https://example.com/x"


def test_unknown_failure_mode_is_rejected_at_construction():
    with pytest.raises(ValueError):
        _service(_FakeTransport({"url": "x"}), failure_mode="retry_forever")
