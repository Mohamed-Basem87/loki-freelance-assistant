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


NOTIFICATION_GUARD_API_KEY = os.getenv(
    "GROQ_NOTIFICATION_GUARD_API_KEY",
    "",
).strip()


NOTIFICATION_GUARD_MODEL = os.getenv(
    "GROQ_NOTIFICATION_GUARD_MODEL",
    "openai/gpt-oss-120b",
).strip()


NOTIFICATION_GUARD_MAX_RETRIES = int(
    os.getenv(
        "GROQ_NOTIFICATION_GUARD_MAX_RETRIES",
        "2",
    )
)
