import asyncio
import pathlib
import tempfile
from datetime import datetime

from telegram import (
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)

from app.logger import logger
from app.user_bot import my_chat_member_update, stop_command


def _isolated_db():
    db = pathlib.Path(tempfile.mkdtemp()) / "user_bot.db"
    original = logger.path
    logger.close()
    logger.path = db
    logger.initialize()
    return original


def _restore_db(original):
    logger.close()
    logger.path = original


def _is_active(telegram_user_id) -> str:
    cursor = logger._conn.execute(
        'SELECT "Is Active" FROM users WHERE "Telegram User ID" = ?',
        (str(telegram_user_id),),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _my_chat_member_update(telegram_user_id, new_member_factory, chat_type="private"):
    """
    Build a real python-telegram-bot Update carrying a my_chat_member
    change, matching exactly what create_user_bot_application()'s
    ChatMemberHandler(..., ChatMemberHandler.MY_CHAT_MEMBER) actually
    delivers -- not a loose mock, so this exercises the real attribute
    path (update.effective_chat, update.my_chat_member.new_chat_
    member.status) my_chat_member_update() relies on.
    """
    user = User(id=telegram_user_id, first_name="Tester", is_bot=False)
    chat = Chat(id=telegram_user_id, type=chat_type)
    bot_user = User(id=999, first_name="Loki", is_bot=True)

    old_member = ChatMemberMember(user=bot_user)
    new_member = new_member_factory(bot_user)

    changed = ChatMemberUpdated(
        chat=chat,
        from_user=user,
        date=datetime.now(),
        old_chat_member=old_member,
        new_chat_member=new_member,
    )

    return Update(update_id=1, my_chat_member=changed)


def test_stopping_the_bot_deactivates_the_user_immediately():
    """
    Regression test for the gap where "Is Active" only ever got
    corrected reactively, on the next failed send attempt -- a user
    who stops the bot and then never happens to match another
    notification stayed "Is Active"="1" indefinitely. Telegram pushes
    the my_chat_member update the moment the user stops/blocks the
    bot, before the bot ever attempts another send.
    """
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555111, "tester", "Tester")
            assert _is_active(555111) == "1"

            update = _my_chat_member_update(
                555111,
                lambda bot_user: ChatMemberBanned(user=bot_user, until_date=datetime.now()),
            )
            await my_chat_member_update(update, context=None)

            assert _is_active(555111) == "0"

        asyncio.run(run())
    finally:
        _restore_db(original)


def test_unblocking_does_not_reactivate_the_user_without_start():
    """
    A user who unblocks the bot becomes reachable again, but Option A
    keeps the subscription inactive until the user explicitly sends /start.
    """
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555222, "tester", "Tester")
            await logger.run(logger.set_destination_active, 555222, False)
            assert _is_active(555222) == "0"

            update = _my_chat_member_update(
                555222,
                lambda bot_user: ChatMemberMember(user=bot_user),
            )
            await my_chat_member_update(update, context=None)

            assert _is_active(555222) == "0"

        asyncio.run(run())
    finally:
        _restore_db(original)


def test_left_status_also_deactivates_the_user():
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555333, "tester", "Tester")

            update = _my_chat_member_update(
                555333,
                lambda bot_user: ChatMemberLeft(user=bot_user),
            )
            await my_chat_member_update(update, context=None)

            assert _is_active(555333) == "0"

        asyncio.run(run())
    finally:
        _restore_db(original)


def test_channel_status_changes_never_touch_the_user_table_via_this_handler():
    """
    my_chat_member fires for ANY chat the bot is a member of, including
    the configured public channel destination -- a distinct,
    admin-membership concept unrelated to a subscriber unsubscribing.
    The handler must ignore non-private chats entirely rather than
    risk deactivating the channel destination on some unrelated
    membership status change there.
    """
    original = _isolated_db()
    try:
        async def run():
            await logger.run(
                logger.ensure_channel_destination, 777444, "Test Channel"
            )
            assert _is_active(777444) == "1"

            update = _my_chat_member_update(
                777444,
                lambda bot_user: ChatMemberLeft(user=bot_user),
                chat_type="channel",
            )
            await my_chat_member_update(update, context=None)

            # Unchanged: the handler returned early on chat.type != "private".
            assert _is_active(777444) == "1"

        asyncio.run(run())
    finally:
        _restore_db(original)


