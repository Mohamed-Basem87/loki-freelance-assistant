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
# The Mostaql channels' current layout has no الوصف marker: the title is
# followed by a contiguous profile/stats block, then the description.
# Match the known labels tightly so a description line that merely starts
# with one of these emoji is never mistaken for metadata.
_HEADER_LINE_RES = (
    re.compile(r"^👤\ufe0f?\s*"),
    re.compile(r"^💼\ufe0f?\s*"),
    re.compile(r"^💵\ufe0f?\s*"),
    re.compile(r"^⌛\ufe0f?\s*"),
    re.compile(r"^✍\ufe0f?\s*(?:مسجل منذ|تاريخ التسجيل|\d{1,2}\s+\S+\s+\d{4})"),
    re.compile(r"^📊\ufe0f?\s*(?:معدل التوظيف|\d+(?:\.\d+)?%)"),
    re.compile(r"^📂\ufe0f?\s*المشاريع المفتوحة"),
    re.compile(r"^🕒\ufe0f?\s*\d{4}-\d{2}-\d{2}"),
    re.compile(r"^📄\ufe0f?\s*تفاصيل المشروع"),
)
_PROFILE_START_RE = re.compile(r"^👤\ufe0f?\s*")
_DETAILS_LINE_RE = re.compile(r"^📄\ufe0f?\s*تفاصيل المشروع")


def _is_mostaql_source(source: str) -> bool:
    source_lower = (source or "").casefold()
    return "mostaql" in source_lower or "مستقل" in (source or "")


def _header_line(line: str) -> str:
    # Header lines are commonly prefixed with RLM/LRM/BOM and, on a few
    # messages, a zero-width space; strip those before matching.
    return _HEADER_MARKS_RE.sub("", line).replace("\u200b", "").strip()


def _strip_profile_header(lines: list[str]) -> list[str] | None:
    """Remove the marker-less 👤/💼/💵/⌛/✍️/📊/📂/🕒/📄 header block.

    Only strips when the block starts with a 👤 line and consumes only
    contiguous lines matching a known metadata label, so the description
    body is never affected. Returns None when the layout does not match.
    """
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return None
    header_index = next(
        (i for i in range(first_index + 1, len(lines)) if lines[i].strip()),
        None,
    )
    if header_index is None:
        return None
    if not _PROFILE_START_RE.match(_header_line(lines[header_index])):
        return None
    end_index = header_index
    while end_index < len(lines):
        if lines[end_index].strip():
            if not any(
                pattern.match(_header_line(lines[end_index]))
                for pattern in _HEADER_LINE_RES
            ):
                break
            end_index += 1
            continue
        # A blank line normally ends the header block. Older messages put
        # the "📄 تفاصيل المشروع:" divider after a blank line, so step over
        # the blanks only when the next non-empty line is that divider.
        next_index = next(
            (i for i in range(end_index + 1, len(lines)) if lines[i].strip()),
            None,
        )
        if next_index is None or not _DETAILS_LINE_RE.match(
            _header_line(lines[next_index])
        ):
            break
        end_index = next_index
    return lines[:header_index] + lines[end_index:]


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
    stripped = _strip_profile_header(lines)
    if stripped is not None:
        return stripped, ""
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
