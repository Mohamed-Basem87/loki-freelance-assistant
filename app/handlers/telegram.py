from telethon import TelegramClient, events

from app.config import (
    API_HASH,
    API_ID,
    SESSION_NAME,
    TARGET_CHANNELS,
)
from app.logger import logger
from app.message_processor import process_message


async def start():
    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )

    await client.start()

    me = await client.get_me()

    print("=" * 70)
    print(f"Logged in as: {me.first_name}")
    print("=" * 70)
    print("Listening for new jobs...\n")

    @client.on(events.NewMessage(chats=list(TARGET_CHANNELS)))
    async def handler(event):
        chat = await event.get_chat()

        print(
            f"[TARGET] {chat.title} | "
            f"Message ID: {event.id}"
        )

        try:
            await process_message(event)

        except Exception as e:
            logger.log_error(
                "MessageHandler",
                e,
            )

            print(
                f"[ERROR] Failed to process message "
                f"{event.id}: {e}"
            )

    await client.run_until_disconnected()
