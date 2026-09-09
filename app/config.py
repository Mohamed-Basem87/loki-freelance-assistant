"""Compatibility configuration surface backed by runtime_config.

Credential validation is lazy: selecting an adapter is what requires that
adapter's credentials. This lets source-only/test processes import the app
without unrelated Telegram/LLM secrets while preserving the existing names
when the environment is configured.
"""
import os
from app.runtime_config import BASE_DIR, RUNTIME, RECOVERY, source_profile, LLM_PROVIDERS, SOURCES
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

def _require_env(name):
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}\nPlease set it in your .env file.")
    return value.strip()
def _require_int_env(name):
    try: return int(_require_env(name))
    except ValueError as e: raise RuntimeError(f"Environment variable '{name}' must be an integer.") from e
def _optional_int_env(name):
    value=os.getenv(name)
    if not value or not value.strip(): return None
    try: return int(value)
    except ValueError as e: raise RuntimeError(f"Environment variable '{name}' must be an integer.") from e
def _require_channel_ids(name):
    try: return {int(x.strip()) for x in _require_env(name).split(",") if x.strip()}
    except ValueError as e: raise RuntimeError(f"Environment variable '{name}' must contain comma-separated integer IDs.") from e

def get_api_id(): return _require_int_env("API_ID")
def get_api_hash(): return _require_env("API_HASH")
def get_phone_number(): return _require_env("PHONE_NUMBER")
def get_gemini_api_keys():
    values=[x.strip() for x in _require_env("GEMINI_API_KEYS").split(",") if x.strip()]
    if not values: raise RuntimeError("GEMINI_API_KEYS is required")
    return values
def get_groq_api_key(): return _require_env("GROQ_API_KEY")
def get_bot_token(): return _require_env("BOT_TOKEN")
def get_bot_chat_id(): return _require_int_env("BOT_CHAT_ID")
def get_bot_channel_id(): return _optional_int_env("BOT_CHANNEL_ID")
def get_target_channels(): return _require_channel_ids("TARGET_CHANNEL_IDS")
def get_freehub_user_id(): return _require_env("FREEHUB_USER_ID")

# Backward-compatible values when configured; absent values stay None until an
# adapter is selected and asks for them.
API_ID = int(os.getenv("API_ID")) if os.getenv("API_ID", "").strip().isdigit() else None
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
GEMINI_API_KEYS = [x.strip() for x in os.getenv("GEMINI_API_KEYS", "").split(",") if x.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_CHAT_ID = int(os.getenv("BOT_CHAT_ID")) if os.getenv("BOT_CHAT_ID", "").strip().lstrip("-").isdigit() else None
BOT_CHANNEL_ID = _optional_int_env("BOT_CHANNEL_ID")
BOT_CHANNEL_CATEGORY_ID = os.getenv("BOT_CHANNEL_CATEGORY_ID", "data_analysis").strip()
TARGET_CHANNELS = _require_channel_ids("TARGET_CHANNEL_IDS") if os.getenv("TARGET_CHANNEL_IDS") else set()
FREEHUB_USER_ID = os.getenv("FREEHUB_USER_ID")
FREEHUB_BASE_URL = RUNTIME.freehub_base_url
FREEHUB_POLL_INTERVAL = RUNTIME.freehub_poll_interval
FREEHUB_PAGE_SIZE = RUNTIME.freehub_page_size
SESSION_NAME = str(BASE_DIR / "sessions" / "telegram")
NOTIFICATION_RETRY_INTERVAL = RUNTIME.notification_retry_interval

def source_display_name(source):
    profile=source_profile(source)
    return profile.display_name if profile else source
