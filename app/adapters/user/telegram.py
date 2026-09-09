"""Telegram command-surface and user-messaging adapters."""
import asyncio
from app.ports import CommandSurface, UserMessaging

class TelegramCommandSurface(CommandSurface):
    id = "telegram"
    def __init__(self, application, channel_registrar=None):
        self._application = application
        self._channel_registrar = channel_registrar
    def application(self): return self._application
    async def initialize(self): await self._application.initialize()
    async def register_channel(self):
        if self._channel_registrar is None:
            raise RuntimeError(
                "TelegramCommandSurface requires an injected channel_registrar "
                "(the composition root wires app.user_bot.register_configured_channel)"
            )
        await self._channel_registrar(self._application)
    async def start(self): await self._application.start()
    async def run(self):
        await self.start()
        updater = self._application.updater
        if updater is None: raise RuntimeError("Telegram user bot updater is unavailable")
        await updater.start_polling(allowed_updates=("message", "callback_query", "my_chat_member"))
        await asyncio.Event().wait()
    async def stop(self):
        updater = self._application.updater
        if updater is not None and updater.running:
            await updater.stop()
        await self._application.stop()
        await self._application.shutdown()

class TelegramUserMessaging(UserMessaging):
    id = "telegram"
    def __init__(self, bot): self.bot = bot
    async def notify_user(self, user_id, rendered, **kwargs):
        await self.bot.send_message(chat_id=user_id, **rendered)
        return True
