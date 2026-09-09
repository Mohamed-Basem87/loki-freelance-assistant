from app.ports import MessageRenderer
from app.message_builder import build_job_message, safe_button_url

class TelegramMessageRenderer(MessageRenderer):
    id = "telegram-html"
    def render(self, payload):
        text = build_job_message(title=payload.get("title", ""), description=payload.get("description", ""), source=payload.get("source", ""), reason=payload.get("reason", ""), url=payload.get("url", ""), budget=payload.get("budget", ""), categories=payload.get("categories"), ai_used=payload.get("ai_used", False), channel_style=False)
        return {"text": text, "button_url": safe_button_url(payload.get("url", "")), "format": "HTML", "payload": payload}
