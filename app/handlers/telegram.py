import asyncio
from collections import defaultdict

from telethon import TelegramClient, events

from app.config import (
    API_HASH,
    API_ID,
    PHONE_NUMBER,
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


async def _warm_entity_cache(client):
    """
    Fresh sessions (e.g. a container's first boot) carry no cached
    entities, so Telethon cannot resolve a numeric channel ID on its
    own -- it needs the channel's access_hash, which is only obtained
    from a previous fetch. messages.GetDialogs returns every chat the
    account is in; that caches each monitored channel's hash in memory
    and persists it to the session file, so recovery's
    client.get_messages() can resolve. Locally this was always a no-op
    because the long-lived session file already had the channels
    cached.
    """

    await client.get_dialogs()

    missing = []

    for channel in TARGET_CHANNELS:
        try:
            await client.get_entity(channel)
        except ValueError as e:
            missing.append((channel, str(e)))

    if missing:
        for channel, error in missing:
            print(f"[ERROR] Cannot resolve monitored channel {channel}: {error}")

        print(
            "Is the Loki account a member of every channel in "
            "TARGET_CHANNEL_IDS? A private channel can only be resolved "
            "by numeric ID when the account is in it."
        )

        raise RuntimeError(
            "Unresolvable target channel(s): "
            f"{', '.join(str(channel) for channel, _ in missing)}"
        )


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
        return True

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
                return False

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
            # it on the next recovery run. Returning False also tells
            # the startup coordinator to keep this channel's recovery
            # barrier active so live messages cannot advance its
            # watermark past the failed message.
            return False

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

    return True


async def _handle_live_message(event, channel_locks, recovery_blocked=None):
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
                # If startup recovery for this channel stopped on a
                # failed message, live messages are still captured and
                # processed, but they must not advance the watermark
                # past that failed recovery point. Otherwise the first
                # live message after the failure could make the failed
                # message permanently unrecoverable on the next restart.
                blocked = (
                    recovery_blocked is not None
                    and recovery_blocked.get(event.chat_id, False)
                )

                if not blocked:
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


async def _recover_all_channels(client, channel_locks, recovery_blocked):
    """
    Recover every configured Telegram channel, one at a time, while
    guaranteeing that a live NewMessage event can never be processed
    ahead of that channel's own recovery.

    Startup race fix: previously the live NewMessage handler was only
    registered *after* every channel finished recovering, so a message
    arriving in that window -- after a channel's recovery snapshot was
    taken but before the handler existed -- was silently dropped by
    both mechanisms. The fix has two parts, split across this function
    and `start()`:

    1. `start()` acquires every channel's lock (see `channel_locks`,
       shared with `_handle_live_message`) up front, then registers
       the live handler, and only then calls this function. Because
       none of that involves an actual suspension point (an
       uncontended `asyncio.Lock.acquire()` never yields control back
       to the event loop), the handler is guaranteed to be registered
       -- and every lock guaranteed to already be held -- before
       Telethon can dispatch a single live update. A live event can
       therefore never be lost: `_handle_live_message` always finds a
       registered handler waiting for it.

    2. This function then recovers each channel in turn and keeps that
       channel's lock held for the entire recovery attempt. Once the
       attempt finishes -- successfully or by stopping at a failed
       message -- the lock is released. A live event for a channel
       whose lock is still held
       (recovery not yet reached it, or still running) is captured by
       the handler immediately but blocks inside
       `_handle_live_message`'s `async with channel_locks[chat_id]`
       until this function releases it -- so it is processed strictly
       after that channel's recovery, never before and never
       concurrently with it. This preserves recovery's oldest -> newest
       watermark ordering: a live event can't advance a channel's
       watermark past a point recovery hasn't reached yet.

    The same underlying message can therefore be seen once by recovery
    (in its snapshot) and once by the live handler (queued behind the
    lock). That's harmless: the existing SQLite job_uuid dedup in
    app.job_processor.process_job makes the second observation a
    no-op.

    If recovery stops on a failed message, the lock is still released
    so live processing can continue, but `recovery_blocked[channel]`
    remains true. Live processing then deliberately does not advance
    that channel's watermark, preventing a newer live message from
    making the failed recovery message unrecoverable.
    """

    for channel in TARGET_CHANNELS:
        try:
            recovered = await _recover_channel(client, channel)

            # A failed recovery still releases the lock so live events
            # are not lost forever, but the shared barrier remains
            # active. Those live events can be processed for real-time
            # notifications, while their watermark is held behind the
            # failed recovery message until the next restart can retry
            # it.
            recovery_blocked[channel] = not recovered
        finally:
            channel_locks[channel].release()


async def start():
    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )

    # Explicitly log in as the phone-number userbot, never a bot token.
    # Passing the phone up front also removes the ambiguous interactive
    # prompt that previously let a bot token silently create a bot
    # session -- which cannot enumerate dialogs (get_dialogs) or resolve
    # the monitored channels by numeric ID (see _warm_entity_cache).
    await client.start(phone=PHONE_NUMBER)

    me = await client.get_me()

    print("=" * 70)
    print(f"Logged in as: {me.first_name}")
    print("=" * 70)

    # See _warm_entity_cache for why this must run before recovery.
    await _warm_entity_cache(client)

    # See _handle_live_message for why this needs to be per-channel
    # locked (H-1), and _recover_all_channels for why every lock is
    # acquired here -- synchronously, before the live handler below is
    # registered -- rather than inside the recovery loop itself.
    channel_locks = defaultdict(asyncio.Lock)
    recovery_blocked = {
        channel: True
        for channel in TARGET_CHANNELS
    }

    for channel in TARGET_CHANNELS:
        await channel_locks[channel].acquire()

    @client.on(events.NewMessage(chats=list(TARGET_CHANNELS)))
    async def handler(event):

        chat = await event.get_chat()

        print(
            f"[TARGET] {chat.title} | "
            f"Message ID: {event.id}"
        )

        await _handle_live_message(
            event,
            channel_locks,
            recovery_blocked,
        )

    print("Recovering missed messages...\n")

    await _recover_all_channels(client, channel_locks, recovery_blocked)

    print("Recovery complete.")
    print("Listening for new jobs...\n")

    await client.run_until_disconnected()