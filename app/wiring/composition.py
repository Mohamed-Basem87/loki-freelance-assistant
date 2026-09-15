"""Single composition root: configuration -> adapters -> application runtime.

All dependency construction happens here. Workers, sources, and services
receive their dependencies via constructor injection from this root rather
than constructing their own infrastructure.
"""
import asyncio
import os
import sys
from functools import partial

from app.adapters.repositories.postgres import PostgresRepository
from app.adapters.state.json import JsonStateStore
from app.adapters.state.dedup import StateDedupStore
from app.services.state import state as _state_manager
from app.adapters.streams.redis_publisher import RedisJobStreamPublisher
import redis.asyncio as _redis
from app.adapters.http.aiohttp import AioHttpTransport
from app.wiring.dependencies import configure
from app.services.parser import get_parser_registry
from app.notification_guard.guard import NotificationGuard
from app.notification_guard.integration import NotificationGuardIntegration
from app.infra.url_shortener import UrlShortenerService
from app.infra.runtime_config import RUNTIME, RECOVERY, BASE_DIR
from app.domain.categories.registry import enabled_categories
import importlib as _importlib
from app.adapters.sources.registry import register as register_source_factory
from app.adapters.sources.freehub import FreeHubApiClient, FreeHubJobSource
from app.adapters.sources.telegram import TelegramChannelJobSource
from app.adapters.sources.scraper_file import (
    LinkedInFileJobSource,
    WuzzufFileJobSource,
    ScraperFileClient,
)
from app.infra.runtime_config import JOB_SOURCES
import app.services.freehub as freehub_logic
from app.infra.config import (
    FREEHUB_BASE_URL,
    TARGET_CHANNELS,
    get_api_id,
    get_api_hash,
    get_phone_number,
    get_freehub_user_id,
    SESSION_NAME,
)


class Runtime:
    """Owns every resource the application needs. The single
    authoritative place the full dependency graph is held, making
    resource ownership explicit and lifecycle/shutdown deterministic."""

    def __init__(self, repository, state, dedup, stream_publisher,
                 parser_registry, guard=None,
                 http_transport=None,
                 shutdown_hooks=(), telegram_source=None):
        self.repository = repository
        self.state = state
        self.dedup = dedup
        self.stream_publisher = stream_publisher
        self.parser_registry = parser_registry
        self.guard = guard
        self.http_transport = http_transport
        self.shutdown_hooks = tuple(shutdown_hooks)
        self.telegram_source = telegram_source

    async def initialize_database(self):
        await self.repository.initialize()

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
        if self.stream_publisher is not None:
            close = getattr(self.stream_publisher, "close", None)
            if close is not None:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    print(f"[SHUTDOWN] error closing stream publisher (continuing): {exc}", file=sys.stderr)
        for hook in self.shutdown_hooks:
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                print(f"[SHUTDOWN] shutdown hook failed (continuing): {exc}", file=sys.stderr)


