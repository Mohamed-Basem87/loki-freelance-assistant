"""Regression tests for the aiohttp transport lifecycle.

Audit finding: AioHttpTransport previously opened and closed a brand
new aiohttp.ClientSession on every single request() call, discarding
connection pooling/keep-alive and adding needless handshake overhead to
a process that polls the same host repeatedly for its whole lifetime.
"""
import asyncio

import pytest

from app.adapters.http.aiohttp import AioHttpTransport


class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {"ok": True}
        self.released = False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    def release(self):
        self.released = True


class _FakeSession:
    """Stands in for aiohttp.ClientSession to assert it is reused."""

    def __init__(self):
        self.closed = False
        self.request_count = 0
        self.responses = []

    async def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.request_count += 1
        response = _FakeResponse()
        self.responses.append(response)
        return response

    async def close(self):
        self.closed = True


def test_transport_reuses_one_session_across_requests(monkeypatch):
    transport = AioHttpTransport(timeout=5)
    fake_session = _FakeSession()
    monkeypatch.setattr(
        "app.adapters.http.aiohttp.aiohttp.ClientSession",
        lambda timeout=None: fake_session,
    )

    async def scenario():
        r1 = await transport.request("GET", "https://example.invalid/a")
        r2 = await transport.request("GET", "https://example.invalid/b")
        r3 = await transport.request("GET", "https://example.invalid/c")
        return r1, r2, r3

    asyncio.run(scenario())

    assert fake_session.request_count == 3, (
        "three requests were issued but the underlying session must be "
        "the same object each time, not recreated per request"
    )
    assert not fake_session.closed


def test_transport_creates_session_lazily_and_only_once_under_concurrency(monkeypatch):
    transport = AioHttpTransport(timeout=5)
    created = {"count": 0}
    fake_session = _FakeSession()

    def _factory(timeout=None):
        created["count"] += 1
        return fake_session

    monkeypatch.setattr(
        "app.adapters.http.aiohttp.aiohttp.ClientSession", _factory
    )

    async def scenario():
        assert transport._session is None, "session must not exist before first use"
        await asyncio.gather(
            transport.request("GET", "https://example.invalid/x"),
            transport.request("GET", "https://example.invalid/y"),
            transport.request("GET", "https://example.invalid/z"),
        )

    asyncio.run(scenario())
    assert created["count"] == 1, (
        "concurrent first-use requests must not each create their own session"
    )


def test_transport_close_is_idempotent_and_rejects_further_requests(monkeypatch):
    transport = AioHttpTransport(timeout=5)
    fake_session = _FakeSession()
    monkeypatch.setattr(
        "app.adapters.http.aiohttp.aiohttp.ClientSession",
        lambda timeout=None: fake_session,
    )

    async def scenario():
        await transport.request("GET", "https://example.invalid/a")
        await transport.close()
        await transport.close()  # must not raise
        with pytest.raises(RuntimeError):
            await transport.request("GET", "https://example.invalid/b")

    asyncio.run(scenario())
    assert fake_session.closed


def test_get_json_releases_the_response_after_reading():
    transport = AioHttpTransport.__new__(AioHttpTransport)
    from app.adapters.http.aiohttp import AioHttpResponse

    fake = _FakeResponse(payload={"hello": "world"})
    wrapped = AioHttpResponse(fake)

    async def scenario():
        return await wrapped.json()

    result = asyncio.run(scenario())
    assert result == {"hello": "world"}
    assert fake.released


def test_status_only_caller_can_release_explicitly():
    """A caller that inspects only .status / raise_for_status() and never
    reads the body must be able to return the connection explicitly via
    .release() / .aclose() -- otherwise that connection leaks until GC."""
    from app.adapters.http.aiohttp import AioHttpResponse

    fake = _FakeResponse(status=200)
    wrapped = AioHttpResponse(fake)

    async def scenario():
        assert wrapped.status == 200
        wrapped.raise_for_status()
        # Never read the body -- instead release explicitly.
        await wrapped.aclose()

    asyncio.run(scenario())
    assert fake.released, "status-only caller must release the connection"


def test_release_is_idempotent_and_safe_after_a_body_read():
    """release()/aclose() must be harmless to call multiple times and
    after a body read (which already auto-releases) -- never a double
    release of the underlying response."""
    from app.adapters.http.aiohttp import AioHttpResponse

    fake = _FakeResponse(payload={"ok": True})
    wrapped = AioHttpResponse(fake)

    async def scenario():
        await wrapped.json()  # auto-release on body read
        wrapped.release()
        await wrapped.aclose()
        wrapped.release()

    asyncio.run(scenario())
    assert fake.released


def test_body_read_still_releases_even_when_decode_raises():
    """A JSON decode failure must not leak the connection: release is in
    a finally, so the response is returned to the pool regardless."""
    from app.adapters.http.aiohttp import AioHttpResponse

    class _BadJsonResponse(_FakeResponse):
        async def json(self):
            raise ValueError("bad json")

    fake = _BadJsonResponse()
    wrapped = AioHttpResponse(fake)

    async def scenario():
        with pytest.raises(ValueError):
            await wrapped.json()

    asyncio.run(scenario())
    assert fake.released, (
        "a body/decode error must still release the connection so the "
        "shared session does not accumulate leaked responses"
    )


def test_get_json_releases_even_on_decode_failure(monkeypatch):
    """HttpTransport.get_json must guarantee the response is released
    even when response.json() raises (it previously leaked for any
    caller that did not consume the body themselves)."""
    from app.ports import HttpTransport

    released = {"flag": False}

    class _BrokenResponse:
        def __init__(self):
            self.aclose_called = False

        async def json(self):
            raise ValueError("decode failed")

        async def aclose(self):
            self.aclose_called = True
            released["flag"] = True

    class _Transport(HttpTransport):
        async def request(self, method, url, *, headers=None, timeout=None):
            return _BrokenResponse()

        async def close(self):
            pass

    async def scenario():
        with pytest.raises(ValueError):
            await _Transport().get_json("https://example.invalid/x")

    asyncio.run(scenario())
    assert released["flag"] is True
