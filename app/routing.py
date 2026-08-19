"""User routing for final, single-category jobs.

A user may subscribe to many categories, but each job carries exactly
one final category. Routing therefore creates at most one pending
notification per (job, user) pair.
"""

from app.logger import logger


async def queue_for_category(job_uuid, category_id):
    if not category_id:
        return 0
    return await logger.run(
        logger.queue_user_notifications,
        job_uuid,
        category_id,
        save=True,
    )
