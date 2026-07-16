from html import escape

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from app.config import (
    BOT_CHANNEL_ID,
    BOT_TOKEN,
)
from app.logger import logger


MAX_DESCRIPTION_LENGTH = 3000

bot = Bot(BOT_TOKEN)


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
    score: int | None = None,
    categories=None,
    ai_used: bool = False,
):

    if not BOT_CHANNEL_ID:
        return True

    if categories is None:
        categories = []

    if description and len(description) > MAX_DESCRIPTION_LENGTH:
        description = (
            description[: MAX_DESCRIPTION_LENGTH - 3].rstrip()
            + "..."
        )

    message = f"""🚀 <b>New Data Analysis Opportunity</b>

📄 <b>{escape(title)}</b>
🏢 <b>Platform</b>
{escape(source)}"""

    if budget:
        message += f"""

💰 <b>Budget</b>
{escape(budget)}"""

    if description:
        message += f"""

────────────────────────
📋 <b>Description</b>
{escape(description)}"""

    if categories:
        hashtags = " ".join(
            f"#{category.replace(' ', '')}"
            for category in categories
        )

        message += f"""

────────────────────────
🏷 <b>Tags</b>
{escape(hashtags)}"""

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

        logger.log_notification(
            job_uuid,
            "Telegram Channel",
            "Sent",
        )

        return True

    except Exception as e:
        logger.log_notification(
            job_uuid,
            "Telegram Channel",
            "Failed",
        )

        logger.log_error(
            "ChannelNotifier",
            e,
        )

        return False
