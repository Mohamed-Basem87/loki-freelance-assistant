"""Regression tests for F-3 (audit finding: hung-worker detection gap).

app.heartbeat.heartbeat_loop only proves the event loop itself is still
turning. A genuinely async await that never resolves (a stalled
Telethon RPC call) or a stuck background thread behind
asyncio.to_thread (an LLM provider SDK call with no request timeout)
does not block the event loop -- heartbeat_loop keeps ticking on
schedule and the container's healthcheck reports healthy even though
that one worker task is permanently stuck.

app.timeouts.call_with_timeout / iter_with_timeout close that gap by
giving such calls a real ceiling. These tests exercise the helpers
directly (an actually-hanging awaitable/async-iterator), independent
of any particular call site.
"""
import asyncio

import pytest

from app.timeouts import call_with_timeout, iter_with_timeout


def test_call_with_timeout_raises_on_a_call_that_never_resolves():
    async def hangs_forever():
        await asyncio.sleep(3600)

    async def scenario():
        with pytest.raises(TimeoutError, match="stuck thing timed out"):
            await call_with_timeout(hangs_forever(), label="stuck thing", timeout=0.05)

    asyncio.run(scenario())


def test_call_with_timeout_passes_through_a_call_that_completes_in_time():
    async def quick():
        await asyncio.sleep(0)
        return "done"

    async def scenario():
        result = await call_with_timeout(quick(), label="quick thing", timeout=5)
        assert result == "done"

    asyncio.run(scenario())


def test_call_with_timeout_still_propagates_the_call_s_own_exception():
    async def fails():
        raise ValueError("boom")

    async def scenario():
        with pytest.raises(ValueError, match="boom"):
            await call_with_timeout(fails(), label="failing thing", timeout=5)

    asyncio.run(scenario())


def test_iter_with_timeout_raises_when_a_single_step_stalls():
    async def one_item_then_hang():
        yield 1
        await asyncio.sleep(3600)
        yield 2  # pragma: no cover - never reached

    async def scenario():
        items = []
        with pytest.raises(TimeoutError, match="stuck iterator timed out"):
            async for item in iter_with_timeout(
                one_item_then_hang(), label="stuck iterator", timeout=0.05
            ):
                items.append(item)
        assert items == [1]

    asyncio.run(scenario())


def test_iter_with_timeout_does_not_penalize_a_large_but_healthy_backlog():
    """A big legitimate result set (e.g. a large Telegram recovery
    backlog) must not be penalized by one overall deadline -- only a
    genuine per-step stall should raise. Each of these 200 items
    resolves immediately, well within the per-step timeout, even though
    their total count would blow past a naive whole-iteration budget."""

    async def many_quick_items():
        for i in range(200):
            await asyncio.sleep(0)
            yield i

    async def scenario():
        items = [
            item
            async for item in iter_with_timeout(
                many_quick_items(), label="healthy backlog", timeout=0.05
            )
        ]
        assert items == list(range(200))

    asyncio.run(scenario())
