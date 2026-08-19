import asyncio
import pathlib
import tempfile

from app.logger import logger
from app.routing import queue_for_category


def test_user_routing_is_single_category_and_idempotent():
    db = pathlib.Path(tempfile.mkdtemp()) / "routing.db"
    original = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        async def run():
            user_id = await logger.run(
                logger.ensure_user, 123456, "tester", "Tester"
            )
            await logger.run(
                logger.set_user_category, user_id, "data_analysis"
            )

            first = await queue_for_category("job-1", "data_analysis")
            second = await queue_for_category("job-1", "data_analysis")
            rows = await logger.run(logger.claim_pending_user_notifications)

            assert first == 1
            assert second == 0
            assert len(rows) == 1
            assert rows[0]["Job UUID"] == "job-1"
            assert rows[0]["Category ID"] == "data_analysis"
            assert rows[0]["Telegram User ID"] == "123456"

        asyncio.run(run())
    finally:
        logger.close()
        logger.path = original
