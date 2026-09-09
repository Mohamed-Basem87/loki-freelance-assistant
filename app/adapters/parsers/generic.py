from .common import _extract_url, _fallback_title, _normalize_description


def parse(source: str, text: str) -> dict[str, str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = _fallback_title(text)
    title_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    description_text = "\n".join(lines[title_index + 1:]) if title_index is not None else ""
    return {
        "title": title,
        "description": _normalize_description(description_text),
        "budget": "",
        "url": _extract_url(text),
        "source": source,
        "raw_text": text,
    }
