import re


ARABIC_TRANSLATION = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
})


def normalize(text: str) -> str:
    text = text.lower()

    text = text.translate(ARABIC_TRANSLATION)

    # Remove Arabic diacritics
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)

    # Replace separators with spaces
    text = re.sub(r'[-_/\\|]', ' ', text)

    # Remove punctuation
    text = re.sub(r'[^\w\s]', ' ', text)

    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)

    return text.strip()
