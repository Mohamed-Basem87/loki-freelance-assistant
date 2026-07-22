import asyncio

from app.freehub_worker import freehub_worker
from app.handlers.telegram import start
from app.logger import initialize_workbook
from app.state import state


async def run():

    initialize_workbook()

    state.load()

    await asyncio.gather(
        start(),
        freehub_worker(),
    )


def main():
    asyncio.run(run())
