import asyncio

from app.config import NOTIFICATION_RETRY_INTERVAL
from app.freehub_worker import freehub_worker
from app.handlers.telegram import start
from app.job_processor import notification_retry_loop
from app.logger import initialize_database, logger
from app.state import state
from app.user_bot import create_user_bot_application, register_configured_channel, user_notification_worker


async def run():
    initialize_database()
    state.load()

    user_bot = create_user_bot_application()
    await user_bot.initialize()

    # This app is started manually (not via run_polling()), so PTB does not
    # invoke post_init for us. Register/verify the configured subscriber
    # channel explicitly after initialization.
    await register_configured_channel(user_bot)

    # Recover any user notifications that were mid-send when Loki stopped.
    await logger.run(logger.reset_sending_user_notifications)
    await user_bot.start()
    if user_bot.updater is None:
        raise RuntimeError("Telegram user bot updater is unavailable")
    await user_bot.updater.start_polling()

    try:
        await asyncio.gather(
            start(),
            freehub_worker(),
            notification_retry_loop(NOTIFICATION_RETRY_INTERVAL),
            user_notification_worker(),
        )
    finally:
        await user_bot.updater.stop()
        await user_bot.stop()
        await user_bot.shutdown()


def main():
    asyncio.run(run())
