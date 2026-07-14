import asyncio

from app.logger import logger
from app.message_processor import process_message

logger.initialize()


class FakeChat:
    title = "Pipeline Test"


class FakeEvent:
    id = 888888
    chat = FakeChat()

    raw_text = """
Need someone to create SQL queries for a reporting system.
"""


asyncio.run(process_message(FakeEvent()))

logger.close()