def compose():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")
    repository = PostgresRepository(database_url)
    state = JsonStateStore(_state_manager)
    # NOTE: dedup deliberately resolves its own raw backend rather than
    # receiving `state` here. `state` is the StateStore *port* facade
    # (JsonStateStore), which only exposes the port's async surface; it
    # has no `.run()` executor method. StateDedupStore expects the raw
    # serialized backend (the same `app.services.state.state` StateManager
    # singleton `state` itself wraps) so its `get_seen`/
    # `set_seen`/etc. calls stay routed through that one serialized
    # executor instead of raising AttributeError on first use.
    dedup = StateDedupStore(_state_manager)
    parser_registry = get_parser_registry()

    if RUNTIME.rejection_reason_category_id and RUNTIME.rejection_reason_category_id not in {p.id for p in enabled_categories()}:
        raise ValueError(
            f"REJECTION_REASON_CATEGORY_ID references an unknown or disabled category: {RUNTIME.rejection_reason_category_id!r}"
        )

    # Build shared HTTP transport for FreeHub sources (one connection pool).
    http_transport = AioHttpTransport()

    # URL shortener: reuses the same shared HTTP transport/connection pool
    # (see app.infra.url_shortener) rather than opening a second one.
    # Required-at-composition-time, not required-at-import-time -- see the
    # comment above RUNTIME.url_shortener_failure_mode's validation in
    # app/core/runtime_config.py for why domain/endpoint aren't validated there.
    if not RUNTIME.url_shortener_domain:
        raise RuntimeError("Missing required environment variable: URL_SHORTENER_DOMAIN")
    if not RUNTIME.url_shortener_endpoint:
        raise RuntimeError("Missing required environment variable: URL_SHORTENER_ENDPOINT")
    url_shortener_service = UrlShortenerService(
        transport=http_transport,
        domain=RUNTIME.url_shortener_domain,
        endpoint=RUNTIME.url_shortener_endpoint,
        failure_mode=RUNTIME.url_shortener_failure_mode,
    )

    # Build every enabled job source's collaborators here and register a
    # per-source factory (a closure over those collaborators) with the source
    # registry. The registry therefore remains a pure wiring table -- it
    # performs no dependency discovery, and the adapters never construct
    # infrastructure or read configuration themselves.
    



    def _freehub_factory(**overrides):
        client = overrides.pop("http_client", None) or FreeHubApiClient(
            base_url=FREEHUB_BASE_URL,
            user_id=get_freehub_user_id(),
            timeout=RUNTIME.http_timeout_seconds,
            page_size=RUNTIME.freehub_page_size,
            transport=http_transport,
        )
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

    def _scraper_file_factory(source_class, **overrides):
        file_path = overrides.pop("file_path", None)
        if not file_path:
            file_path = str(BASE_DIR / "jobs_results.json")
        elif not os.path.isabs(file_path):
            file_path = str(BASE_DIR / file_path)
        client = overrides.pop("client", None) or ScraperFileClient(
            file_path=file_path,
            platform=source_class.platform,
        )
        return source_class(client=client)

    for _cfg in JOB_SOURCES:
        if not _cfg.enabled:
            continue
        _adapter = _adapter_type(_cfg.adapter)
        if _adapter is FreeHubJobSource:
            register_source_factory(_cfg.id, _freehub_factory)
        elif _adapter is TelegramChannelJobSource:
            register_source_factory(_cfg.id, _telegram_channels_factory)
        elif _adapter in (LinkedInFileJobSource, WuzzufFileJobSource):
            register_source_factory(
                _cfg.id, partial(_scraper_file_factory, _adapter)
            )
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

    # Guard stays in this node: it durably decides notify/do_not_notify
    # per job. Everything downstream of that decision (fan-out,
    # rendering, delivery) moved to a separate service that consumes
    # the Redis stream published below.
    guard = NotificationGuard(repository=repository)
    guard_integration = NotificationGuardIntegration(guard, repository=repository)

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("Missing required environment variable: REDIS_URL")
    redis_client = _redis.from_url(redis_url)
    stream_publisher = RedisJobStreamPublisher(
        redis_client,
        stream=os.getenv("JOB_STREAM_NAME", "jobs:notify"),
    )

    # Bind all dependencies into the proxy layer for legacy callers.
    configure(
        persistence=repository,
        state_store=state,
        dedup_store=dedup,
        parser_registry=parser_registry,
        notification_resolver=guard_integration.resolve_category,
        guard_allow_fn=guard_integration.allow,
        stream_publisher_service=stream_publisher,
        url_shortener_service=url_shortener_service,
    )

    def _shutdown_state():
        from app.services.state import state as _state_manager
        _state_manager.shutdown()

    def _shutdown_db_engine():
        repository._engine.dispose()

    return Runtime(
        repository, state, dedup, stream_publisher,
        parser_registry, guard,
        http_transport=http_transport,
        shutdown_hooks=(_shutdown_db_engine, _shutdown_state),
        telegram_source=telegram_source,
    )

def _adapter_type(adapter_path):
    module_name, sep, attr = adapter_path.partition(":")
    if not sep:
        raise ValueError(
            f"Invalid source adapter path: {adapter_path!r}; "
            "expected module:Class"
        )
    return getattr(_importlib.import_module(module_name), attr)
