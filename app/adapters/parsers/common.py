import re

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


