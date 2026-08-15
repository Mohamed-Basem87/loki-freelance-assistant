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

    # Replace separators with spaces
    text = re.sub(r'[-_/\\|]', ' ', text)

    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)

    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)

    return text.strip()
