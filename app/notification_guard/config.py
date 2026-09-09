from pathlib import Path
import os

from dotenv import load_dotenv


# The notification guard is intentionally independently importable.
# It may be initialized before app.config is imported, so it must
# load the project's .env itself rather than relying on app.config's
# import-time load_dotenv() side effect.

BASE_DIR = Path(__file__).resolve().parent.parent.parent

load_dotenv(BASE_DIR / ".env")


_ENABLED_VALUES = {"1", "true", "yes", "on"}


NOTIFICATION_GUARD_ENABLED = (
    os.getenv(
        "NOTIFICATION_GUARD_ENABLED",
        "false",
    )
    .strip()
    .lower()
    in _ENABLED_VALUES
)


NOTIFICATION_GUARD_API_KEYS = [
    key.strip()
    for key in os.getenv("GROQ_NOTIFICATION_GUARD_API_KEY", "").split(",")
    if key.strip()
]


# Guard model order is deployment configuration, not orchestration code.
NOTIFICATION_GUARD_MODELS = tuple(
    x.strip() for x in os.getenv("GROQ_NOTIFICATION_GUARD_MODELS", os.getenv("GROQ_MODELS", "")).split(",") if x.strip()
)

NOTIFICATION_GUARD_MAX_RETRIES = int(
    os.getenv(
        "GROQ_NOTIFICATION_GUARD_MAX_RETRIES",
        "2",
    )
)

NOTIFICATION_GUARD_PROVIDERS = tuple(x.strip() for x in os.getenv("NOTIFICATION_GUARD_PROVIDERS", "groq").split(",") if x.strip())


def validate():
    if NOTIFICATION_GUARD_MAX_RETRIES < 1:
        raise ValueError("GROQ_NOTIFICATION_GUARD_MAX_RETRIES must be >= 1")
    if NOTIFICATION_GUARD_ENABLED and not NOTIFICATION_GUARD_API_KEYS:
        raise ValueError("NOTIFICATION_GUARD_ENABLED=true requires GROQ_NOTIFICATION_GUARD_API_KEY")
    if NOTIFICATION_GUARD_ENABLED and not NOTIFICATION_GUARD_MODELS:
        raise ValueError("NOTIFICATION_GUARD_ENABLED=true requires GROQ_NOTIFICATION_GUARD_MODELS or GROQ_MODELS")
    if not NOTIFICATION_GUARD_PROVIDERS:
        raise ValueError("NOTIFICATION_GUARD_PROVIDERS must contain at least one provider")
    unknown = sorted(set(x.lower() for x in NOTIFICATION_GUARD_PROVIDERS) - {"groq"})
    if unknown:
        raise ValueError(f"NOTIFICATION_GUARD_PROVIDERS contains unknown provider(s): {', '.join(unknown)}")


validate()
