import asyncio

from app.config import NOTIFICATION_RETRY_INTERVAL
from app.freehub_worker import freehub_worker
from app.handlers.telegram import start
from app.job_processor import notification_retry_loop
from app.logger import initialize_database
from app.state import state


async def run():

    initialize_database()

    state.load()

    await asyncio.gather(
        start(),
        freehub_worker(),
        notification_retry_loop(NOTIFICATION_RETRY_INTERVAL),
    )


def main():
    asyncio.run(run())
