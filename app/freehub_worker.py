import asyncio

from app.config import FREEHUB_POLL_INTERVAL
from app.freehub import mark_project_seen, poll_once
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

                try:

                    job = {
                        "title": project["title"],
                        "description": project["description"],
                        # Fallback raw text for logging/debugging.
                        "raw_text": (
                            f"{project['title']}\n\n"
                            f"{project['description']}"
                        ),
                        # Show the real marketplace instead of "FreeHub"
                        "source": project.get("platform", "FreeHub"),
                        "budget": project.get("price", ""),
                        "url": project.get("project_link", ""),
                    }

                    await process_job(
                        job=job,
                        job_id=project["uid"],
                        # job["source"] (the "platform" field, used
                        # for display) is live API response data and
                        # not guaranteed stable for the same project
                        # across polls. app.freehub tags every
                        # returned project with "_poll_source", the
                        # fixed kafiil/freelancer value its own
                        # seen-cache dedup already keys on -- use that
                        # for job identity instead, so the two dedup
                        # mechanisms (FreeHub's seen-cache and
                        # job_processor's job_uuid) can't disagree
                        # about whether this is the same project.
                        identity_source=project.get(
                            "_poll_source", job["source"]
                        ),
                    )

                    # Only mark the project as seen after the shared
                    # processing pipeline completes without raising.
                    await mark_project_seen(project)

                except Exception as e:

                    await logger.run(
                        logger.log_error,
                        "FreeHub Project",
                        e,
                        project.get("uid", ""),
                    )

        except Exception as e:

            await logger.run(
                logger.log_error,
                "FreeHub Worker",
                e,
            )

        await asyncio.sleep(FREEHUB_POLL_INTERVAL)