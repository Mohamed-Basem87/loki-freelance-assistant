from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_CHAT_ID = int(os.getenv("BOT_CHAT_ID"))

SESSION_NAME = str(BASE_DIR / "sessions" / "telegram")
