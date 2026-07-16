import asyncio
import time
import uuid

from app.channel_notifier import send_channel_notification
from app.filters import keyword_filter
from app.gemini import evaluate_job
from app.logger import logger
from app.notifier import send_notification
from app.parser import parse_job


async def process_message(event):

    try:

        start = time.perf_counter()

        job_uuid = str(uuid.uuid4())

        text = event.raw_text or ""
        source = event.chat.title if event.chat else ""

        job = parse_job(source, text)

        # If the parser didn't find a URL in the message text,
        # try extracting one from Telegram inline buttons.
        if not job["url"] and event.message.buttons:
            for row in event.message.buttons:
                for button in row:

                    # Some Telethon versions expose the URL directly.
                    url = getattr(button, "url", None)

                    # Most versions wrap it inside MessageButton.button.
                    if not url:
                        inner_button = getattr(button, "button", None)
                        url = getattr(inner_button, "url", None)

                    if url:
                        job["url"] = url
                        break

                if job["url"]:
                    break

        filter_text = f"{job['title']}\n{job['description']}"

        result = keyword_filter(filter_text)

        filter_time = round((time.perf_counter() - start) * 1000, 2)

        logger.create_job(
            job_uuid=job_uuid,
            job_id=str(event.id),
            source=job["source"],
            title=job["title"],
            company="",
            url=job["url"],
            score=result.get("score"),
            categories=result.get("categories", []),
            positive_matches=[
                m["keyword"] for m in result.get("positive_matches", [])
            ],
            negative_matches=[
                m["keyword"] for m in result.get("soft_negative_matches", [])
            ],
            hard_reject=result["hard_reject"],
            notify_directly=result["notify_directly"],
            needs_gemini=result["needs_gemini"],
            decision_reason="Initial Filter",
            filter_time_ms=filter_time,
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
            decision_reason = "High Keyword Score"
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

                logger.log_error(
                    "Gemini",
                    e,
                )

                final_decision = "Rejected"
                decision_reason = "Gemini Error"

            else:

                gemini_time = round(
                    (time.perf_counter() - gemini_start) * 1000,
                    2,
                )

                final_decision = gemini["decision"].capitalize()
                decision_reason = gemini["reason"]
                should_notify = gemini["decision"] == "accept"

                logger.update_job(
                    job_uuid,
                    gemini_decision=gemini["decision"],
                )

                logger.log_gemini(
                    job_uuid=job_uuid,
                    score_before=result["score"],
                    prompt_tokens="",
                    completion_tokens="",
                    response_time_ms=gemini_time,
                    decision=gemini["decision"],
                    confidence=gemini["confidence"],
                )

        logger.update_job(
            job_uuid,
            final_decision=final_decision,
            decision_reason=decision_reason,
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
                score=result["score"],
                categories=result["categories"],
                ai_used=result["needs_gemini"],
            )

            await send_notification(**notification_kwargs)
            await send_channel_notification(**notification_kwargs)

    finally:
        try:
            logger.save()
        except Exception as e:
            logger.log_error("Logger", e)
