import re


ARABIC_TRANSLATION = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
    # Taa marbuta -> haa. Very common informal-writing variance in
    # Telegram posts (e.g. "لوحه" vs "لوحة"); without this, a keyword
    # spelled with one form never matches a post spelled with the
    # other. Trades a small amount of over-merging for meaningfully
    # better recall.
    "ة": "ه",
})


def normalize(text: str) -> str:
    text = text.lower()

    text = text.translate(ARABIC_TRANSLATION)

    # Remove Arabic diacritics
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)

    # Remove Arabic tatweel/kashida (used for stylistic stretching,
    # e.g. "مـــمتاز") which otherwise breaks substring matches.
    text = re.sub(r'\u0640', '', text)

    # Telegram users often concatenate Latin and Arabic text without a
    # separator (e.g. "PowerBIلوحة" or "لوحةPowerBI").  The keyword
    # matcher intentionally uses Unicode word boundaries, so without
    # a separator a valid Latin/Arabic keyword can become part of one
    # larger token and fail to match.  Insert a boundary in both
    # directions before punctuation/separator cleanup.
    text = re.sub(r'(?<=[A-Za-z])(?=[\u0600-\u06FF])', ' ', text)
    text = re.sub(r'(?<=[\u0600-\u06FF])(?=[A-Za-z])', ' ', text)

    # Replace separators with spaces
    text = re.sub(r'[-_/\\|]', ' ', text)

    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)

    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)

    return text.strip()
