import asyncio
from collections import defaultdict

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


# Safety cap for the recovery backfill. Without a bound, a channel
# that's been offline for a very long time (or a corrupted/reset
# state.json -- see app.state) could try to walk the channel's
# *entire* history. This is still ~50x the previous hardcoded window
# and is explicitly a fallback bound, not the expected case.
MAX_RECOVERY_MESSAGES = 2000


async def _recover_channel(client, channel):
    """
    Process every message newer than the last one we recorded for
    this channel, not just the most recent 40. The previous
    `limit=40` meant that if a channel received more than 40 messages
    while the bot was offline, everything older than that window was
    silently skipped forever (state still jumped forward to the
    newest of the 40 fetched, so those messages could never be
    recovered on a later run either).

    A channel with no recorded state yet (last_id == 0, i.e. this is
    the first time we've ever tracked it) is seeded from the current
    newest message instead of walking its full history -- we have no
    natural stopping point for "how far back is a missed message"
    the very first time we see a channel.
    """

    last_id = state.get_last_message_id(channel)

    if last_id == 0:

        newest = await client.get_messages(channel, limit=1)

        if newest:
            await state.async_set_last_message_id(channel, newest[0].id)

        print(f"[RECOVERY] {channel}: first run, seeded (no backfill)")
        return

    messages = []

    async for message in client.iter_messages(
        channel,
        min_id=last_id,
        reverse=True,  # oldest -> newest
        limit=MAX_RECOVERY_MESSAGES,
    ):
        messages.append(message)

    new_messages = 0

    for message in messages:

        try:

            processed = await process_message(message)

            if not processed:
                # Recovery is oldest -> newest. Never advance past a
                # failed message or a later message could make the
                # failed one permanently unrecoverable.
                print(
                    f"[RECOVERY ERROR] "
                    f"Message {message.id} failed; "
                    f"stopping recovery at this watermark."
                )
                break

            await state.async_set_last_message_id(
                message.chat_id,
                message.id,
            )

            new_messages += 1

        except Exception as e:

            await logger.run(
                logger.log_error,
                "StartupRecovery",
                e,
            )

            print(
                f"[RECOVERY ERROR] "
                f"Message {message.id}: {e}"
            )

            # Leave the watermark before the failed message and retry
            # it on the next recovery run.
            break

    if len(messages) >= MAX_RECOVERY_MESSAGES:
        print(
            f"[RECOVERY WARNING] {channel}: hit the "
            f"{MAX_RECOVERY_MESSAGES}-message safety cap -- there may "
            f"still be unrecovered messages older than what was just "
            f"processed. Re-run recovery (restart the bot) to continue."
        )

    print(
        f"[RECOVERY] {channel}: "
        f"{new_messages} new message(s)"
    )


async def _handle_live_message(event, channel_locks):
    """
    Process one live NewMessage event, serialized per-channel via
    `channel_locks`.

    H-1 fix: Telethon dispatches each NewMessage as its own task, so
    without serialization a fast message (e.g. an instant
    hard_reject) can finish -- and advance the watermark -- before a
    slower earlier message (e.g. one awaiting a Gemini call) does.
    That permanently drops the earlier message from the recovery
    window if it later fails or the process crashes before it
    finishes. Locking per channel forces messages from the same
    channel to be processed, and their watermarks advanced, strictly
    in arrival order -- matching the guarantee `_recover_channel`
    already provides on startup. Different channels still run fully
    concurrently; only same-channel messages are serialized.
    """

    async with channel_locks[event.chat_id]:

        try:

            processed = await process_message(event)

            if processed:
                await state.async_set_last_message_id(
                    event.chat_id,
                    event.id,
                )
            else:
                print(
                    f"[ERROR] Message {event.id} failed; "
                    f"watermark not advanced."
                )

        except Exception as e:

            await logger.run(
                logger.log_error,
                "MessageHandler",
                e,
            )

            print(
                f"[ERROR] Failed to process "
                f"message {event.id}: {e}"
            )


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
        await _recover_channel(client, channel)

    print("Recovery complete.")
    print("Listening for new jobs...\n")

    # See _handle_live_message for why this needs to be per-channel
    # locked (H-1).
    channel_locks = defaultdict(asyncio.Lock)

    @client.on(events.NewMessage(chats=list(TARGET_CHANNELS)))
    async def handler(event):

        chat = await event.get_chat()

        print(
            f"[TARGET] {chat.title} | "
            f"Message ID: {event.id}"
        )

        await _handle_live_message(event, channel_locks)

    await client.run_until_disconnected()