"""Compatibility seam for the canonical Telegram implementation.

The canonical implementation lives entirely in
app.adapters.sources.telegram and has NO application/config-global
dependencies -- every credential, channel set, and recovery option is
injected by the composition root (app.composition.compose) or threaded
through parameters.

This module is the LEGACY compatibility surface. It owns only the two
config-derived constructors (``start`` / ``build_telegram_source``) that
historically lived here, and each is a thin delegator into the canonical
machinery / ``TelegramChannelJobSource``. Everything else re-exported
below resolves straight to the canonical module. This module contains NO
independent ingestion, recovery, watermark, retry, or business logic and
must never grow a second Telegram implementation.
"""

from app.adapters.sources import telegram as _canonical

from app.config import (
    SESSION_NAME,
    TARGET_CHANNELS,
    get_api_id,
    get_api_hash,
    get_phone_number,
)
from app.runtime_config import RECOVERY


async def start():
    """Legacy compatibility entry point: builds a TelegramClient from the
    configured credentials (app.config), then runs the canonical live loop
    (_canonical._run_telegram_loop) over the configured channels with the
    configured recovery options. Production uses TelegramChannelWorker over
    a composition-injected TelegramChannelJobSource instead."""
    client = _canonical.TelegramClient(
        SESSION_NAME,
        get_api_id(),
        get_api_hash(),
    )

    # Explicitly log in as the phone-number userbot, never a bot token.
    # Passing the phone up front also removes the ambiguous interactive
    # prompt that previously let a bot token silently create a bot
    # session -- which cannot enumerate dialogs (get_dialogs) or resolve
    # the monitored channels by numeric ID (see _warm_entity_cache).
    await client.start(phone=get_phone_number())

    await _canonical._run_telegram_loop(
        client,
        tuple(TARGET_CHANNELS),
        max_recovery_messages=RECOVERY.telegram_max_messages,
        recovery_retry_base_seconds=RECOVERY.telegram_retry_base_seconds,
        recovery_retry_cap_seconds=RECOVERY.telegram_retry_cap_seconds,
    )


def build_telegram_source():
    """Legacy compatibility constructor for a TelegramChannelJobSource
    using config-derived credentials and recovery options. The canonical
    production path injects every collaborator -- including recovery
    options -- through the composition root (app.composition.compose)."""
    return _canonical.TelegramChannelJobSource(
        channels=tuple(TARGET_CHANNELS),
        parser=None,
        state_store=None,
        api_id=get_api_id(),
        api_hash=get_api_hash(),
        phone_number=get_phone_number(),
        session_name=SESSION_NAME,
        max_recovery_messages=RECOVERY.telegram_max_messages,
        recovery_retry_base_seconds=RECOVERY.telegram_retry_base_seconds,
        recovery_retry_cap_seconds=RECOVERY.telegram_retry_cap_seconds,
    )


from app.adapters.sources.telegram import (  # noqa: E402  (canonical single source of truth)
    TelegramClient,  # noqa: F401
    events,  # noqa: F401
    logger,  # noqa: F401
    process_message,  # noqa: F401
    state,  # noqa: F401
    DEFAULT_BATCH_LIMIT,  # noqa: F401
    DEFAULT_RECOVERY_MAX_MESSAGES,  # noqa: F401
    DEFAULT_RECOVERY_RETRY_BASE_SECONDS,  # noqa: F401
    DEFAULT_RECOVERY_RETRY_CAP_SECONDS,  # noqa: F401
    _warm_entity_cache,  # noqa: F401
    _recover_channel,  # noqa: F401
    _handle_live_message,  # noqa: F401
    _retry_recovery_channel,  # noqa: F401
    _recover_all_channels,  # noqa: F401
    _run_telegram_loop,  # noqa: F401
    TelegramChannelWorker,  # noqa: F401
    TelegramChannelJobSource,  # noqa: F401
)

__all__ = [
    "SESSION_NAME",
    "TARGET_CHANNELS",
    "TelegramClient",
    "events",
    "logger",
    "process_message",
    "state",
    "start",
    "build_telegram_source",
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_RECOVERY_MAX_MESSAGES",
    "DEFAULT_RECOVERY_RETRY_BASE_SECONDS",
    "DEFAULT_RECOVERY_RETRY_CAP_SECONDS",
    "_warm_entity_cache",
    "_recover_channel",
    "_handle_live_message",
    "_retry_recovery_channel",
    "_recover_all_channels",
    "_run_telegram_loop",
    "TelegramChannelWorker",
    "TelegramChannelJobSource",
]