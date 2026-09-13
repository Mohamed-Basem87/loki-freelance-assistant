"""Authoritative startup sequence owned by the composition root."""


def default_startup_steps(runtime):
    async def initialize():
        await runtime.initialize_database()
        runtime.state.load()
        await runtime.user_bot.initialize()

    async def register_channel():
        await runtime.register_channel()

    async def reset_notifications():
        await runtime.reset_inflight_notifications()

    return [initialize, register_channel, reset_notifications]
