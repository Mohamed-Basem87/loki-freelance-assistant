import asyncio
from app.wiring.composition import compose
from app.wiring.workers import default_registry
from app.wiring.startup import default_startup_steps

async def run():
    runtime = compose()
    steps = await default_startup_steps(runtime)
    try:
        for step in steps:
            await step()
        registry = default_registry(runtime)
        await registry.run()
    finally:
        await runtime.shutdown()

def main(): asyncio.run(run())
