from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from app.config import BOT_CHANNEL_ID
from app.logger import logger
from app.message_builder import build_job_message
from app.telegram_bot import bot


async def send_channel_notification(
    *,
    job_uuid: str,
    title: str,
    description: str = "",
    source: str,
    decision: str,
    reason: str,
    url: str = "",
    budget: str = "",
    categories=None,
    core_hit_count: int = 0,
    supporting_weight: int = 0,
    ai_used: bool = False,
):

    if not BOT_CHANNEL_ID:
        return True

    message = build_job_message(
        title=title,
        description=description,
        source=source,
        reason=reason,
        url=url,
        budget=budget,
        categories=categories,
        ai_used=ai_used,
        channel_style=True,
    )

    keyboard = None

    if url:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔗 Open Project",
                        url=url,
                    )
                ]
            ]
        )

    try:
        await bot.send_message(
            chat_id=BOT_CHANNEL_ID,
            text=message,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        return True

    except Exception as e:

        logger.log_error(
            "ChannelNotifier",
            e,
            job_uuid,
            save=False,
        )

        return False
