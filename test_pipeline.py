import asyncio

from app.logger import logger
from app.message_processor import process_message


logger.initialize()


class FakeButton:
    url = None


class FakeMessage:
    buttons = []


class FakeChat:
    title = "Pipeline Test"


class FakeEvent:
    id = 888888
    chat = FakeChat()
    message = FakeMessage()

    raw_text = """
Need someone to create SQL queries for a reporting system.
"""


async def main():
    jobs_before = logger.workbook["Jobs"].max_row

    await process_message(FakeEvent())

    logger.save()

    jobs_after = logger.workbook["Jobs"].max_row

    assert jobs_after == jobs_before + 1, (
        "Expected exactly one new job to be logged."
    )

    last_row = jobs_after

    sheet = logger.workbook["Jobs"]

    assert sheet.cell(row=last_row, column=2).value is not None, \
        "Job UUID was not written."

    assert sheet.cell(row=last_row, column=5).value, \
        "Job title is empty."

    assert sheet.cell(row=last_row, column=8).value is not None, \
        "Score was not calculated."

    print("✅ Pipeline smoke test passed.")


try:
    asyncio.run(main())
finally:
    logger.close()
