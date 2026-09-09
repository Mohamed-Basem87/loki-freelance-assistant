import re
from .common import _extract_url, _fallback_title, _normalize_description

_MOSTAQL_PROFILE_BADGES = {
    "صانع محتوى", "مبرمج", "مطور", "مطور مواقع", "مطور تطبيقات", "مصمم",
    "مصمم جرافيك", "كاتب", "مترجم", "مسوق", "مسوق رقمي", "مدخل بيانات",
    "معلم", "مدير مشروع",
}
_DESCRIPTION_MARKER_RE = re.compile(r"~{2,}\s*\S*الوصف\S*\s*~{2,}")
_HEADER_MARKS_RE = re.compile("[\u200e\u200f\u202a-\u202e\ufeff]")
_BUDGET_LINE_RE = re.compile(r"\U0001f4b5\ufe0f?\s*(.+)")


def _is_mostaql_source(source: str) -> bool:
    source_lower = (source or "").casefold()
    return "mostaql" in source_lower or "مستقل" in (source or "")


def _strip_mostaql_header(lines: list[str], source: str) -> tuple[list[str], str]:
    if not _is_mostaql_source(source):
        return lines, ""
    marker_index = next((i for i, line in enumerate(lines) if _DESCRIPTION_MARKER_RE.search(line)), None)
    if marker_index is not None:
        title_index = next((i for i, line in enumerate(lines) if line.strip()), None)
        header_start = title_index + 1 if title_index is not None else 0
        budget = ""
        for line in lines[header_start:marker_index]:
            match = _BUDGET_LINE_RE.match(_HEADER_MARKS_RE.sub("", line).strip())
            if match:
                budget = match.group(1).strip()
                break
        return lines[:header_start] + lines[marker_index + 1:], budget
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return lines, ""
    badge = lines[first_index].strip()
    if badge.casefold() not in _MOSTAQL_PROFILE_BADGES:
        return lines, ""
    following_index = next((i for i in range(first_index + 1, len(lines)) if lines[i].strip()), None)
    if following_index is None:
        return lines, ""
    return lines[:first_index] + lines[first_index + 1:], ""


def parse(source: str, text: str) -> dict[str, str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines, header_budget = _strip_mostaql_header(lines, source)
    title = _fallback_title("\n".join(lines))
    title_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    description_text = "\n".join(lines[title_index + 1:]) if title_index is not None else ""
    return {
        "title": title,
        "description": _normalize_description(description_text),
        "budget": header_budget,
        "url": _extract_url(text),
        "source": source,
        "raw_text": text,
    }
