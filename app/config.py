from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _require_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Missing required environment variable: {name}\n"
            f"Please set it in your .env file."
        )

    return value


def _require_int_env(name: str) -> int:
    value = _require_env(name)

    try:
        return int(value)
    except ValueError as e:
        raise RuntimeError(
            f"Environment variable '{name}' must be an integer "
            f"(got '{value}')."
        ) from e


API_ID = _require_int_env("API_ID")
API_HASH = _require_env("API_HASH")
PHONE_NUMBER = _require_env("PHONE_NUMBER")

GEMINI_API_KEY = _require_env("GEMINI_API_KEY")

BOT_TOKEN = _require_env("BOT_TOKEN")
BOT_CHAT_ID = _require_int_env("BOT_CHAT_ID")

SESSION_NAME = str(BASE_DIR / "sessions" / "telegram")
