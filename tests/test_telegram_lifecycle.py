"""Regression test for audit finding: Telegram bot lifecycle.

The Telegram Application's lifecycle in this codebase is driven
manually (TelegramCommandSurface.initialize() -> start() ->
updater.start_polling(); stop() -> updater.stop() ->
application.stop()/shutdown()), matched by the explicit step sequence
in app.startup.default_startup_steps(). python-telegram-bot's
`post_init` hook is only ever invoked by the library's own
run_polling()/run_webhook() convenience methods, neither of which this
codebase calls -- so a `post_init` registered on the Application
builder here would never run. It previously sat on the builder,
duplicating (and silently never performing) work -- resetting
in-flight user notifications and registering the configured channel --
that app.startup already does through the one authoritative manual
sequence.
"""
import asyncio

import pytest

from app.startup import default_startup_steps


def test_built_application_has_no_dead_post_init_hook(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-tests")
    from app.user_bot import create_user_bot_application

    application = create_user_bot_application()
    assert application.post_init is None, (
        "no post_init hook should be registered: this codebase drives "
        "the Application lifecycle manually (initialize()/start()), and "
        "a hook that only fires under run_polling()/run_webhook() would "
        "never execute -- it must not be registered at all"
    )


def test_authoritative_startup_sequence_registers_channel_and_resets_notifications():
    calls = []

    class _FakeState:
        def load(self):
            calls.append("state.load")

    class _FakeUserBot:
        async def initialize(self):
            calls.append("user_bot.initialize")

        async def register_channel(self):
            calls.append("register_channel")

    class _FakeRuntime:
        state = _FakeState()
        user_bot = _FakeUserBot()

        async def initialize_database(self):
            calls.append("initialize_database")

        async def register_channel(self):
            calls.append("register_channel")

        async def reset_inflight_notifications(self):
            calls.append("reset_inflight_notifications")

    async def scenario():
        for step in default_startup_steps(_FakeRuntime()):
            await step()

    asyncio.run(scenario())

    # Both steps that a dead post_init previously *claimed* to perform
    # must actually run exactly once, through the one authoritative
    # sequence, and channel registration must happen before
    # notifications are resumed (a stopped-then-restarted deployment
    # should have somewhere to deliver to before recovery flushes).
    assert calls.count("register_channel") == 1
    assert calls.count("reset_inflight_notifications") == 1
    assert calls.index("register_channel") < calls.index(
        "reset_inflight_notifications"
    )
