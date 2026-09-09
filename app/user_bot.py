"""User-facing Telegram bot and durable multi-user notification delivery."""

import asyncio
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

from app.categories.registry import enabled_categories
from app.config import BOT_CHANNEL_CATEGORY_ID, BOT_CHANNEL_ID, get_bot_token
from app.dependencies import logger, user_messaging, user_renderer
from app.heartbeat import STATE_RUNNING, sleep_with_beats
from app.runtime_config import RUNTIME, SOURCES

DELIVERY_CONCURRENCY = RUNTIME.delivery_concurrency
POLL_INTERVAL = RUNTIME.user_bot_poll_interval
BATCH_SIZE = RUNTIME.user_bot_batch_size
MAX_ATTEMPTS = RUNTIME.user_bot_max_attempts

# These are source IDs, not metadata entities. They are stored as a
# comma-separated preference on the user row.
SOURCE_OPTIONS = tuple((p.id, p.display_name) for p in SOURCES)

_INACTIVE_NOTICE = (
    "⏸ You're currently unsubscribed from Loki Jobs.\n"
    "Your choices below are saved, but won't be delivered until you "
    "resume notifications.\n\n"
)

# Distinct resume callback per screen (see category_callback) so the
# handler knows which picker to redraw after reactivating -- reusing
# "cat:"/"src:" prefixes here would collide with real category/source
# IDs, so this uses its own top-level "reactivate:" prefix instead.
_RESUME_CATEGORIES_CALLBACK = "reactivate:cat"
_RESUME_SOURCES_CALLBACK = "reactivate:src"


async def _is_active(telegram_user_id) -> bool:
    """True unless the user has explicitly /stopped or blocked the bot.

    Looked up by Telegram user ID (not the internal "User ID" UUID)
    since that's what every command handler already has on hand from
    `update.effective_user.id`, and it's the same column
    `record_subscription_event`/`set_destination_active` write to.
    A user who has never been seen before (no row yet) is treated as
    active -- ensure_user() is expected to have already created the
    row by the time this is called from any real command path, so
    this only matters for defensive callers.
    """
    destination = await logger.get_destination(telegram_user_id)
    if destination is None:
        return True
    return str(destination.get("Is Active", "1")) == "1"


def _source_keyboard(selected_sources, *, inactive=False):
    selected_sources = set(selected_sources)
    rows = []
    if inactive:
        rows.append([
            InlineKeyboardButton(
                "▶️ Resume notifications",
                callback_data=_RESUME_SOURCES_CALLBACK,
            )
        ])
    for source_id, display_name in SOURCE_OPTIONS:
        prefix = "✅" if source_id in selected_sources else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{prefix} {display_name}",
                callback_data=f"src:{source_id}",
            )
        ])
    rows.append([InlineKeyboardButton("Done", callback_data="src:done")])
    return InlineKeyboardMarkup(rows)


async def _render_sources(query, user_id, telegram_user_id, *, edit=True):
    selected = await logger.get_user_sources(user_id)
    inactive = not await _is_active(telegram_user_id)
    text = (
        (_INACTIVE_NOTICE if inactive else "")
        + "Choose the sources you want to receive.\n\n"
        "If you select none, you'll receive jobs from all sources."
    )
    markup = _source_keyboard(selected, inactive=inactive)
    if edit:
        await query.edit_message_text(text=text, reply_markup=markup)
    else:
        await query.message.reply_text(text=text, reply_markup=markup)


def _category_keyboard(selected_ids, *, inactive=False):
    selected_ids = set(selected_ids)
    rows = []
    if inactive:
        rows.append([
            InlineKeyboardButton(
                "▶️ Resume notifications",
                callback_data=_RESUME_CATEGORIES_CALLBACK,
            )
        ])
    for profile in enabled_categories():
        prefix = "✅" if profile.id in selected_ids else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{prefix} {profile.name}",
                callback_data=f"cat:{profile.id}",
            )
        ])
    rows.append([InlineKeyboardButton("Done", callback_data="done")])
    return InlineKeyboardMarkup(rows)


