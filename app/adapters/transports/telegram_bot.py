"""Outbound Telegram Bot transport adapter."""
import asyncio

from app.ports import NotificationTransport


class TelegramBotTransport(NotificationTransport):
    id = "telegram-bot"
    def __init__(self, bot=None, bot_token=None):
        self._bot = bot
        # A bot the transport built itself (bot=None) is owned by the
        # transport: shutdown must close it. A bot injected by a caller
        # stays owned by its injector -- the transport only ever shuts
        # down what it created.
        self._owns_bot = bot is None
        self._closed = False
        # Narrow injected configuration. A concrete token is used
        # verbatim; a zero-arg callable defers credential resolution to
        # first send (the historical lazy behavior the composition root
        # preserves). Neither form reads application config here, so the
        # adapter stays independently testable.
        self._bot_token = bot_token
    def _resolve_token(self):
        token = self._bot_token
        if callable(token):
            return token()
        if token is None:
            from app.config import get_bot_token
            return get_bot_token()
        return token
    def _get_bot(self):
        if self._closed:
            raise RuntimeError(
                "TelegramBotTransport is closed; refusing to send "
                "through a shut-down bot."
            )
        if self._bot is None:
            from telegram import Bot
            self._bot = Bot(self._resolve_token())
        return self._bot
    async def send_message(self, **kwargs):
        return await self._get_bot().send_message(**kwargs)
    async def close(self):
        """Shut down the bot this transport created (if any) exactly
        once. Idempotent and safe:

          * never-built bot   -> no-op;
          * created bot       -> awaited bot.shutdown() -- PTB 22.8's
                                 resource-lifecycle method, which stops
                                 the bot's request resources. (Bot.close()
                                 is the Bot API's close/move *operation*,
                                 not lifecycle cleanup, so it must not be
                                 used here.)
          * caller-injected bot -> left to its owner.

        A failing close must never raise: this runs as lifecycle cleanup
        during shutdown, so cleanup of one resource must not block the
        sequence that follows (the http transport and user bot follow
        the same principle)."""
        if self._closed:
            return
        self._closed = True
        bot = self._bot
        if bot is None or not self._owns_bot:
            return
        shutdown = getattr(bot, "shutdown", None)
        if shutdown is None:
            return
        try:
            result = shutdown()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            print(
                f"[SHUTDOWN] error shutting down telegram bot (continuing): {exc}",
                flush=True,
            )