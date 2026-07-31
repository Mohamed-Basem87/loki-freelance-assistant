from html import escape


# Telegram's hard limit on a single text message. Kept a little under
# the real 4096 so we always have headroom after escaping and after
# appending the reason/tags block, instead of finding out via a
# failed send.
TELEGRAM_MESSAGE_LIMIT = 4096
_SAFETY_MARGIN = 96
MAX_MESSAGE_LENGTH = TELEGRAM_MESSAGE_LIMIT - _SAFETY_MARGIN

MAX_DESCRIPTION_LENGTH = 3000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def build_job_message(
    *,
    title: str,
    description: str = "",
    source: str,
    reason: str,
    url: str = "",
    budget: str = "",
    categories=None,
    ai_used: bool = False,
    channel_style: bool = False,
) -> str:
    """
    Build the HTML-formatted job notification message shared by both
    the direct (personal chat) and channel notifiers. `channel_style`
    switches between the two previously-independent formats (hashtag
    categories + no AI/Direct header vs. plain categories + header),
    which is the only real difference between what notifier.py and
    channel_notifier.py used to build separately.

    The full message (not just `description`) is bounded to Telegram's
    message-length limit -- title/budget/reason can all be arbitrarily
    long (an LLM-generated `reason` in particular), so bounding only
    the description was not sufficient to guarantee the send succeeds.
    """

    if categories is None:
        categories = []

    description = _truncate(description, MAX_DESCRIPTION_LENGTH) if description else ""

    if channel_style:
        header = "🚀 <b>New Data Analysis Opportunity</b>\n\n"
        header += f"📄 <b>{escape(title)}</b>\n"
        header += f"🏢 <b>Platform</b>\n{escape(source)}"
    else:
        ai_header = (
            "🧠 <b>AI Recommendation</b>"
            if ai_used
            else "⚡ <b>Direct Match</b>"
        )
        header = (
            f"🚀 <b>New Freelance Opportunity</b>\n\n"
            f"{ai_header}\n\n"
            f"🏢 <b>Platform</b>\n{escape(source)}\n\n"
            f"📄 <b>Project</b>\n{escape(title)}"
        )

    sections = []

    if budget:
        sections.append(f"💰 <b>Budget</b>\n{escape(budget)}")

    if description:
        sections.append(
            f"────────────────────────\n\n"
            f"📋 <b>Description</b>\n\n{escape(description)}"
        )

    if categories:
        if channel_style:
            hashtags = " ".join(
                f"#{category.replace(' ', '')}" for category in categories
            )
            sections.append(
                f"────────────────────────\n"
                f"🏷 <b>Tags</b>\n{escape(hashtags)}"
            )
        else:
            sections.append(
                f"🏷 <b>Categories</b>\n{escape(' • '.join(categories))}"
            )

    if reason and not channel_style:
        sections.append(
            f"────────────────────────\n\n💡 <b>Reason</b>\n\n{escape(reason)}"
        )

    message = header
    for section in sections:
        message += "\n\n" + section

    if len(message) > MAX_MESSAGE_LENGTH:
        # Bounding just the description wasn't enough (title/budget/
        # reason can push it over on their own) -- fall back to a
        # hard truncation of the whole message so the send always
        # succeeds instead of silently failing.
        message = _truncate(message, MAX_MESSAGE_LENGTH)

    return message