async def _render_categories(query, user_id, telegram_user_id, *, edit=True):
    selected = await logger.get_user_categories(user_id)
    inactive = not await _is_active(telegram_user_id)
    text = (
        (_INACTIVE_NOTICE if inactive else "")
        + "Choose the job categories you want to receive.\n\n"
        "You can select more than one."
    )
    markup = _category_keyboard(selected, inactive=inactive)
    if edit:
        await query.edit_message_text(text=text, reply_markup=markup)
    else:
        await query.message.reply_text(text=text, reply_markup=markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.effective_chat is None:
        return

    internal_id = await logger.ensure_user(user.id, user.username or "", user.first_name or "")
    await logger.record_subscription_event(user.id, user.first_name or "", user.username or "", True, "start")

    await update.message.reply_text(
        "Welcome to Loki Jobs 👋\n\n"
        "Choose the categories you want to receive.",
        reply_markup=_category_keyboard(
            await logger.get_user_categories(internal_id)
        ),
    )


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    internal_id = await logger.ensure_user(user.id, user.username or "", user.first_name or "")
    inactive = not await _is_active(user.id)
    text = (
        (_INACTIVE_NOTICE if inactive else "")
        + "Choose the sources you want to receive.\n\n"
        "If you select none, you'll receive jobs from all sources."
    )
    await update.message.reply_text(
        text,
        reply_markup=_source_keyboard(
            await logger.get_user_sources(internal_id),
            inactive=inactive,
        ),
    )


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    internal_id = await logger.ensure_user(user.id, user.username or "", user.first_name or "")
    inactive = not await _is_active(user.id)
    text = (
        (_INACTIVE_NOTICE if inactive else "")
        + "Choose the job categories you want to receive.\n\n"
        "You can select more than one."
    )
    await update.message.reply_text(
        text,
        reply_markup=_category_keyboard(
            await logger.get_user_categories(internal_id),
            inactive=inactive,
        ),
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opt the user out of notifications.

    Telegram never sends a my_chat_member update for a device-level
    "Stop Bot" -- that only fires on a hard block/unblock (see Bot API
    "my_chat_member": "For private chats, this update is received only
    when the bot is blocked or unblocked by the user"). So /stop is the
    only reliable in-band way for a user to unsubscribe. It sets
    Is Active=0 so the notification claim query stops picking the user
    up; /start re-activates them.

    /stop is opt-out semantics, not pause-and-deliver-later: it also
    cancels any notifications already queued but not yet delivered, so
    a user who unsubscribes and later sends /start again does not get
    hit with a burst of every job that piled up while they were
    inactive (audit finding: user subscription semantics -- this is a
    deliberate choice between "pause, preserve backlog" and "cancel
    stale queued notifications"; deterministic and restart-safe since
    it is a direct, persisted status write, not a timer or in-memory
    flag).
    """
    user = update.effective_user
    if user is None or update.effective_chat is None:
        return

    await logger.ensure_user(user.id, user.username or "", user.first_name or "")
    await logger.record_subscription_event(user.id, user.first_name or "", user.username or "", False, "stop")
    await logger.cancel_pending_user_notifications(user.id)

    try:
        await update.message.reply_text(
            "You've been unsubscribed from Loki Jobs. "
            "You won't receive any more job notifications.\n\n"
            "To start receiving jobs again, send /start."
        )
    except Exception as exc:
        await logger.log_error("Stop Command", exc, "", save=True)


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    data = query.data or ""
    internal_id = await logger.ensure_user(user.id, user.username or "", user.first_name or "")

    if data.startswith("reactivate:"):
        await query.answer("Notifications resumed ✅")
        await logger.record_subscription_event(user.id, user.first_name or "", user.username or "", True, "reactivate")
        screen = data.split(":", 1)[1]
        if screen == "src":
            await _render_sources(query, internal_id, user.id)
        else:
            await _render_categories(query, internal_id, user.id)
        return

    if data == "done":
        await query.answer()
        await _render_sources(query, internal_id, user.id)
        return

    if data.startswith("src:"):
        source_id = data[4:]
        valid_sources = {source_id for source_id, _ in SOURCE_OPTIONS}
        if source_id == "done":
            await query.answer()
            selected = await logger.get_user_sources(internal_id)
            names = {source_id: display_name for source_id, display_name in SOURCE_OPTIONS}
            if selected:
                selected_names = [names[source_id] for source_id in selected if source_id in names]
                text = "✅ Saved. You'll receive sources:\n\n" + "\n".join(f"• {name}" for name in selected_names)
            else:
                text = "✅ Saved. No source filter is active, so you'll receive jobs from all sources."
            await query.edit_message_text(text=text)
            return

        if source_id not in valid_sources:
            await query.answer("That source is no longer available.", show_alert=True)
            return

        await query.answer()
        selected = set(await logger.get_user_sources(internal_id))
        await logger.set_user_source(internal_id, source_id, source_id not in selected, True)
        await _render_sources(query, internal_id, user.id)
        return

    if not data.startswith("cat:"):
        await query.answer()
        return

    category_id = data[4:]
    valid_ids = {profile.id for profile in enabled_categories()}
    if category_id not in valid_ids:
        await query.answer("That category is no longer available.", show_alert=True)
        return

    await query.answer()
    selected = set(await logger.get_user_categories(internal_id))
    await logger.set_user_category(internal_id, category_id, category_id not in selected, True)
    await _render_categories(query, internal_id, user.id)


async def register_configured_channel(application: Application):
    """Verify and register the configured public channel as a subscriber."""
    if BOT_CHANNEL_ID is None:
        return

    valid_ids = {profile.id for profile in enabled_categories()}
    if BOT_CHANNEL_CATEGORY_ID not in valid_ids:
        raise RuntimeError(
            f"BOT_CHANNEL_CATEGORY_ID '{BOT_CHANNEL_CATEGORY_ID}' is not an enabled category"
        )

    bot_user = await application.bot.get_me()
    chat = await application.bot.get_chat(BOT_CHANNEL_ID)
    member = await application.bot.get_chat_member(BOT_CHANNEL_ID, bot_user.id)

    if member.status not in {"administrator", "creator"}:
        raise RuntimeError(
            f"Bot is not an admin of BOT_CHANNEL_ID={BOT_CHANNEL_ID} "
            f"(status={member.status})"
        )

    can_post = getattr(member, "can_post_messages", None)
    if can_post is False:
        raise RuntimeError(
            f"Bot is an administrator of BOT_CHANNEL_ID={BOT_CHANNEL_ID} "
            "but does not have permission to post messages"
        )

    destination_id = await logger.ensure_channel_destination(BOT_CHANNEL_ID, getattr(chat, "title", "") or "", True)
    await logger.set_user_category(destination_id, BOT_CHANNEL_CATEGORY_ID, True, True)

    print(
        f"[SUBSCRIBER CHANNEL] Registered '{getattr(chat, 'title', '')}' "
        f"({BOT_CHANNEL_ID}) under category '{BOT_CHANNEL_CATEGORY_ID}'"
    )


def _next_backoff_attempt_at(attempts):
    """Exponential backoff timestamp for the given (post-increment)
    attempt count, or None once MAX_ATTEMPTS has been reached and no
    further retry should be scheduled.
    """
    if attempts >= MAX_ATTEMPTS:
        return None
    delay = min(
        RUNTIME.notification_backoff_cap_seconds,
        2 ** attempts * RUNTIME.notification_backoff_base_seconds,
    )
    return (datetime.now() + timedelta(seconds=delay)).isoformat()


async def _send_one(notification):
    notification_id = notification["Notification ID"]
    telegram_user_id = int(notification["Telegram User ID"])
    job_uuid = notification["Job UUID"]
    category_id = notification["Category ID"]

    job = await logger.get_job(job_uuid)
    if not job:
        # A missing job record can never be delivered: spend one real
        # failure attempt and let the max-attempt gate stop further
        # claims (claims themselves never spend attempts; see
        # claim_pending_user_notifications). While Attempts is still
        # under MAX_ATTEMPTS this must still schedule a real backoff
        # window -- leaving "Next Attempt At" untouched (None) would
        # keep whatever timestamp is already on the row, which is
        # always already-expired for a freshly claimed "Sending" row,
        # so the notification would be immediately reclaimable on the
        # very next poll tick with no backoff at all.
        attempts = int(notification.get("Attempts") or 0) + 1
        next_attempt = _next_backoff_attempt_at(attempts)
        await logger.update_user_notification(notification_id, "Failed", attempts, "Job record not found", next_attempt)
        return

    rendered = user_renderer.render_user(job, category_id)
    message = rendered["text"]

    reply_markup = None
    button_url = rendered.get("button_url")
    if button_url:
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Open Project", url=button_url)]]
        )

    try:
        await user_messaging.notify_user(
            telegram_user_id,
            {
                "text": message,
                "parse_mode": ParseMode.HTML,
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )
    except RetryAfter as exc:
        # Telegram backpressure, not a delivery failure: record it under a
        # distinct status so it never shares the MAX_ATTEMPTS budget with
        # genuine failures (see claim_pending_user_notifications).
        retry_at = datetime.now() + timedelta(seconds=float(exc.retry_after))
        await logger.update_user_notification(notification_id, "RateLimited", notification.get("Attempts", "0"), f"Telegram rate limit: retry after {exc.retry_after}s", retry_at.isoformat())
    except Forbidden:
        # User blocked the bot or otherwise made the chat unavailable.
        # Deactivate the destination so no further notifications are even
        # queued for them (claim_pending_user_notifications excludes
        # inactive users), and cancel their entire not-yet-delivered backlog
        # -- the same discard-on-unsubscribe semantics /stop uses. Without
        # the cancel, the pending rows would only defer and then ALL become
        # claimable again the moment /start flips "Is Active" back to 1, so
        # a user who blocked the bot would come back to a burst of stale
        # jobs. The row being delivered right now is Cancelled terminally so
        # it, too, can never re-enter the claim set.
        await logger.set_destination_active(telegram_user_id, False, False)
        await logger.cancel_pending_user_notifications(telegram_user_id, save=False)
        await logger.update_user_notification(
            notification_id,
            "Cancelled",
            notification.get("Attempts", "0"),
            "Telegram user blocked the bot or chat is unavailable",
            None,
        )
    except TelegramError as exc:
        attempts = int(notification.get("Attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            status = "Failed"
            next_attempt = None
        else:
            status = "Failed"
            delay = min(RUNTIME.notification_backoff_cap_seconds, 2 ** attempts * RUNTIME.notification_backoff_base_seconds)
            next_attempt = (datetime.now() + timedelta(seconds=delay)).isoformat()

        await logger.update_user_notification(notification_id, status, attempts, str(exc), next_attempt)
    except Exception as exc:
        attempts = int(notification.get("Attempts") or 0) + 1
        next_attempt = None
        if attempts < MAX_ATTEMPTS:
            next_attempt = (
                datetime.now() + timedelta(seconds=min(RUNTIME.notification_backoff_cap_seconds, 2 ** attempts * RUNTIME.notification_backoff_base_seconds))
            ).isoformat()

        await logger.update_user_notification(notification_id, "Failed", attempts, str(exc), next_attempt)
    else:
        # Success: the failure count stays untouched -- Attempts counts
        # real delivery failures, not deliveries.
        await logger.update_user_notification(notification_id, "Sent", notification.get("Attempts", "0"), "", "")


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    React the moment a user's relationship with the bot changes, instead
    of only finding out reactively the next time a send happens to fail.

    Telegram pushes a my_chat_member update to the bot when a user
    blocks/leaves it or becomes reachable again. Without this
    handler, "Is Active" only ever gets corrected by _send_one's Forbidden
    handler above, which means a user who stops the bot and then never
    happens to match another notification stays "Is Active"="1" in the DB
    indefinitely, even though they are not actually reachable.

    Telegram can report a blocked/left private chat as an unreachable
    membership state. That is a reachability change, not a new subscription
    event. Explicit subscription analytics are recorded only by /start and
    /stop.

    Scoped to private 1:1 chats only (update.effective_chat.type ==
    "private") so a status change on the configured public channel
    destination -- a different, admin-membership concept entirely -- is
    never misread as a user unsubscribing.
    """

    chat = update.effective_chat
    if chat is None or chat.type != "private":
        return

    new_status = update.my_chat_member.new_chat_member.status
    chat_id = str(chat.id)

    if new_status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
        await logger.set_destination_active(chat_id, False)
    elif new_status == ChatMemberStatus.MEMBER:
        # Becoming reachable again does not mean the user subscribed again.
        # Only /start reactivates the subscription.
        return


async def user_notification_worker():
    # Beats per-worker liveness while idling so the healthcheck can observe
    # this worker as alive even when the poll interval exceeds the staleness
    # window (see heartbeat.sleep_with_beats).
    semaphore = asyncio.Semaphore(DELIVERY_CONCURRENCY)

    async def limited_send(notification):
        async with semaphore:
            await _send_one(notification)

    while True:
        try:
            batch = await logger.claim_pending_user_notifications(BATCH_SIZE)
            if batch:
                await asyncio.gather(
                    *(limited_send(item) for item in batch),
                    return_exceptions=True,
                )
            else:
                await sleep_with_beats(POLL_INTERVAL, "user_notifications", STATE_RUNNING)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.log_error("User Notification Worker", exc, "", save=True)
            await sleep_with_beats(POLL_INTERVAL, "user_notifications", STATE_RUNNING)


def create_user_bot_application():
    # NOTE: this application's lifecycle is driven manually by
    # TelegramCommandSurface (initialize() -> start() ->
    # updater.start_polling(), and stop() -> updater.stop() ->
    # application.stop()/shutdown()), and the equivalent manual
    # startup sequence in app.startup.default_startup_steps(). A
    # `post_init` hook registered on the builder here is only ever
    # invoked by python-telegram-bot's own run_polling()/run_webhook()
    # convenience methods, neither of which this codebase uses, so it
    # would never run -- it previously sat on the builder looking like
    # part of the startup path while silently never firing.
    # register_channel() and reset_inflight_notifications() in
    # app.startup are the single authoritative place those two steps
    # happen; do not reintroduce a second one here.
    application = Application.builder().token(get_bot_token()).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("categories", categories_command))
    application.add_handler(CommandHandler("sources", sources_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CallbackQueryHandler(category_callback))
    # ChatMemberHandler.MY_CHAT_MEMBER: updates about the bot's own
    # membership status (blocked/stopped/unblocked), not other members'
    # statuses in a group -- Telegram sends these by default without
    # needing an explicit allowed_updates change on start_polling().
    application.add_handler(
        ChatMemberHandler(my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    return application