def test_unknown_telegram_id_is_a_harmless_no_op():
    """
    A my_chat_member update for a chat id that was never ensure_user'd
    (e.g. someone who stopped the bot before ever completing /start)
    must not raise -- set_destination_active's UPDATE simply matches
    zero rows.
    """
    original = _isolated_db()
    try:
        async def run():
            update = _my_chat_member_update(
                999888,
                lambda bot_user: ChatMemberBanned(user=bot_user, until_date=datetime.now()),
            )
            await my_chat_member_update(update, context=None)
            assert _is_active(999888) is None

        asyncio.run(run())
    finally:
        _restore_db(original)


def _stop_command_update(telegram_user_id):
    """Build a real message-bearing Update for /stop.

    stop_command() calls update.effective_user, update.effective_chat and
    update.message.reply_text. The Bot/Message classes auto-build a Bot when
    given a token via api_kwargs={"bot": ...}? Not so -- pass bot via the
    positional args and set_bot(). reply_text needs a bound bot to send the
    confirmation through.
    """
    user = User(id=telegram_user_id, first_name="Tester", is_bot=False)
    chat = Chat(id=telegram_user_id, type="private")
    message = Message(
        1,
        datetime.now(),
        chat=chat,
        from_user=user,
        text="/stop",
    )
    return Update(update_id=2, message=message)


def test_stop_command_deactivates_the_user():
    """/stop is the in-band opt-out: Telegram never sends a my_chat_member
    update for a device-level Stop, so the user must be able to unsubscribe
    explicitly. It must set Is Active=0 and remain reachable to /start."""
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555444, "tester", "Tester")
            assert _is_active(555444) == "1"

            update = _stop_command_update(555444)
            await stop_command(update, context=None)

            assert _is_active(555444) == "0"

        asyncio.run(run())
    finally:
        _restore_db(original)


def _subscription_events(telegram_user_id):
    cursor = logger._conn.execute(
        'SELECT "Telegram User ID", "First Name", "Username", '
        '"Event Type", "Occurred At", "Trigger" '
        'FROM subscription_events WHERE "Telegram User ID" = ? '
        'ORDER BY rowid',
        (str(telegram_user_id),),
    )
    return cursor.fetchall()


def test_subscription_events_record_only_real_state_transitions():
    """Repeated /start or /stop must not create duplicate analytics events."""
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555555, "basem", "Basem")

            changed = await logger.run(
                logger.record_subscription_event,
                555555, "Basem", "basem", True, "start"
            )
            assert changed is False

            changed = await logger.run(
                logger.record_subscription_event,
                555555, "Basem", "basem", False, "stop"
            )
            assert changed is True

            changed = await logger.run(
                logger.record_subscription_event,
                555555, "Basem", "basem", False, "stop"
            )
            assert changed is False

            changed = await logger.run(
                logger.record_subscription_event,
                555555, "Basem", "basem", True, "start"
            )
            assert changed is True

            changed = await logger.run(
                logger.record_subscription_event,
                555555, "Basem", "basem", True, "start"
            )
            assert changed is False

            events = _subscription_events(555555)
            assert len(events) == 2
            assert events[0][0:4] == ("555555", "Basem", "basem", "unsubscribed")
            assert events[0][5] == "stop"
            assert events[1][0:4] == ("555555", "Basem", "basem", "subscribed")
            assert events[1][5] == "start"
            assert events[0][4]
            assert events[1][4]
            assert _is_active(555555) == "1"

        asyncio.run(run())
    finally:
        _restore_db(original)


def test_ensure_user_preserves_unsubscribed_state():
    """Profile updates must never silently undo an explicit /stop."""
    original = _isolated_db()
    try:
        async def run():
            await logger.run(logger.ensure_user, 555666, "old", "Old")
            await logger.run(
                logger.record_subscription_event,
                555666, "Old", "old", False, "stop"
            )
            assert _is_active(555666) == "0"

            await logger.run(logger.ensure_user, 555666, "new", "New")
            assert _is_active(555666) == "0"
            assert _subscription_events(555666)[0][3] == "unsubscribed"

        asyncio.run(run())
    finally:
        _restore_db(original)
