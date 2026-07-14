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
            r"تفاصيل المشروع\s*:\s*(.*?)\s*الميزانية",
            text,
            re.DOTALL,
        )

        budget = re.search(
            r"الميزانية\s*:\s*(.+)",
            text,
        )

        if title:
            job["title"] = title.group(1).strip()

        if description:
            job["description"] = description.group(1).strip()

        if budget:
            job["budget"] = budget.group(1).strip()

        job["url"] = _extract_url(text)

        return job

    # -----------------------------
    # Mostaql & Generic channels
    # -----------------------------
    job["title"] = _fallback_title(text)
    job["description"] = text
    job["url"] = _extract_url(text)

    return job
