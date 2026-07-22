from app.job_processor import process_job
from app.logger import logger
from app.parser import parse_job


async def process_message(event):

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

        logger.log_error(
            "Message Processor",
            e,
        )

    finally:

        try:
            logger.save()

        except Exception as e:
            logger.log_error(
                "Logger",
                e,
            )
