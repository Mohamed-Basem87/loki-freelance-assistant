"""Bound external calls that have no timeout of their own and are
awaited directly on a worker's event-loop task.

Audit finding (hung-worker detection gap): app.heartbeat.heartbeat_loop
only proves the event loop itself is still turning. A genuinely async
await that never resolves (a Telethon RPC call stalled on the network)
or a stuck background thread behind ``asyncio.to_thread`` (an LLM
provider SDK call with no request timeout) does not block the event
loop -- heartbeat_loop keeps ticking on schedule and the container's
healthcheck reports healthy even though that one worker task is
permanently stuck.

These helpers give such calls a real ceiling (RuntimePolicy.
external_call_timeout_seconds) so they fail loudly with a plain
``TimeoutError`` instead of hanging forever. That error is deliberately
ordinary: every call site wrapped here already has its own
try/except-and-retry or fail-closed handling for provider/network
failures, so a timeout is simply routed into the same existing
handling rather than requiring new branches -- this only closes the
"stuck forever, never even raises" gap, it does not change what
happens once a failure is observed.
"""
import asyncio

from app.runtime_config import RUNTIME


async def call_with_timeout(awaitable, *, label, timeout=None):
    """Await ``awaitable``, raising ``TimeoutError`` if it does not
    complete within ``timeout`` seconds (default:
    RUNTIME.external_call_timeout_seconds).
    """
    timeout = RUNTIME.external_call_timeout_seconds if timeout is None else timeout
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"{label} timed out after {timeout}s with no response"
        ) from exc


async def iter_with_timeout(async_iterable, *, label, timeout=None):
    """Iterate ``async_iterable``, bounding each individual step rather
    than the iteration as a whole -- a large legitimate result set
    (e.g. a big Telegram recovery backlog) should not be penalized by
    one overall deadline; only a genuine per-step stall should raise.
    """
    timeout = RUNTIME.external_call_timeout_seconds if timeout is None else timeout
    iterator = async_iterable.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"{label} timed out after {timeout}s waiting for the next item"
            ) from exc
        yield item
