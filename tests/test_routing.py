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


def test_user_source_preference_filters_subscriber_delivery():
    db = pathlib.Path(tempfile.mkdtemp()) / "routing-source.db"
    original = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        async def scenario():
            user_id = await logger.run(
                logger.ensure_user, 123458, "tester3", "Tester 3"
            )
            await logger.run(logger.set_user_category, user_id, "data_analysis")
            await logger.run(logger.set_user_source, user_id, "nafezly")

            nafezly = await queue_for_category(
                "job-source-1", "data_analysis", "نفذلي"
            )
            mostaql = await queue_for_category(
                "job-source-2", "data_analysis", "مستقل | برمجة"
            )
            rows = await logger.run(logger.claim_pending_user_notifications)

            return nafezly, mostaql, rows

        nafezly, mostaql, rows = asyncio.run(scenario())
    finally:
        logger.close()
        logger.path = original

    assert nafezly == 1
    assert mostaql == 0
    assert len(rows) == 1
    assert rows[0]["Job UUID"] == "job-source-1"


def test_existing_user_with_no_source_preference_receives_all_sources():
    db = pathlib.Path(tempfile.mkdtemp()) / "routing-all.db"
    original = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        async def scenario():
            user_id = await logger.run(
                logger.ensure_user, 123459, "tester4", "Tester 4"
            )
            await logger.run(logger.set_user_category, user_id, "data_analysis")

            first = await queue_for_category(
                "job-all-1", "data_analysis", "نفذلي"
            )
            second = await queue_for_category(
                "job-all-2", "data_analysis", "مستقل"
            )
            rows = await logger.run(logger.claim_pending_user_notifications)

            return first, second, rows

        first, second, rows = asyncio.run(scenario())
    finally:
        logger.close()
        logger.path = original

    assert first == 1
    assert second == 1
    assert {row["Job UUID"] for row in rows} == {"job-all-1", "job-all-2"}


def test_category_preferences_are_stored_on_user_and_source_filters_still_apply():
    db = pathlib.Path(tempfile.mkdtemp()) / "routing-merged.db"
    original = logger.path
    logger.close()
    try:
        logger.path = db
        logger.initialize()

        async def scenario():
            user_id = await logger.run(
                logger.ensure_user, 123460, "tester5", "Tester 5"
            )
            await logger.run(
                logger.set_user_category, user_id, "data_analysis", True
            )
            await logger.run(
                logger.set_user_source, user_id, "nafezly", True
            )

            categories = await logger.run(logger.get_user_categories, user_id)
            sources = await logger.run(logger.get_user_sources, user_id)

            first = await queue_for_category(
                "job-merged-1", "data_analysis", "نفذلي"
            )
            second = await queue_for_category(
                "job-merged-2", "data_analysis", "مستقل"
            )
            return categories, sources, first, second

        categories, sources, first, second = asyncio.run(scenario())
    finally:
        logger.close()
        logger.path = original

    assert categories == ["data_analysis"]
    assert sources == ["nafezly"]
    assert first == 1
    assert second == 0
