from telethon import TelegramClient, events

from app.config import (
    API_HASH,
    API_ID,
    SESSION_NAME,
    TARGET_CHANNELS,
)
from app.logger import logger
from app.message_processor import process_message
from app.state import state


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

    print("Recovering missed messages...\n")

    for channel in TARGET_CHANNELS:

        last_id = state.get_last_message_id(channel)

        messages = []

        async for message in client.iter_messages(
            channel,
            limit=40,
        ):
            messages.append(message)

        messages.reverse()  # oldest -> newest

        new_messages = 0

        for message in messages:

            if message.id <= last_id:
                continue

            try:

                await process_message(message)

                state.set_last_message_id(
                    message.chat_id,
                    message.id,
                )

                new_messages += 1

            except Exception as e:

                logger.log_error(
                    "StartupRecovery",
                    e,
                )

                print(
                    f"[RECOVERY ERROR] "
                    f"Message {message.id}: {e}"
                )

        print(
            f"[RECOVERY] {channel}: "
            f"{new_messages} new message(s)"
        )

    print("Recovery complete.")
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

            state.set_last_message_id(
                event.chat_id,
                event.id,
            )

        except Exception as e:

            logger.log_error(
                "MessageHandler",
                e,
            )

            print(
                f"[ERROR] Failed to process "
                f"message {event.id}: {e}"
            )

    await client.run_until_disconnected()
