"""Notification sink/renderer registries; selected SDKs are loaded lazily."""
import importlib
import os

_SINK_PATHS = {"telegram": "app.adapters.notifications.telegram:TelegramNotificationSink"}
_RENDERER_PATHS = {"telegram": "app.adapters.notifications.renderer:TelegramMessageRenderer"}
_SINK_FACTORIES = {}
_RENDERER_FACTORIES = {}

def register_sink(backend_id, factory): _SINK_FACTORIES[backend_id.strip().lower()] = factory
def register_renderer(renderer_id, factory): _RENDERER_FACTORIES[renderer_id.strip().lower()] = factory

def _load(path):
    module_name, _, attr = path.partition(":")
    return getattr(importlib.import_module(module_name), attr)

def build(renderer=None, transports=None, chat_id=None):
    configured = tuple(x.strip().lower() for x in os.getenv("NOTIFICATION_SINKS", "telegram").split(",") if x.strip())
    bindings=[]
    for key in configured:
        factory = _SINK_FACTORIES.get(key) or _load(_SINK_PATHS[key])
        renderer_factory = _RENDERER_FACTORIES.get(key) or _load(_RENDERER_PATHS[key])
        register_sink(key, factory); register_renderer(key, renderer_factory)
        renderer_instance = renderer if renderer is not None else renderer_factory()
        kwargs = {"renderer": renderer_instance}
        if chat_id is not None: kwargs["chat_id"] = chat_id
        if transports and key in transports: kwargs["transport"] = transports[key]
        bindings.append(factory(**kwargs))
    return tuple(bindings)
