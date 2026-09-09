"""Lazy Telegram transport used by the user-messaging adapter.

The application never needs arbitrary Bot attributes; the adapter exposes the
single semantic operation it uses.
"""
from app.config import get_bot_token


class TelegramBotClient:
    def __init__(self):
        self._bot = None

    def _get(self):
        if self._bot is None:
            from telegram import Bot
            self._bot = Bot(get_bot_token())
        return self._bot

    async def send_message(self, **kwargs):
        return await self._get().send_message(**kwargs)


bot = TelegramBotClient()
