import asyncio
from app.composition import compose
from app.workers import default_registry
from app.startup import default_startup_steps

async def run():
    runtime = compose()
    try:
        for step in default_startup_steps(runtime):
            await step()
        registry = default_registry(runtime)
        await registry.run()
    finally:
        await runtime.shutdown()

def main(): asyncio.run(run())
