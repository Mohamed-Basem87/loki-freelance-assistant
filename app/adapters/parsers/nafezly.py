import re
from .common import _extract_url, _fallback_title, _normalize_description


def parse(source: str, text: str) -> dict[str, str]:
    title = re.search(r"عنوان المشروع\s*:\s*(.+)", text, re.IGNORECASE)
    description = re.search(r"تفاصيل المشروع\s*:\s*(.*?)(?:\s*الميزانية|$)", text, re.DOTALL)
    budget = re.search(r"الميزانية\s*:\s*(.+)", text)
    return {
        "title": title.group(1).strip() if title else _fallback_title(text),
        "description": _normalize_description(description.group(1)) if description else _normalize_description(text),
        "budget": budget.group(1).strip() if budget else "",
        "url": _extract_url(text),
        "source": source,
        "raw_text": text,
    }
