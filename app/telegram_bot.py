from telegram import Bot

from app.config import BOT_TOKEN


# Shared bot instance used by the private notifier and subscriber worker.
# Public channels are subscriber destinations, so there is no separate
# direct channel notifier.
bot = Bot(BOT_TOKEN)
