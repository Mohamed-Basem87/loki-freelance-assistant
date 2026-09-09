"""Regression tests: notification/transport adapters take their Telegram
credentials from the composition root via narrow constructor injection and
no longer reach into app config themselves when those values are supplied.

The injection accepts either a concrete token/chat id (used verbatim) or a
zero-arg callable (deferred to first send), preserving the historical lazy
credential resolution the composition root relies on.
"""
import asyncio

import pytest

from app.adapters.notifications.telegram import TelegramNotificationSink
from app.adapters.transports.telegram_bot import TelegramBotTransport


def test_transport_uses_injected_token_and_builds_bot_lazily(monkeypatch):
    stashed = {}

    def fake_bot(token):
        stashed["token"] = token

        class _FakeBot:
            async def send_message(self, **kwargs):
                stashed["sent"] = kwargs
                return object()

        return _FakeBot()

    monkeypatch.setattr("telegram.Bot", fake_bot)
    transport = TelegramBotTransport(bot_token="injected-token")
    assert transport._bot is None  # never built at construction time
    asyncio.run(transport.send_message(chat_id=1, text="x"))
    assert stashed["token"] == "injected-token"
    assert stashed["sent"]["chat_id"] == 1


def test_transport_defers_token_resolution_through_callable(monkeypatch):
    calls = {"n": 0}
    seen = []

    def resolve():
        calls["n"] += 1
        return "deferred-token"

    def fake_bot(token):
        seen.append(token)

        class _FakeBot:
            async def send_message(self, **kwargs):
                return None

        return _FakeBot()

    monkeypatch.setattr("telegram.Bot", fake_bot)
    transport = TelegramBotTransport(bot_token=resolve)
    assert calls["n"] == 0  # nothing resolved until first send
    asyncio.run(transport.send_message(chat_id=1, text="x"))
    assert calls["n"] == 1
    assert seen == ["deferred-token"]


def test_sink_uses_injected_chat_id_and_transport():
    sent = {}

    class _FakeTransport:
        async def send_message(self, **kwargs):
            sent.update(kwargs)

    sink = TelegramNotificationSink(transport=_FakeTransport(), chat_id=12345)
    assert sink._resolve_chat_id() == 12345
    asyncio.run(sink.send(payload={"title": "t"}))
    assert sent["chat_id"] == 12345
    assert "text" in sent


def test_sink_resolves_chat_id_through_callable():
    calls = {"n": 0}

    def resolve():
        calls["n"] += 1
        return 999

    class _FakeTransport:
        async def send_message(self, **kwargs):
            return None

    sink = TelegramNotificationSink(transport=_FakeTransport(), chat_id=resolve)
    assert sink._resolve_chat_id() == 999
    assert calls["n"] == 1  # resolved once, lazily


# ------------------------------------------------------------------
# P2-C: the transport owns and shuts down the bot it created. The
# crash averted: Runtime.shutdown() never closed the bot, so every
# production process exit leaked the background request session the
# python-telegram-bot transport holds until interpreter shutdown. The
# cleanup is bot.shutdown(), the PTB 22.8 resource-lifecycle method
# (Bot.close() is the Bot API's close/move *operation*, not lifecycle
# cleanup).
# ------------------------------------------------------------------


def test_transport_close_awaits_shutdown_of_the_bot_it_created_exactly_once(monkeypatch):
    events = []

    def fake_bot(token):
        class _FakeBot:
            async def send_message(self, **kwargs):
                return None

            async def shutdown(self):
                events.append("shutdown")

            async def close(self):
                # The Bot API operation must NOT be used for lifecycle
                # cleanup; if the transport calls it, the test fails.
                events.append("bot-api-close")

        return _FakeBot()

    monkeypatch.setattr("telegram.Bot", fake_bot)
    transport = TelegramBotTransport(bot_token="token")
    asyncio.run(transport.send_message(chat_id=1, text="x"))  # owns a created bot
    asyncio.run(transport.close())
    asyncio.run(transport.close())  # idempotent
    assert events == ["shutdown"], (
        "the created bot must be shut down via bot.shutdown() exactly "
        "once -- Bot.close() (the Bot API operation) must never be used "
        "for resource cleanup"
    )


def test_transport_close_without_ever_sending_is_a_no_op():
    transport = TelegramBotTransport(bot_token="token")
    asyncio.run(transport.close())  # must not raise / must not build a bot
    assert transport._bot is None


def test_transport_refuses_to_send_after_close(monkeypatch):
    monkeypatch.setattr("telegram.Bot", lambda token: object())
    transport = TelegramBotTransport(bot_token="token")
    asyncio.run(transport.close())
    with pytest.raises(RuntimeError, match="closed"):
        asyncio.run(transport.send_message(chat_id=1, text="x"))


def test_transport_never_shuts_down_a_bot_injected_by_its_caller(monkeypatch):
    lifecycle_calls = []

    class _FakeBot:
        async def send_message(self, **kwargs):
            return None

        async def shutdown(self):
            lifecycle_calls.append("shutdown")

        async def close(self):
            lifecycle_calls.append("close")

    # No monkeypatch of telegram.Bot: the injected bot is used verbatim.
    injected = _FakeBot()
    transport = TelegramBotTransport(bot=injected, bot_token="token")
    asyncio.run(transport.close())
    assert lifecycle_calls == [], (
        "ownership stays with the injector: the transport must not shut "
        "down (or Bot-API-close) a bot it did not create"
    )
    assert transport._bot is injected