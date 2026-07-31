import asyncio
import tempfile
from pathlib import Path

from app.logger import logger
from app.message_processor import process_message


# Use an isolated, temporary workbook instead of the real
# logs/freelance_bot_logs.xlsx -- the previous version of this test
# ran directly against the production log file, silently polluting
# real operational data with test rows every time it was run.
_tmp_dir = tempfile.mkdtemp(prefix="freelance_assistant_test_")
logger.path = Path(_tmp_dir) / "test_logs.xlsx"
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
        "Decision was not calculated."

    print("✅ Pipeline smoke test passed (job logged with a decision).")

    # Regression check for the job_uuid dedup fix (M3): reprocessing
    # the exact same source event a second time must NOT create a
    # second row. job_uuid is now derived deterministically from
    # (source, job_id) instead of a fresh uuid4() every call, so
    # logger.has_job() can actually recognize the repeat.
    await process_message(FakeEvent())

    jobs_after_repeat = logger.workbook["Jobs"].max_row

    assert jobs_after_repeat == jobs_after, (
        "Reprocessing the same message must not create a duplicate "
        "row (job_uuid dedup regression)."
    )

    print("✅ Duplicate-message dedup check passed.")


try:
    asyncio.run(main())
finally:
    logger.close()
