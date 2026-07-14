import re


def parse_job(source: str, text: str):

    job = {
        "title": "",
        "description": "",
        "budget": "",
        "url": "",
        "source": source,
        "raw_text": text,
    }

    if source and "nafezly" in source.lower():

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

        url = re.search(
            r"https?://\S+",
            text,
        )

        if title:
            job["title"] = title.group(1).strip()

        if description:
            job["description"] = description.group(1).strip()

        if budget:
            job["budget"] = budget.group(1).strip()

        if url:
            job["url"] = url.group(0)

    else:
        job["description"] = text

    return job
