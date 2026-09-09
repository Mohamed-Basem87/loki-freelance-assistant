"""Default Telegram subscriber message renderer.

This adapter is the authoritative subscriber-message builder. Subscriber
delivery is a personalized destination, not a new message format: it reuses the
channel-style builder so subscribers receive the same normalized source name,
category heading, tags, description, and project button content as the public
category channel.
"""
from app.ports import UserMessageRenderer
from app.message_builder import build_job_message, safe_button_url
from app.categories.registry import enabled_categories

class TelegramUserMessageRenderer(UserMessageRenderer):
    id = "telegram-user-html"
    def render_user(self, job_row, category_id):
        message = build_user_notification(job_row, category_id)
        return {
            "text": message,
            "format": "HTML",
            "button_url": safe_button_url(job_row.get("URL") or ""),
        }


def build_user_notification(job_row, category_id):
    """Build the subscriber message using the exact public-channel format."""
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

    profile = next(
        (p for p in enabled_categories() if p.id == category_id),
        None,
    )
    category_name = profile.name if profile is not None else ""

    return build_job_message(
        title=job_row.get("Title") or "",
        description=job_row.get("Description") or "",
        source=job_row.get("Source") or "",
        reason=job_row.get("Decision Reason") or "",
        url=job_row.get("URL") or "",
        budget="",
        categories=categories,
        category_name=category_name,
        ai_used=(job_row.get("Category Selection Method") == "llm"),
        channel_style=True,
    )
