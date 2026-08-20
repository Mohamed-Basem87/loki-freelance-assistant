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


def test_user_subscribed_to_multiple_categories_gets_one_job_notification():
    db = pathlib.Path(tempfile.mkdtemp()) / "routing-multi.db"
    original = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        async def run():
            user_id = await logger.run(
                logger.ensure_user, 123457, "tester2", "Tester 2"
            )
            await logger.run(logger.set_user_category, user_id, "data_analysis")
            await logger.run(logger.ensure_category, "web_development", "Web Development", "Web")

            await logger.run(logger.set_user_category, user_id, "web_development")

            queued = await queue_for_category("job-web-1", "web_development")
            rows = await logger.run(logger.claim_pending_user_notifications)

            assert queued == 1
            assert len(rows) == 1
            assert rows[0]["Job UUID"] == "job-web-1"
            assert rows[0]["Category ID"] == "web_development"
            assert rows[0]["Telegram User ID"] == "123457"

        asyncio.run(run())
    finally:
        logger.close()
        logger.path = original
