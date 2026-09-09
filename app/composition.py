"""Single composition root: configuration -> adapters -> application runtime.

All dependency construction happens here. Workers, sources, and services
receive their dependencies via constructor injection from this root rather
than constructing their own infrastructure.
"""
import asyncio
import sys
from functools import partial

from app.adapters.repositories.registry import build as build_repository
from app.adapters.state.registry import build as build_state
from app.adapters.state.dedup_registry import build as build_dedup
from app.adapters.notifications.registry import build as build_sinks
from app.adapters.http.registry import build as build_http_transport
from app.dependencies import configure
from app.notifier import NotificationService
from app.parser import get_parser_registry
from app.routing import queue_for_category
from app.notification_guard.guard import NotificationGuard
from app.notification_guard.integration import NotificationGuardIntegration, GuardedNotificationService
from app.runtime_config import RUNTIME, RECOVERY


class Runtime:
    """Owns every resource the application needs. The single
    authoritative place the full dependency graph is held, making
    resource ownership explicit and lifecycle/shutdown deterministic."""

    def __init__(self, repository, state, dedup, notifications,
                 parser_registry, user_bot, guard=None,
                 http_transport=None, notification_transport=None,
                 shutdown_hooks=(), telegram_source=None):
        self.repository = repository
        self.state = state
        self.dedup = dedup
        self.notifications = notifications
        self.parser_registry = parser_registry
        self.user_bot = user_bot
        self.guard = guard
        self.http_transport = http_transport
        self.notification_transport = notification_transport
        self.shutdown_hooks = tuple(shutdown_hooks)
        self.telegram_source = telegram_source

    async def initialize_database(self):
        await self.repository.initialize()

    async def register_channel(self):
        await self.user_bot.register_channel()

    async def reset_inflight_notifications(self):
        await self.repository.reset_sending_user_notifications()

    async def shutdown(self):
        """Deterministic resource cleanup. Run exactly once after all
        workers stop. Each resource is cleaned up exactly once, even
        if shutdown is called multiple times."""
        if self.http_transport is not None:
            try:
                await self.http_transport.close()
            except Exception as exc:
                # Cleanup must never block the shutdown sequence. Surface the
                # failure so a resource leak is visible in the logs instead
                # of being swallowed.
                print(f"[SHUTDOWN] error closing http transport (continuing): {exc}", file=sys.stderr)
        if self.notification_transport is not None:
            # The Telegram bot transport owns (and must shut down) the
            # telegram.Bot it lazily created; an injected bot stays with
            # its injector, so this is a no-op unless the transport built
            # its own bot. Same fail-open principle as the http transport.
            close = getattr(self.notification_transport, "close", None)
            if close is not None:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    print(f"[SHUTDOWN] error closing notification transport (continuing): {exc}", file=sys.stderr)
        if self.user_bot is not None:
            try:
                await self.user_bot.stop()
            except Exception as exc:
                print(f"[SHUTDOWN] error stopping user bot (continuing): {exc}", file=sys.stderr)
        for hook in self.shutdown_hooks:
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                print(f"[SHUTDOWN] shutdown hook failed (continuing): {exc}", file=sys.stderr)


