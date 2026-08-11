import os


_ENABLED_VALUES = {"1", "true", "yes", "on"}


NOTIFICATION_GUARD_ENABLED = (
    os.getenv("NOTIFICATION_GUARD_ENABLED", "false").strip().lower()
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
    os.getenv("GROQ_NOTIFICATION_GUARD_MAX_RETRIES", "2")
)
