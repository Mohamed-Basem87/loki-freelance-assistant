from html import escape

from app.config import source_display_name


# Telegram's hard limit on a single text message. Kept a little under
# the real 4096 so we always have headroom after escaping and after
# appending the reason/tags block, instead of finding out via a
# failed send.
TELEGRAM_MESSAGE_LIMIT = 4096
_SAFETY_MARGIN = 96
MAX_MESSAGE_LENGTH = TELEGRAM_MESSAGE_LIMIT - _SAFETY_MARGIN

MAX_DESCRIPTION_LENGTH = 3000

# The LLM-generated `reason` is the other field (besides description)
# that's genuinely unbounded in practice. Pre-truncating it here, on
# the plain-text value before it's HTML-escaped and wrapped in
# section markup, means the *final* whole-message safety truncation
# below is far less likely to ever need to fire at all -- it's now
# only a last-resort fallback for pathological combinations (many
# categories, long title/budget/source, etc.), not the normal path
# for a long reason.
MAX_REASON_LENGTH = 1200


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def _safe_html_truncate(html: str, limit: int) -> str:
    """
    Truncate an already-built HTML message to (approximately) `limit`
    characters without ever leaving malformed HTML behind.

    build_job_message only ever produces simple, non-nested
    <b>...</b> spans, so this doesn't need a real HTML parser: cut the
    text, drop any tag fragment left dangling right at the cut point
    (an unterminated "<...>", e.g. the cut landing inside "<b>" or
    "</b>"), then close any <b> span that's still open as a result.
    The handful of extra characters a closing tag can add is covered
    by MAX_MESSAGE_LENGTH's existing safety margin below Telegram's
    real 4096-character hard limit.
    """
    if len(html) <= limit:
        return html

    truncated = html[: max(limit - 3, 0)].rstrip()

    last_lt = truncated.rfind("<")
    last_gt = truncated.rfind(">")
    if last_lt > last_gt:
        # The cut landed inside a tag -- drop the incomplete fragment
        # rather than risk emitting a broken/partial one.
        truncated = truncated[:last_lt].rstrip()

    open_count = truncated.count("<b>")
    close_count = truncated.count("</b>")

    if open_count > close_count:
        truncated += "</b>" * (open_count - close_count)

    return truncated + "..."


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
    reason = _truncate(reason, MAX_REASON_LENGTH) if reason else reason

    display_source = source_display_name(source) if channel_style else source

    if channel_style:
        header = "🚀 <b>New Data Analysis Opportunity</b>\n\n"
        header += f"📄 <b>{escape(title)}</b>\n"
        header += f"🏢 <b>Platform</b>\n{escape(display_source)}"
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
        # Bounding description/reason individually wasn't always
        # enough on its own (title/budget/categories can still push a
        # pathological combination over the limit) -- fall back to a
        # hard truncation of the whole message so the send always
        # succeeds instead of silently failing. Tag-aware so this
        # never emits malformed HTML (see _safe_html_truncate).
        message = _safe_html_truncate(message, MAX_MESSAGE_LENGTH)

    return message
