"""User-facing Telegram bot and durable multi-user notification delivery."""

import asyncio
from datetime import datetime, timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.categories.registry import enabled_categories
from app.config import BOT_TOKEN
from app.logger import logger
from app.message_builder import build_job_message

DELIVERY_CONCURRENCY = 10
POLL_INTERVAL = 1.0
BATCH_SIZE = 20
MAX_ATTEMPTS = 5

# Shared Bot instance for notification delivery. The existing source/channel
# notifiers keep their own instance for backwards compatibility.
bot = Bot(BOT_TOKEN)


def _category_keyboard(selected_ids):
    selected_ids = set(selected_ids)
    rows = []
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


async def _render_categories(query, user_id, *, edit=True):
    selected = await logger.run(logger.get_user_categories, user_id)
    text = (
        "Choose the job categories you want to receive.\n\n"
        "You can select more than one."
    )
    markup = _category_keyboard(selected)
    if edit:
        await query.edit_message_text(text=text, reply_markup=markup)
    else:
        await query.message.reply_text(text=text, reply_markup=markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or update.effective_chat is None:
        return

    internal_id = await logger.run(
        logger.ensure_user,
        user.id,
        user.username or "",
        user.first_name or "",
    )

    await update.message.reply_text(
        "Welcome to Loki Jobs 👋\n\n"
        "Choose the categories you want to receive.",
        reply_markup=_category_keyboard(
            await logger.run(logger.get_user_categories, internal_id)
        ),
    )


async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    internal_id = await logger.run(
        logger.ensure_user,
        user.id,
        user.username or "",
        user.first_name or "",
    )
    await update.message.reply_text(
        "Choose the job categories you want to receive.\n\n"
        "You can select more than one.",
        reply_markup=_category_keyboard(
            await logger.run(logger.get_user_categories, internal_id)
        ),
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()

    internal_id = await logger.run(
        logger.ensure_user,
        user.id,
        user.username or "",
        user.first_name or "",
    )

    data = query.data or ""
    if data == "done":
        selected = await logger.run(logger.get_user_categories, internal_id)
        names = {
            profile.id: profile.name for profile in enabled_categories()
        }
        selected_names = [names[cid] for cid in selected if cid in names]
        if selected_names:
            text = "✅ Saved. You'll receive:\n\n" + "\n".join(
                f"• {name}" for name in selected_names
            )
        else:
            text = (
                "No categories selected. You can use /categories "
                "whenever you want to subscribe."
            )
        await query.edit_message_text(text=text)
        return

    if not data.startswith("cat:"):
        return

    category_id = data[4:]
    valid_ids = {profile.id for profile in enabled_categories()}
    if category_id not in valid_ids:
        await query.answer("That category is no longer available.", show_alert=True)
        return

    selected = set(await logger.run(logger.get_user_categories, internal_id))
    enabled = category_id not in selected

    await logger.run(
        logger.set_user_category,
        internal_id,
        category_id,
        enabled,
        True,
    )
    await _render_categories(query, internal_id)


async def post_init(application: Application):
    # Recover notifications that were in-flight when Loki stopped.
    await logger.run(logger.reset_sending_user_notifications)


def build_user_notification(job_row, category_id):
    """Build the subscriber message using the exact public-channel format.

    Subscriber delivery is a personalized destination, not a new message
    format. Reuse the channel-style builder so subscribers receive the same
    normalized source name, category heading, tags, description, and project
    button content as the public category channel.
    """
    categories = job_row.get("Categories") or ""
    if isinstance(categories, str):
        categories = [item.strip() for item in categories.split(",") if item.strip()]

    # A legacy/recovery row may not have the stored keyword categories. Keep
    # the final category available as a minimal fallback without changing the
    # normal path, which uses the exact categories already used by the public
    # channel.
    if not categories and category_id:
        profile = next(
            (p for p in enabled_categories() if p.id == category_id),
            None,
        )
        if profile is not None:
            categories = [profile.id]

    return build_job_message(
        title=job_row.get("Title") or "",
        description=job_row.get("Description") or "",
        source=job_row.get("Source") or "",
        reason=job_row.get("Decision Reason") or "",
        url=job_row.get("URL") or "",
        budget="",
        categories=categories,
        ai_used=(job_row.get("Category Selection Method") == "llm"),
        channel_style=True,
    )


async def _send_one(notification):
    notification_id = notification["Notification ID"]
    telegram_user_id = int(notification["Telegram User ID"])
    job_uuid = notification["Job UUID"]
    category_id = notification["Category ID"]

    job = await logger.run(logger.get_job, job_uuid)
    if not job:
        await logger.run(
            logger.update_user_notification,
            notification_id,
            "Failed",
            notification.get("Attempts", "1"),
            "Job record not found",
            datetime.now().isoformat(),
        )
        return

    message = build_user_notification(job, category_id)

    reply_markup = None
    if job.get("URL"):
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Open Project", url=job["URL"])]]
        )

    try:
        await bot.send_message(
            chat_id=telegram_user_id,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except RetryAfter as exc:
        retry_at = datetime.now() + timedelta(seconds=float(exc.retry_after))
        await logger.run(
            logger.update_user_notification,
            notification_id,
            "Failed",
            notification.get("Attempts", "1"),
            f"Telegram rate limit: retry after {exc.retry_after}s",
            retry_at.isoformat(),
        )
    except Forbidden:
        # User blocked the bot or otherwise made the chat unavailable.
        await logger.run(
            logger.set_user_active,
            telegram_user_id,
            False,
            False,
        )
        await logger.run(
            logger.update_user_notification,
            notification_id,
            "Failed",
            notification.get("Attempts", "1"),
            "Telegram user blocked the bot or chat is unavailable",
            None,
        )
    except TelegramError as exc:
        attempts = int(notification.get("Attempts") or 1)
        if attempts >= MAX_ATTEMPTS:
            status = "Failed"
            next_attempt = None
        else:
            status = "Failed"
            delay = min(300, 2 ** attempts * 5)
            next_attempt = (datetime.now() + timedelta(seconds=delay)).isoformat()

        await logger.run(
            logger.update_user_notification,
            notification_id,
            status,
            attempts,
            str(exc),
            next_attempt,
        )
    except Exception as exc:
        attempts = int(notification.get("Attempts") or 1)
        next_attempt = None
        if attempts < MAX_ATTEMPTS:
            next_attempt = (
                datetime.now() + timedelta(seconds=min(300, 2 ** attempts * 5))
            ).isoformat()

        await logger.run(
            logger.update_user_notification,
            notification_id,
            "Failed",
            attempts,
            str(exc),
            next_attempt,
        )
    else:
        await logger.run(
            logger.update_user_notification,
            notification_id,
            "Sent",
            notification.get("Attempts", "1"),
            "",
            "",
        )


async def user_notification_worker():
    semaphore = asyncio.Semaphore(DELIVERY_CONCURRENCY)

    async def limited_send(notification):
        async with semaphore:
            await _send_one(notification)

    while True:
        try:
            batch = await logger.run(
                logger.claim_pending_user_notifications,
                BATCH_SIZE,
            )
            if batch:
                await asyncio.gather(
                    *(limited_send(item) for item in batch),
                    return_exceptions=True,
                )
            else:
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await logger.run(
                logger.log_error,
                "User Notification Worker",
                exc,
                "",
                save=True,
            )
            await asyncio.sleep(POLL_INTERVAL)


def create_user_bot_application():
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("categories", categories_command))
    application.add_handler(CallbackQueryHandler(category_callback))
    return application
