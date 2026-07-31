from app.job_processor import process_job
from app.logger import logger
from app.parser import parse_job

import asyncio


async def process_message(event):

    failed = False

    try:

        text = event.raw_text or ""
        source = event.chat.title if event.chat else ""

        job = parse_job(source, text)

        buttons = getattr(event, "buttons", None)

        if not job["url"] and buttons:

            for row in buttons:

                for button in row:

                    url = getattr(button, "url", None)

                    if not url:

                        inner_button = getattr(button, "button", None)
                        url = getattr(inner_button, "url", None)

                    if url:

                        job["url"] = url
                        break

                if job["url"]:
                    break

        await process_job(
            job=job,
            job_id=str(event.id),
        )

    except Exception as e:

        failed = True

        logger.log_error(
            "Message Processor",
            e,
        )

    finally:

        # process_job() already saves once, off the event loop, on
        # its own happy/handled-error path. This is only a safety
        # net for the case where something raised *before*
        # process_job got a chance to (e.g. parse_job itself failing)
        # -- so any log_error() write above (and any deferred,
        # save=False writes already sitting in memory) still reaches
        # disk. Skipped on the normal path to avoid an extra
        # full-workbook write per message, and moved off the event
        # loop since it's a blocking openpyxl write either way.
        if failed:
            try:
                await asyncio.to_thread(logger.save)
            except Exception as e:
                logger.log_error(
                    "Logger",
                    e,
                )
