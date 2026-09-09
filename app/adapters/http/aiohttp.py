"""aiohttp HTTP transport adapter.

Owns a single reusable aiohttp.ClientSession for its lifetime instead of
opening and closing a new session on every request. The previous
implementation created a brand-new ClientSession (and therefore a fresh
TCP/TLS handshake) for every single call to request(), including every
FreeHub poll -- discarding connection pooling/keep-alive entirely and
adding needless latency and file-descriptor churn to a process that
polls the same host repeatedly for its whole lifetime.
"""
import asyncio

import aiohttp

from app.ports import HttpTransport


class AioHttpResponse:
    """Thin wrapper so callers depend only on the HttpTransport contract.

    The underlying aiohttp response/connection is released back to the
    pool exactly once. Reading the body via .json()/.text()/.read()
    releases it automatically (with a finally, so it is released even
    when the body read or JSON decode raises). A caller that only
    inspects `.status` / `raise_for_status()` and never consumes the
    body must explicitly call .release() (or await .aclose()) to return
    the connection to the pool -- see HttpTransport.get_json, which
    always ensures release. release()/aclose() are idempotent, so a
    released response is safe to release again (e.g. after a body read).
    """

    def __init__(self, response):
        self._response = response
        self._released = False

    def _release(self):
        if not self._released:
            self._released = True
            self._response.release()

    @property
    def status(self):
        return self._response.status

    def raise_for_status(self):
        return self._response.raise_for_status()

    def release(self):
        """Explicitly release the underlying connection back to the pool.

        Safe to call any number of times and after a body read (a no-op
        once released). This is the lifecycle hook for a caller that only
        inspected `.status` / `raise_for_status()` and never consumed the
        body -- without it the connection would be returned to the pool
        only via garbage collection, leaking file descriptors on the
        long-lived shared session.
        """
        self._release()

    async def aclose(self):
        """Async alias for explicit release; mirrors the JobSource/
        HttpTransport close() lifecycle convention."""
        self._release()

    async def json(self):
        try:
            return await self._response.json()
        finally:
            self._release()

    async def text(self):
        try:
            return await self._response.text()
        finally:
            self._release()

    async def read(self):
        try:
            return await self._response.read()
        finally:
            self._release()


class AioHttpTransport(HttpTransport):
    """Reusable, lifecycle-managed aiohttp-backed HTTP transport.

    The session is created lazily on first use (so constructing a
    transport never requires a running event loop) and creation is
    guarded by an asyncio.Lock so concurrent first requests cannot each
    create and leak their own session. Shutdown is explicit: whoever
    owns this transport (see app.workers' shutdown hooks) calls
    close() exactly once when it is no longer needed. After close(),
    further requests fail loudly with RuntimeError instead of silently
    opening a replacement session that would never get closed.
    """

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._session = None
        self._session_lock = asyncio.Lock()
        self._closed = False

    async def _get_session(self):
        if self._closed:
            raise RuntimeError(
                "AioHttpTransport is closed; cannot issue further requests"
            )
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._closed:
                    raise RuntimeError(
                        "AioHttpTransport is closed; cannot issue further requests"
                    )
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.timeout)
                    )
        return self._session

    async def request(self, method, url, *, headers=None, json=None, timeout=None):
        session = await self._get_session()
        request_timeout = (
            aiohttp.ClientTimeout(total=timeout) if timeout is not None else None
        )
        response = await session.request(
            method,
            url,
            headers=headers,
            json=json,
            timeout=request_timeout,
        )
        try:
            response.raise_for_status()
        except Exception:
            response.release()
            raise
        return AioHttpResponse(response)

    async def close(self):
        """Close the owned session. Idempotent -- safe to call more than once."""
        self._closed = True
        if self._session is not None and not self._session.closed:
            await self._session.close()
