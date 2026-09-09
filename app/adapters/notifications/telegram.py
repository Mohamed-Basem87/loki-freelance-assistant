"""Telegram notification transport adapter."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from app.ports import NotificationSink
from app.adapters.notifications.renderer import TelegramMessageRenderer
from app.adapters.transports.registry import build as build_transport

class TelegramNotificationSink(NotificationSink):
    id = "telegram"
    def __init__(self, renderer=None, transport=None, chat_id=None):
        self.renderer = renderer or TelegramMessageRenderer()
        self.transport = transport or build_transport()
        # Narrow injected configuration. A concrete chat id is used
        # verbatim; a zero-arg callable defers credential resolution to
        # first send (the historical lazy behavior the composition root
        # preserves). Neither form reads application config here, so the
        # adapter stays independently testable.
        self._chat_id = chat_id
    def _resolve_chat_id(self):
        chat_id = self._chat_id
        if callable(chat_id):
            return chat_id()
        if chat_id is None:
            from app.config import get_bot_chat_id
            return get_bot_chat_id()
        return chat_id
    async def send(self, rendered=None, **payload):
        rendered = rendered or self.renderer.render(payload)
        keyboard = None
        if rendered.get("button_url"):
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Project", url=rendered["button_url"])]])
        await self.transport.send_message(chat_id=self._resolve_chat_id(), text=rendered["text"], parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)
        return True