def compose():
    repository = build_repository()
    state = build_state()
    # NOTE: dedup deliberately resolves its own raw backend rather than
    # receiving `state` here. `state` is the StateStore *port* facade
    # (JsonStateStore), which only exposes the port's async surface; it
    # has no `.run()` executor method. StateDedupStore expects the raw
    # serialized backend (the same `app.state.state` StateManager
    # singleton `build_state()` itself wraps) so its `get_seen`/
    # `set_seen`/etc. calls stay routed through that one serialized
    # executor instead of raising AttributeError on first use.
    dedup = build_dedup()
    parser_registry = get_parser_registry()
    from app.categories.registry import enabled_categories
    if RUNTIME.rejection_reason_category_id and RUNTIME.rejection_reason_category_id not in {p.id for p in enabled_categories()}:
        raise ValueError(
            f"REJECTION_REASON_CATEGORY_ID references an unknown or disabled category: {RUNTIME.rejection_reason_category_id!r}"
        )

    # Build shared HTTP transport for FreeHub sources (one connection pool).
    http_transport = build_http_transport()

    # Build every enabled job source's collaborators here and register a
    # per-source factory (a closure over those collaborators) with the source
    # registry. The registry therefore remains a pure wiring table -- it
    # performs no dependency discovery, and the adapters never construct
    # infrastructure or read configuration themselves.
    import importlib as _importlib

    from app.adapters.sources.registry import register as register_source_factory
    from app.adapters.sources.freehub import FreeHubApiClient, FreeHubJobSource
    from app.adapters.sources.telegram import TelegramChannelJobSource
    from app.runtime_config import JOB_SOURCES
    import app.freehub as freehub_logic
    from app.config import (
        FREEHUB_BASE_URL,
        TARGET_CHANNELS,
        get_api_id,
        get_api_hash,
        get_phone_number,
        get_freehub_user_id,
        SESSION_NAME,
    )

    def _adapter_type(adapter_path):
        module_name, sep, attr = adapter_path.partition(":")
        if not sep:
            raise ValueError(
                f"Invalid source adapter path: {adapter_path!r}; "
                "expected module:Class"
            )
        return getattr(_importlib.import_module(module_name), attr)

    freehub_client = FreeHubApiClient(
        base_url=FREEHUB_BASE_URL,
        user_id=get_freehub_user_id(),
        timeout=RUNTIME.http_timeout_seconds,
        page_size=RUNTIME.freehub_page_size,
        transport=http_transport,
    )

    def _freehub_factory(**overrides):
        client = overrides.pop("http_client", None) or freehub_client
        return FreeHubJobSource(
            poller=lambda: freehub_logic.poll_once(client=client),
            marker=lambda job: freehub_logic.mark_project_seen(job),
            http_client=client,
        )

    def _telegram_channels_factory(**overrides):
        return TelegramChannelJobSource(
            client=overrides.pop("client", None),
            channels=tuple(overrides.pop("channels", None) or TARGET_CHANNELS),
            parser=overrides.pop("parser", None) or parser_registry,
            state_store=overrides.pop("state_store", None) or state,
            api_id=overrides.pop("api_id", get_api_id()),
            api_hash=overrides.pop("api_hash", get_api_hash()),
            phone_number=overrides.pop("phone_number", get_phone_number()),
            session_name=overrides.pop("session_name", SESSION_NAME),
            batch_limit=overrides.pop("batch_limit", None),
            max_recovery_messages=overrides.pop(
                "max_recovery_messages", RECOVERY.telegram_max_messages
            ),
            recovery_retry_base_seconds=overrides.pop(
                "recovery_retry_base_seconds",
                RECOVERY.telegram_retry_base_seconds,
            ),
            recovery_retry_cap_seconds=overrides.pop(
                "recovery_retry_cap_seconds", RECOVERY.telegram_retry_cap_seconds
            ),
        )

    for _cfg in JOB_SOURCES:
        if not _cfg.enabled:
            continue
        _adapter = _adapter_type(_cfg.adapter)
        if _adapter is FreeHubJobSource:
            register_source_factory(_cfg.id, _freehub_factory)
        elif _adapter is TelegramChannelJobSource:
            register_source_factory(_cfg.id, _telegram_channels_factory)
        else:
            raise RuntimeError(
                f"No composition wiring for job-source adapter {_cfg.adapter!r}"
            )

    # Canonical live Telegram production source (drives the 'telegram'
    # worker). Built only when that worker is enabled so a source-only
    # deployment never requires Telegram credentials.
    telegram_source = None
    if "telegram" in RUNTIME.enabled_workers:
        telegram_source = TelegramChannelJobSource(
            channels=tuple(TARGET_CHANNELS),
            parser=parser_registry,
            state_store=state,
            api_id=get_api_id(),
            api_hash=get_api_hash(),
            phone_number=get_phone_number(),
            session_name=SESSION_NAME,
            max_recovery_messages=RECOVERY.telegram_max_messages,
            recovery_retry_base_seconds=RECOVERY.telegram_retry_base_seconds,
            recovery_retry_cap_seconds=RECOVERY.telegram_retry_cap_seconds,
            liveness_beat_interval_seconds=RUNTIME.heartbeat_interval_seconds,
        )

    # Build notification infrastructure. The notification adapters are
    # configuration-free: bot token / chat id are injected as deferred
    # resolvers (still resolved lazily at first send, preserving the
    # historical behavior) so the composition root is the only place
    # that reads application config for them.
    from app.adapters.transports.registry import build as build_transport
    from app.config import get_bot_chat_id, get_bot_token
    from telegram import Bot
    
    # Create a SINGLE shared Bot instance for all outbound notifications.
    # This avoids creating multiple Bot instances for the same token and
    # ensures deterministic shutdown. The user-facing command surface
    # (user_bot) runs its own Application with its own Bot for inbound
    # polling; that is a separate lifecycle managed by user_bot.stop().
    shared_notification_bot = Bot(get_bot_token())
    
    guard = NotificationGuard(repository=repository)
    guard_integration = NotificationGuardIntegration(guard, repository=repository)
    # Inject the shared bot so the transport doesn't create its own.
    notification_transport = build_transport(bot=shared_notification_bot)
    notifications = NotificationService(
        sinks=build_sinks(
            transports={"telegram": notification_transport},
            chat_id=lambda: get_bot_chat_id(),
        ),
        repository=repository,
    )
    guarded_notifications = GuardedNotificationService(notifications, guard_integration)
    # Thread the composed repository into routing through the guard
    # wrapper's construction boundary; app.routing itself no longer
    # reaches into the service locator in production.
    guarded_routing = guard_integration.wrap_routing(
        partial(queue_for_category, repository=repository)
    )

    # Build user-facing Telegram surface.
    from app.adapters.user.registry import build as build_user_surface, build_messaging
    from app.user_bot import create_user_bot_application, register_configured_channel
    user_bot = build_user_surface(
        application_factory=create_user_bot_application,
        channel_registrar=register_configured_channel,
    )
    # Use the SAME shared bot for user messaging (outbound DMs to users).
    # The user_bot's Application has its own Bot for inbound polling;
    # this shared bot is only for outbound notifications.
    user_messaging = build_messaging(shared_notification_bot)
    from app.adapters.user.renderer import TelegramUserMessageRenderer
    user_renderer = TelegramUserMessageRenderer()

    # Bind all dependencies into the proxy layer for legacy callers.
    configure(
        persistence=repository,
        state_store=state,
        dedup_store=dedup,
        notification_service=guarded_notifications,
        routing=guarded_routing,
        parser_registry=parser_registry,
        notification_resolver=guard_integration.resolve_category,
        user_messaging_service=user_messaging,
        user_renderer_service=user_renderer,
    )

    def _shutdown_db():
        from app.logger import logger as _db_logger
        _db_logger.shutdown()

    def _shutdown_state():
        from app.state import state as _state_manager
        _state_manager.shutdown()

    async def _shutdown_shared_notification_bot():
        """Shut down the shared notification bot exactly once. Async to avoid
        run_until_complete() inside an already-running event loop."""
        try:
            # PTB 22.8+: Bot.shutdown() stops the request resources.
            # Bot.close() is an API operation, not lifecycle cleanup.
            shutdown = getattr(shared_notification_bot, "shutdown", None)
            if shutdown is not None:
                result = shutdown()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as exc:
            print(
                f"[SHUTDOWN] error shutting down shared notification bot (continuing): {exc}",
                flush=True,
            )

    return Runtime(
        repository, state, dedup, guarded_notifications,
        parser_registry, user_bot, guard,
        http_transport=http_transport,
        notification_transport=notification_transport,
        shutdown_hooks=(_shutdown_db, _shutdown_state, _shutdown_shared_notification_bot),
        telegram_source=telegram_source,
    )
