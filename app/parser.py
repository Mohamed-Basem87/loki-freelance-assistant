import re


def _extract_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else ""


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
    if "nafezly" in source_name:
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
    job["title"] = _fallback_title(text)
    job["description"] = _normalize_description(text)
    job["url"] = _extract_url(text)

    return job
