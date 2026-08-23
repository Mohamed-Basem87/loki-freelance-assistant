import re


# Mostaql channel posts can prepend a short client/profile profession
# badge before the actual project title.  The badge is metadata, not
# job content, so it must not become the title or classifier input.
# Keep this list deliberately conservative: only exact standalone
# badge labels with a following line are stripped.  A one-line post
# is never stripped, so a legitimate one-line title such as "مبرمج"
# remains intact.
_MOSTAQL_PROFILE_BADGES = {
    "صانع محتوى",
    "مبرمج",
    "مطور",
    "مطور مواقع",
    "مطور تطبيقات",
    "مصمم",
    "مصمم جرافيك",
    "كاتب",
    "مترجم",
    "مسوق",
    "مسوق رقمي",
    "مدخل بيانات",
    "مدير مشروع",
}


def _is_mostaql_source(source: str) -> bool:
    source_lower = (source or "").casefold()
    return "mostaql" in source_lower or "مستقل" in (source or "")


def _strip_mostaql_profile_badge(lines: list[str], source: str) -> list[str]:
    """Remove one known Mostaql profile badge from the message header.

    Only the first non-empty line is eligible, and only when another
    non-empty line follows it.  This prevents a legitimate one-line
    project title from being discarded merely because it happens to
    match a common profession label.
    """
    if not _is_mostaql_source(source):
        return lines

    first_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_index is None:
        return lines

    badge = lines[first_index].strip()
    if badge.casefold() not in _MOSTAQL_PROFILE_BADGES:
        return lines

    following_index = next(
        (
            index
            for index in range(first_index + 1, len(lines))
            if lines[index].strip()
        ),
        None,
    )
    if following_index is None:
        return lines

    return lines[:first_index] + lines[first_index + 1:]


def _extract_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    if not match:
        return ""
    # \S+ is greedy and will happily sweep trailing prose punctuation
    # into the match (a period ending the sentence, a closing paren
    # around the link, a comma before the next clause, etc.), which
    # then becomes part of the "Open Project" button's target URL.
    # Trim it off, but leave balanced-looking punctuation alone (e.g.
    # a URL that's genuinely supposed to end in a closing paren
    # because it opened with one).
    url = match.group(0)
    while url and url[-1] in ".,;:!?)]}\"'":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _fallback_title(text: str) -> str:
    """
    Return the first meaningful non-empty line as the title.
    """
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _normalize_description(text: str) -> str:
    """
    Normalize Telegram message formatting while preserving paragraphs.
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove trailing spaces from lines
    lines = [line.strip() for line in text.split("\n")]

    normalized = []
    previous_blank = False

    for line in lines:
        if not line:
            if not previous_blank:
                normalized.append("")
            previous_blank = True
            continue

        previous_blank = False
        normalized.append(line)

    text = "\n".join(normalized)

    # Single newlines become spaces.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    # Collapse excessive spaces.
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def parse_job(source: str, text: str) -> dict[str, str]:
    job = {
        "title": "",
        "description": "",
        "budget": "",
        "url": "",
        "source": source,
        "raw_text": text,
    }

    source_name = (source or "").lower()

    # -----------------------------
    # Nafezly
    # -----------------------------
    if "nafezly" in source_name or "نفذلي" in (source or ""):
        title = re.search(
            r"عنوان المشروع\s*:\s*(.+)",
            text,
            re.IGNORECASE,
        )

        description = re.search(
            r"تفاصيل المشروع\s*:\s*(.*?)(?:\s*الميزانية|$)",
            text,
            re.DOTALL,
        )

        budget = re.search(
            r"الميزانية\s*:\s*(.+)",
            text,
        )

        if title:
            job["title"] = title.group(1).strip()
        else:
            job["title"] = _fallback_title(text)

        if description:
            job["description"] = _normalize_description(
                description.group(1)
            )
        else:
            job["description"] = _normalize_description(text)

        if budget:
            job["budget"] = budget.group(1).strip()

        job["url"] = _extract_url(text)

        return job

    # -----------------------------
    # Mostaql & Generic channels
    # -----------------------------
    # Parse the header/body from the same line list so a Mostaql
    # profile badge can be removed before it becomes the title or
    # classifier evidence.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = _strip_mostaql_profile_badge(lines, source)

    job["title"] = _fallback_title("\n".join(lines))

    # The first meaningful line is the title. Do not feed that same
    # line into description as well: job_processor combines title and
    # description for classification, so keeping the title here would
    # count title keywords twice and distort supporting weights.
    title_index = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    description_text = (
        "\n".join(lines[title_index + 1:])
        if title_index is not None
        else ""
    )

    job["description"] = _normalize_description(description_text)
    job["url"] = _extract_url(text)

    return job
