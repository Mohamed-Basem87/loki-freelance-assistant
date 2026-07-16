from html import escape

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode

from app.config import (
    BOT_CHAT_ID,
    BOT_CHANNEL_ID,
    BOT_TOKEN,
)
from app.logger import logger


MAX_DESCRIPTION_LENGTH = 3000

bot = Bot(BOT_TOKEN)


async def send_notification(
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

    if categories is None:
        categories = []

    if description and len(description) > MAX_DESCRIPTION_LENGTH:
        description = (
            description[: MAX_DESCRIPTION_LENGTH - 3].rstrip() + "..."
        )

    header = (
        "🧠 <b>AI Recommendation</b>"
        if ai_used
        else "⚡ <b>Direct Match</b>"
    )

    message = f"""🚀 <b>New Freelance Opportunity</b>

{header}

🏢 <b>Platform</b>
{escape(source)}

📄 <b>Project</b>
{escape(title)}
"""

    if budget:
        message += f"""

💰 <b>Budget</b>
{escape(budget)}
"""

    if score is not None:
        message += f"""

⭐ <b>Keyword Score</b>
{score}
"""

    if categories:
        message += f"""

🏷 <b>Categories</b>
{escape(" • ".join(categories))}
"""

    if description:
        message += f"""

────────────────────────

📋 <b>Description</b>

{escape(description)}
"""

    message += f"""

────────────────────────

💡 <b>Reason</b>

{escape(reason)}
"""

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

    targets = [BOT_CHAT_ID]

    if BOT_CHANNEL_ID:
        targets.append(BOT_CHANNEL_ID)

    try:
        for chat_id in targets:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

        logger.log_notification(
            job_uuid,
            "Telegram",
            "Sent",
        )

        return True

    except Exception as e:
        logger.log_notification(
            job_uuid,
            "Telegram",
            "Failed",
        )

        logger.log_error(
            "Notifier",
            e,
        )

        return False