"""User command/UX adapter registry; concrete SDKs are resolved lazily."""
import importlib
import os
from .telegram import TelegramCommandSurface, TelegramUserMessaging

_FACTORIES = {"telegram": TelegramCommandSurface}
_MESSAGING_FACTORIES = {"telegram": TelegramUserMessaging}

def register(surface_id, factory, messaging_factory=None):
    _FACTORIES[surface_id.strip().lower()] = factory
    if messaging_factory is not None: _MESSAGING_FACTORIES[surface_id.strip().lower()] = messaging_factory

def build(surface_id=None, *, application_factory=None, channel_registrar=None):
    key=(surface_id or os.getenv("USER_COMMAND_SURFACE","telegram")).strip().lower()
    if key == "telegram":
        if application_factory is None:
            raise ValueError(
                "The telegram command surface requires an application_factory "
                "(the composition root wires app.user_bot.create_user_bot_application)"
            )
        return _FACTORIES[key](application_factory(), channel_registrar=channel_registrar)
    factory=_FACTORIES.get(key)
    if factory is None: raise KeyError(f"Unknown USER_COMMAND_SURFACE: {key}")
    return factory()

def build_messaging(bot, surface_id=None):
    key=(surface_id or os.getenv("USER_COMMAND_SURFACE","telegram")).strip().lower()
    try: return _MESSAGING_FACTORIES[key](bot)
    except KeyError: raise KeyError(f"Unknown USER_COMMAND_SURFACE: {key}")
