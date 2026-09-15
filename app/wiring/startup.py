"""Authoritative startup sequence owned by the composition root."""


def default_startup_steps(runtime):
    async def initialize():
        await runtime.initialize_database()
        runtime.state.load()

    return [initialize]
