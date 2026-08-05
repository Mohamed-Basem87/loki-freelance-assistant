import asyncio
import time
import uuid

from app.channel_notifier import send_channel_notification
from app.filters import keyword_filter
from app.llm.manager import evaluate_job
from app.logger import logger
from app.notifier import send_notification


# Deterministic namespace for deriving job_uuid from (source, job_id).
# Using uuid5 instead of uuid4 means the same underlying message/
# project always maps to the same job_uuid, so a job that gets
# reprocessed (e.g. after a state.json reset or a FreeHub cache
# reset on restart) is recognized as a duplicate instead of silently
# creating a second row every time. See logger.has_job().
_JOB_UUID_NAMESPACE = uuid.UUID("6f6e6465-7370-4a6f-6273-7570706f7274")


def _make_job_uuid(source: str, job_id: str) -> str:
    return str(
        uuid.uuid5(_JOB_UUID_NAMESPACE, f"{source}:{job_id}")
    )


async def process_job(job: dict, job_id: str):

    start = time.perf_counter()

    job_uuid = _make_job_uuid(job.get("source", ""), job_id)

    if await logger.run(logger.has_job, job_uuid):
        # Already logged (and, if should_notify fired, already
        # notified) -- this is a reprocessing of the same source
        # message/project, not a new job. Skip it rather than
        # creating a duplicate row and sending a duplicate
        # notification.
        return

    filter_text = f"{job['title']}\n{job['description']}"

    result = keyword_filter(filter_text, title=job["title"])

    filter_time = round(
        (time.perf_counter() - start) * 1000,
        2,
    )

    await logger.run(
        logger.create_job,
        job_uuid=job_uuid,
        job_id=job_id,
        source=job["source"],
        title=job["title"],
        description=job["description"],
        raw_message=job["raw_text"],
        filter_text=filter_text,
        company=job.get("company", ""),
        url=job["url"],
        filter_result=result,
        filter_time_ms=filter_time,
        save=False,
    )

    final_decision = "Rejected"
    decision_reason = ""
    should_notify = False

    if result["hard_reject"]:

        decision_reason = "Hard Reject"

    elif not result["matched"]:

        decision_reason = "No Matching Keywords"

    elif result["notify_directly"]:

        final_decision = "Accepted"
        decision_reason = result["reason"]
        should_notify = True

    elif result["needs_gemini"]:

        gemini_start = time.perf_counter()

        try:

            gemini = await asyncio.to_thread(
                evaluate_job,
                filter_text,
                result,
            )

        except Exception as e:

            # manager.evaluate_job() only raises once BOTH Gemini and
            # Groq have failed (see app.llm.manager) -- so this is a
            # total-LLM-fallback exhaustion, not specifically a
            # Gemini problem. Logging it as "Gemini Error" (as before)
            # mislabeled Groq-side failures too. "LLM" / "LLM Error"
            # reflects what actually happened; the full detail from
            # both providers is still in `e` (see manager.py) and
            # lands in the Errors sheet either way.
            await logger.run(
                logger.log_error,
                "LLM",
                e,
                job_uuid,
                save=False,
            )

            final_decision = "Rejected"
            decision_reason = "LLM Error"

        else:

            gemini_time = round(
                (time.perf_counter() - gemini_start) * 1000,
                2,
            )

            final_decision = (
                "Accepted"
                if gemini["decision"] == "accept"
                else "Rejected"
            )
            decision_reason = gemini["reason"]
            should_notify = gemini["decision"] == "accept"

            await logger.run(
                logger.update_job,
                job_uuid,
                gemini_decision=gemini["decision"],
                save=False,
            )

            await logger.run(
                logger.log_gemini,
                job_uuid=job_uuid,
                decision_before=result["decision"],
                reason_before=result["reason"],
                prompt_tokens="",
                completion_tokens="",
                response_time_ms=gemini_time,
                decision=gemini["decision"],
                confidence=gemini["confidence"],
                save=False,
            )

    else:

        decision_reason = "Below Gemini Threshold"

    await logger.run(
        logger.update_job,
        job_uuid,
        final_decision=final_decision,
        decision_reason=decision_reason,
        save=False,
    )

    if should_notify:

        notification_kwargs = dict(
            job_uuid=job_uuid,
            title=job["title"],
            description=job["description"],
            source=job["source"],
            decision=final_decision,
            reason=decision_reason,
            url=job["url"],
            budget=job["budget"],
            categories=result["categories"],
            core_hit_count=result["core_positive_hit_count"],
            supporting_weight=result["supporting_positive_weight"],
            ai_used=result["needs_gemini"],
        )

        sent_direct = await send_notification(**notification_kwargs)
        sent_channel = await send_channel_notification(**notification_kwargs)

        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram",
            "Sent" if sent_direct else "Failed",
            save=False,
        )
        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram Channel",
            "Sent" if sent_channel else "Failed",
            save=False,
        )

    # One full-workbook save per job instead of one per intermediate
    # step (create_job / update_job x2 / log_gemini / log_notification
    # x2 previously each saved independently -- up to ~7 full
    # serializations for a single job). Runs on the same single
    # logger thread as every other workbook access this job made
    # above (see ExcelLogger.run) -- not a generic asyncio.to_thread
    # worker -- so it can never overlap with another job's workbook
    # mutation or save.
    await logger.run(logger.save)