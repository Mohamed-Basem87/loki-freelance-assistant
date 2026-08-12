from app.job_processor import process_job
from app.logger import logger
from app.parser import parse_job


async def process_message(event):

    failed = False

    try:

        text = event.raw_text or ""
        source = event.chat.title if event.chat else ""

        # Telethon exposes chat_id on both a live NewMessage event and
        # a plain Message object returned by iter_messages() (used
        # during startup recovery -- see app.handlers.telegram), so
        # this works identically on both call paths.
        chat_id = getattr(event, "chat_id", None)

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
            # job["source"] (the channel *title*) stays as display/
            # logging metadata and, in app.parser.parse_job, as the
            # actual dispatch signal for Nafezly-specific parsing --
            # it must not be repurposed as identity. The channel's
            # numeric chat_id is stable across renames, so use that
            # for job_uuid derivation instead; job_id (the message id)
            # is unaffected. Falls back to the title only in the
            # unlikely case chat_id is unavailable, matching the
            # previous behavior exactly in that edge case.
            identity_source=str(chat_id) if chat_id is not None else source,
        )

    except Exception as e:

        failed = True

        await logger.run(
            logger.log_error,
            "Message Processor",
            e,
        )

    finally:

        # process_job() already saves once, on the single logger
        # thread, on its own happy/handled-error path. This is only a
        # safety net for the case where something raised *before*
        # process_job got a chance to (e.g. parse_job itself failing)
        # -- so any log_error() write above (and any deferred,
        # save=False writes already sitting in memory) still reaches
        # disk. Skipped on the normal path to avoid an extra
        # full-workbook write per message. Routed through
        # logger.run() (the same single dedicated thread every other
        # workbook access uses -- see app.logger.ExcelLogger.run)
        # rather than a bare asyncio.to_thread(), so this save can
        # never overlap with another job's workbook mutation or save.
        if failed:
            try:
                await logger.run(logger.save)
            except Exception as e:
                await logger.run(
                    logger.log_error,
                    "Logger",
                    e,
                )