from telegram import Bot

from app.config import BOT_TOKEN


# Shared across notifier.py and channel_notifier.py -- both send to
# the same bot account, so there's no reason to hold two separate
# Bot/HTTP-client instances for the same token.
bot = Bot(BOT_TOKEN)
