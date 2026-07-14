from telethon import TelegramClient, events

from app.config import API_HASH, API_ID, SESSION_NAME
from app.message_processor import process_message

TARGET_CHANNELS = {
    -1001335768304,  # Nafezly
    -1002142292720,  # Mostaql Programming
    -1001994689105,  # Mostaql Data Entry
}


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
        await process_message(event)

    await client.run_until_disconnected()
