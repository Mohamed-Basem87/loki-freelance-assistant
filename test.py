from telethon.sync import TelegramClient

API_ID = 30585116
API_HASH = "1870dbf0d0e5da1e9cc8799d31db5c8"

with TelegramClient("test_session", API_ID, API_HASH) as client:
    me = client.get_me()
    print(f"Logged in as: {me.first_name}")
