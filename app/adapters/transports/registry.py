"""Transport registry for outbound human-facing delivery."""
import os
from app.adapters.transports.telegram_bot import TelegramBotTransport
_FACTORIES = {"telegram-bot": TelegramBotTransport}
def register(transport_id, factory): _FACTORIES[transport_id] = factory
def build(transport_id=None, **kwargs):
    key = (transport_id or os.getenv("NOTIFICATION_TRANSPORT", "telegram-bot")).strip().lower()
    try: factory = _FACTORIES[key]
    except KeyError: raise KeyError(f"Unknown notification transport: {key}")
    return factory(**kwargs)
