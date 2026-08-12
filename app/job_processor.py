import asyncio
import time
import uuid

from app.channel_notifier import send_channel_notification
from app.filters import keyword_filter
from app.llm.manager import evaluate_job
from app.logger import logger
from app.notifier import send_notification


# Deterministic namespace for deriving job_uuid from (source, job_id).
_JOB_UUID_NAMESPACE = uuid.UUID("6f6e6465-7370-4a6f-6273-7570706f7274")


def _make_job_uuid(source: str, job_id: str) -> str:
    return str(
        uuid.uuid5(_JOB_UUID_NAMESPACE, f"{source}:{job_id}")
    )


def _notification_payload_from_row(job_uuid: str, row: dict) -> dict:
    categories = row.get("Categories") or ""
    if isinstance(categories, str):
        categories = [item.strip() for item in categories.split(",") if item.strip()]

    return {
        "job_uuid": job_uuid,
        "title": row.get("Title") or "",
        "description": row.get("Description") or "",
        "source": row.get("Source") or "",
        "decision": row.get("Final Decision") or "Accepted",
        "reason": row.get("Decision Reason") or "",
        "url": row.get("URL") or "",
        # Budget was not historically stored in the Jobs sheet. A
        # retry after restart therefore omits it rather than inventing
        # a value; the notification remains otherwise identical.
        "budget": "",
        "categories": categories,
        "core_hit_count": row.get("Core Positive Hit Count") or 0,
        "supporting_weight": row.get("Supporting Positive Weight") or 0,
        "ai_used": bool(row.get("Needs Gemini")),
    }


def _merge_notification_status(
    current: str,
    platform: str,
    status: str,
) -> str:
    """Replace one platform's durable notification status in-place."""
    entries = {}
    for part in (current or "").split(";"):
        part = part.strip()
        if ": " in part:
            key, value = part.split(": ", 1)
            entries[key] = value

    entries[platform] = status

    order = ("Telegram", "Telegram Channel")
    return "; ".join(
        f"{key}: {entries[key]}"
        for key in order
        if key in entries
    )


async def _record_notification_result(
    job_uuid: str,
    current_status: str,
    platform: str,
    sent: bool,
) -> str:
    status = _merge_notification_status(
        current_status,
        platform,
        "Sent" if sent else "Failed",
    )

    await logger.run(
        logger.update_job,
        job_uuid,
        notification_status=status,
        save=True,
    )

    return status


async def _resume_pending_notifications(job_uuid: str, row: dict):
    """
    Resume a notification workflow that was durably recorded as
    pending or partially complete before an unclean shutdown.

    Notification delivery itself is an external side effect and
    cannot be made transactionally atomic with the workbook. We
    therefore persist the pending state before delivery and persist
    each successful channel immediately afterwards. This turns the
    large end-of-job crash window into a small per-send window and,
    after a restart, avoids re-sending channels whose successful
    delivery was already durably recorded.
    """
    status = row.get("Notification Status") or ""
    if status == "Complete":
        return

    payload = _notification_payload_from_row(job_uuid, row)

    private_sent = "Telegram: Sent" in status
    channel_sent = "Telegram Channel: Sent" in status

    if not private_sent:
        sent_direct = await send_notification(**payload)

        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram",
            "Sent" if sent_direct else "Failed",
            save=False,
        )
        status = await _record_notification_result(
            job_uuid,
            status,
            "Telegram",
            sent_direct,
        )
        private_sent = sent_direct

    if not channel_sent:
        sent_channel = await send_channel_notification(**payload)

        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram Channel",
            "Sent" if sent_channel else "Failed",
            save=False,
        )
        status = await _record_notification_result(
            job_uuid,
            status,
            "Telegram Channel",
            sent_channel,
        )
        channel_sent = sent_channel

    if private_sent and channel_sent:
        await logger.run(
            logger.update_job,
            job_uuid,
            notification_status="Complete",
            save=True,
        )


async def process_job(job: dict, job_id: str, identity_source: str = None):
    """
    Process one job through the classifier/LLM/notification pipeline.

    `identity_source`, together with `job_id`, is what job_uuid is
    derived from. It must remain stable for the same underlying
    message/project across restarts and metadata edits.

    Deduplication uses an atomic check-and-create operation on the
    single Excel logger worker. This prevents concurrent process_job()
    calls for the same identity from both creating rows and notifying.

    Notification state is persisted before the first send and after
    each successful/failed send. If the process restarts after a
    notification workflow has begun, the durable row is used to resume
    only the incomplete portion instead of treating the job as a new
    job.
    """

    start = time.perf_counter()

    if identity_source is None:
        identity_source = job.get("source", "")

    job_uuid = _make_job_uuid(identity_source, job_id)

    legacy_identity_source = job.get("source", "")
    legacy_job_uuid = (
        _make_job_uuid(legacy_identity_source, job_id)
        if legacy_identity_source != identity_source
        else None
    )

    # Fast path for already-known canonical jobs. If a notification
    # workflow was durably left pending/partially complete, resume it
    # instead of silently treating the durable row as fully complete.
    existing = await logger.run(logger.get_job, job_uuid)
    if existing is not None:
        if existing.get("Notification Status"):
            await _resume_pending_notifications(job_uuid, existing)
        return

    # Historical rows may still use the pre-stable-identity UUID.
    # They remain lookup-only compatibility records.
    if legacy_job_uuid is not None and await logger.run(
        logger.has_job, legacy_job_uuid
    ):
        print(
            f"[DEDUP] Recognized job {job_id!r} via legacy identity "
            "(pre-stable-identity UUID) -- skipping reprocessing."
        )
        return

    filter_text = f"{job['title']}\n{job['description']}"
    result = keyword_filter(filter_text, title=job["title"])

    filter_time = round(
        (time.perf_counter() - start) * 1000,
        2,
    )

    created = await logger.run(
        logger.create_job_if_absent,
        legacy_job_uuid=legacy_job_uuid,
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

    if not created:
        # Another concurrent invocation won the atomic create race.
        # Re-read the durable row so a pending notification workflow
        # can be resumed if necessary.
        existing = await logger.run(logger.get_job, job_uuid)
        if existing is not None and existing.get("Notification Status"):
            await _resume_pending_notifications(job_uuid, existing)
        return

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
        decision_reason = result["reason"] or "Below Gemini Threshold"

    await logger.run(
        logger.update_job,
        job_uuid,
        final_decision=final_decision,
        decision_reason=decision_reason,
        save=False,
    )

    if should_notify:
        # Persist the fact that this job requires notification BEFORE
        # creating the external side effect. If Loki crashes before
        # the first send, the next recovery can resume the workflow.
        await logger.run(
            logger.update_job,
            job_uuid,
            notification_status="Pending",
            save=True,
        )

        await _resume_pending_notifications(
            job_uuid,
            {
                "Title": job["title"],
                "Description": job["description"],
                "Source": job["source"],
                "Final Decision": final_decision,
                "Decision Reason": decision_reason,
                "URL": job["url"],
                "Categories": ", ".join(result["categories"]),
                "Core Positive Hit Count": result["core_positive_hit_count"],
                "Supporting Positive Weight": result[
                    "supporting_positive_weight"
                ],
                "Needs Gemini": result["needs_gemini"],
                "Notification Status": "Pending",
            },
        )

        return

    # Non-notifying jobs have no external side effect that needs a
    # durable recovery state, so one final save is sufficient.
    await logger.run(logger.save)
