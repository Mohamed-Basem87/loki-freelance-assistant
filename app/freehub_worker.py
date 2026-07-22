import asyncio

from app.config import FREEHUB_POLL_INTERVAL
from app.freehub import poll_once
from app.job_processor import process_job
from app.logger import logger


async def freehub_worker():
    """
    Poll FreeHub periodically and send newly discovered
    projects through the shared processing pipeline.
    """

    while True:

        try:

            projects = await poll_once()

            if projects:
                print(f"[FREEHUB] {len(projects)} new project(s)")

            for project in projects:

                job = {
                    "title": project["title"],
                    "description": project["description"],
                    # Show the real marketplace instead of "FreeHub"
                    "source": project.get("platform", "FreeHub"),
                    "budget": project.get("price", ""),
                    "url": project.get("project_link", ""),
                }

                await process_job(
                    job=job,
                    job_id=project["uid"],
                )

        except Exception as e:

            logger.log_error(
                "FreeHub Worker",
                e,
            )

        await asyncio.sleep(FREEHUB_POLL_INTERVAL)
