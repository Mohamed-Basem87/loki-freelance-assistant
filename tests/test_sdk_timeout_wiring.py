"""Regression tests for BUG #2: bounded SDK/HTTP lifetime on provider calls.

The synchronous provider SDK calls run on a dedicated thread via
asyncio.to_thread wrapped in asyncio.wait_for. asyncio.wait_for cannot
cancel a blocked thread, so the only real fix is a hard timeout at the
SDK/HTTP layer itself -- the synchronous call must ALWAYS return within
a bounded window, never hang the worker thread on a dead socket.

These tests assert that every client construction passes that bounded
timeout (from RUNTIME.http_timeout_seconds) into the SDK, so the
wiring is present even though no real provider call is made.
"""
import pytest

from app.runtime_config import RUNTIME


def test_gemini_client_is_constructed_with_http_layer_timeout(monkeypatch):
    from app.llm import gemini

    captured = {}
    expected = float(RUNTIME.http_timeout_seconds)

    class _FakeHttpOptions:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        def __bool__(self):
            return True

    class _FakeClient:
        def __init__(self, *, api_key, http_options):
            captured["api_key"] = api_key
            captured["http_options"] = http_options

    monkeypatch.setattr(gemini.genai, "Client", _FakeClient)
    monkeypatch.setattr(gemini.genai, "types", type("T", (), {"HttpOptions": _FakeHttpOptions})())
    monkeypatch.setattr(gemini, "CLIENTS", None)
    monkeypatch.setattr("app.llm.gemini.get_gemini_api_keys", lambda: ["test-key"])

    clients = gemini._clients()
    assert len(clients) == 1
    assert captured["timeout"] == expected, (
        "the Gemini HTTP layer must be given a bounded timeout so a "
        "stalled sync SDK call can never hang the worker thread"
    )


def test_groq_client_is_constructed_with_sdk_timeout(monkeypatch):
    from app.llm import groq

    captured = {}
    expected = float(RUNTIME.http_timeout_seconds)

    class _FakeGroq:
        def __init__(self, *, api_key, timeout):
            captured["api_key"] = api_key
            captured["timeout"] = timeout

    monkeypatch.setattr(groq, "Groq", _FakeGroq)
    monkeypatch.setattr(groq, "CLIENT", None)
    monkeypatch.setattr("app.llm.groq.get_groq_api_key", lambda: "test-key")

    client = groq._client()
    assert captured["timeout"] == expected, (
        "the Groq SDK must be given a bounded timeout so a stalled sync "
        "call can never hang the worker thread"
    )
    assert client is groq._client(), "the client must be cached, not recreated per call"


def test_guard_groq_client_is_constructed_with_sdk_timeout(monkeypatch):
    from app.notification_guard import groq as guard_groq
    from app.notification_guard import config as guard_config

    captured = {}
    expected = float(RUNTIME.http_timeout_seconds)

    class _FakeGroq:
        def __init__(self, *, api_key, timeout):
            captured["timeout"] = timeout

    monkeypatch.setattr(guard_groq, "Groq", _FakeGroq)
    monkeypatch.setattr(guard_groq, "CLIENTS", [])
    # NOTIFICATION_GUARD_API_KEYS is imported by-name into guard_groq at
    # module import, so it must be patched there, not on guard_config.
    monkeypatch.setattr(guard_groq, "NOTIFICATION_GUARD_API_KEYS", ["test-key"])

    guard_groq._clients()
    assert captured["timeout"] == expected, (
        "the Notification Guard's Groq SDK must also get the bounded timeout"
    )


def test_live_event_get_chat_is_bounded_by_the_external_call_timeout(monkeypatch):
    """P2-B: the live-handler chat lookup (event.get_chat()) is a live
    Telethon round-trip on the event loop. It must be wrapped in
    call_with_timeout like every other RPC in the Telegram loop, so a
    stalled connection cannot block the loop forever -- and the wrapped
    call must still resolve exactly what it resolved before (the chat is
    printed and forwarded to _handle_live_message unchanged)."""
    import asyncio
    from types import SimpleNamespace

    from app.adapters.sources import telegram

    wrapped_labels = []
    live_handled = {"n": 0}
    registered = {"handler": None}
    invoked = {"value": False}

    def fake_call_with_timeout(awaitable, *, label, timeout=None):
        wrapped_labels.append(label)

        async def _run():
            return await awaitable

        return _run()

    class _FakeClient:
        async def get_me(self):
            return SimpleNamespace(first_name="Test")

        def on(self, event_filter):
            return event_filter

        async def run_until_disconnected(self):
            # After the loop has registered the handler and completed
            # recovery, drive one live event through the registered
            # handler with a chat lookup that would previously have gone
            # unbounded (bare `await event.get_chat()`).
            handler = registered["handler"]

            def _chat_coro():
                async def _get():
                    return SimpleNamespace(title="Test Channel")
                return _get()

            fake_event = SimpleNamespace(id=777, get_chat=_chat_coro)
            await handler(fake_event)
            invoked["value"] = True

    def fake_new_message(**kwargs):
        def decorator(handler):
            registered["handler"] = handler
            return handler

        return decorator

    async def fake_warm_entity_cache(client, channels=None):
        return None

    async def fake_recover_all_channels(
        client, channel_locks, recovery_blocked, channels=None, max_messages=None
    ):
        for channel in channels:
            recovery_blocked[channel] = False
            channel_locks[channel].release()

    async def fake_handle_live_message(event, channel_locks, recovery_blocked, raise_live_barrier):
        live_handled["n"] += 1

    monkeypatch.setattr(telegram, "call_with_timeout", fake_call_with_timeout)
    monkeypatch.setattr(telegram.events, "NewMessage", fake_new_message)
    monkeypatch.setattr(telegram, "_warm_entity_cache", fake_warm_entity_cache)
    monkeypatch.setattr(telegram, "_recover_all_channels", fake_recover_all_channels)
    monkeypatch.setattr(telegram, "_handle_live_message", fake_handle_live_message)

    asyncio.run(telegram._run_telegram_loop(_FakeClient(), [-1101], max_recovery_messages=0))

    assert invoked["value"] is True
    assert live_handled["n"] == 1, (
        "the bounded chat lookup must still hand the event to "
        "_handle_live_message exactly like the unbounded version did"
    )
    assert wrapped_labels == ["Telegram event.get_chat()"], (
        "event.get_chat() must be routed through call_with_timeout with the "
        "loop's external-call deadline, not awaited bare"
    )
