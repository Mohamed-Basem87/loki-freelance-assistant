import asyncio
import re
import time
import uuid
import weakref

from app.channel_notifier import send_channel_notification
from app.filters import keyword_filter
from app.llm.manager import evaluate_job
from app.logger import logger
from app.notifier import send_notification
from app.state import state


# Deterministic namespace for deriving job_uuid from (source, job_id).
_JOB_UUID_NAMESPACE = uuid.UUID("6f6e6465-7370-4a6f-6273-7570706f7274")


# Notification delivery is an external side effect, so SQLite cannot
# transactionally lock it together with the audit row.  There are,
# however, two notification entry points in this process: normal
# process_job() execution and the periodic retry sweep.  A retry sweep
# can therefore race a live notification unless both paths share a
# per-job asyncio lock.  Weak values keep this registry from growing
# forever as new jobs are processed.
_NOTIFICATION_LOCKS = weakref.WeakValueDictionary()


def _get_notification_lock(job_uuid: str) -> asyncio.Lock:
    lock = _NOTIFICATION_LOCKS.get(job_uuid)
    if lock is None:
        lock = asyncio.Lock()
        _NOTIFICATION_LOCKS[job_uuid] = lock
    return lock


def _make_job_uuid(source: str, job_id: str) -> str:
    return str(
        uuid.uuid5(_JOB_UUID_NAMESPACE, f"{source}:{job_id}")
    )


# Cross-source dedup is persisted and claimed atomically by
# app.state.StateManager.  The state executor serializes the
# check-and-claim operation, so FreeHub and Telegram cannot both win
# for the same project when they arrive concurrently.


def _extract_project_id(url: str):
    """Extract a numeric project ID from a freelance platform URL."""
    if not url:
        return None

    match = re.search(r"/project/(\d+)(?:[/?#\-\s]|$)", url)
    return match.group(1) if match else None


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
        "ai_used": str(row.get("Needs Gemini") or "").strip().lower()
        in ("1", "true", "yes", "y"),
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
    suppressed: bool = False,
) -> str:
    leg_status = "Sent" if sent else ("Suppressed" if suppressed else "Failed")

    status = _merge_notification_status(
        current_status,
        platform,
        leg_status,
    )

    await logger.run(
        logger.update_job,
        job_uuid,
        notification_status=status,
        save=True,
    )

    return status


async def _was_suppressed_by_guard(job_uuid: str) -> bool:
    """
    True only when the most recent Notification Guard evaluation for
    this job was a genuine content-based rejection ("do_not_notify"),
    as opposed to a provider error or the guard never having run.

    This is what stops the periodic retry sweep (P1-1) from
    compounding with the guard's fail-closed behavior (P1-2) into
    endlessly re-asking Groq about a job it already genuinely
    rejected, while still letting a transient provider outage keep
    being retried like any other failed send.
    """
    decision = await logger.run(logger.get_latest_guard_decision, job_uuid)
    return decision == "do_not_notify"


async def _resume_pending_notifications_unlocked(job_uuid: str, row: dict):
    """
    Resume a notification workflow that was durably recorded as
    pending or partially complete before an unclean shutdown, or that
    the periodic retry sweep (retry_incomplete_notifications) picked
    up after a prior send attempt failed.

    Notification delivery itself is an external side effect and
    cannot be made transactionally atomic with the audit log. We
    therefore persist the pending state before delivery and persist
    each successful (or permanently suppressed) channel immediately
    afterwards. This turns the large end-of-job crash window into a
    small per-send window and, after a restart or a retry sweep pass,
    avoids re-sending channels whose outcome was already durably
    recorded.
    """
    status = row.get("Notification Status") or ""
    if status in ("Complete", "Suppressed"):
        return

    payload = _notification_payload_from_row(job_uuid, row)

    private_resolved = "Telegram: Sent" in status or "Telegram: Suppressed" in status
    channel_resolved = (
        "Telegram Channel: Sent" in status or "Telegram Channel: Suppressed" in status
    )

    private_suppressed = "Telegram: Suppressed" in status
    channel_suppressed = "Telegram Channel: Suppressed" in status

    if not private_resolved:
        sent_direct = await send_notification(**payload)
        suppressed = False if sent_direct else await _was_suppressed_by_guard(job_uuid)

        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram",
            "Sent" if sent_direct else ("Suppressed" if suppressed else "Failed"),
            save=False,
        )
        status = await _record_notification_result(
            job_uuid,
            status,
            "Telegram",
            sent_direct,
            suppressed=suppressed,
        )
        private_resolved = sent_direct or suppressed
        private_suppressed = suppressed and not sent_direct

    if not channel_resolved:
        sent_channel = await send_channel_notification(**payload)
        suppressed = (
            False if sent_channel else await _was_suppressed_by_guard(job_uuid)
        )

        await logger.run(
            logger.log_notification,
            job_uuid,
            "Telegram Channel",
            "Sent" if sent_channel else ("Suppressed" if suppressed else "Failed"),
            save=False,
        )
        status = await _record_notification_result(
            job_uuid,
            status,
            "Telegram Channel",
            sent_channel,
            suppressed=suppressed,
        )
        channel_resolved = sent_channel or suppressed
        channel_suppressed = suppressed and not sent_channel

    if private_resolved and channel_resolved:
        final_status = (
            "Suppressed" if (private_suppressed or channel_suppressed) else "Complete"
        )
        await logger.run(
            logger.update_job,
            job_uuid,
            notification_status=final_status,
            save=True,
        )


async def _resume_pending_notifications(job_uuid: str, row: dict):
    """Resume one job's notification workflow under its per-job lock.

    The row returned by the retry sweep is only a snapshot.  Once the
    lock is acquired, re-read the durable row so a waiter that lost a
    race with a live process_job() sees the latest notification state
    and does not send an already-resolved notification a second time.
    """
    lock = _get_notification_lock(job_uuid)
    async with lock:
        latest = await logger.run(logger.get_job, job_uuid)
        if latest is None:
            return

        current_status = latest.get("Notification Status") or ""
        if current_status in ("Complete", "Suppressed"):
            return

        await _resume_pending_notifications_unlocked(job_uuid, latest)


async def retry_incomplete_notifications():
    """
    Sweep every job whose notification workflow is durably recorded
    as started but not "Complete"/"Suppressed", and resume it.

    Previously nothing in the codebase ever revisited a "Failed"
    notification row -- process_job() only resumes a pending workflow
    when the *same* job is reprocessed (a restart, or the same
    message/project arriving again), which never happens for a job
    that was already durably created. A Telegram rate limit, a
    blocked chat ID, or a transient Notification Guard provider
    outage therefore silently and permanently dropped a job the
    system had already decided to notify about (audit P1-1 / P1-2).
    Call this periodically (see app.bot.run) instead.
    """
    rows = await logger.run(logger.get_incomplete_notification_jobs)

    for row in rows:
        job_uuid = row.get("Job UUID")
        if not job_uuid:
            continue

        try:
            await _resume_pending_notifications(job_uuid, row)
        except Exception as e:
            await logger.run(
                logger.log_error,
                "Notification Retry Sweep",
                e,
                job_uuid,
                save=False,
            )

    return len(rows)


async def notification_retry_loop(interval_seconds: int):
    """Background task: retry incomplete notifications on a fixed
    interval for the lifetime of the process. See
    retry_incomplete_notifications()."""
    while True:
        try:
            retried = await retry_incomplete_notifications()
            if retried:
                print(f"[NOTIFY RETRY] resumed {retried} incomplete job(s)")
        except Exception as e:
            await logger.run(
                logger.log_error,
                "Notification Retry Loop",
                e,
                save=False,
            )

        await asyncio.sleep(interval_seconds)


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

    project_id = _extract_project_id(job.get("url", ""))
    job_uuid = _make_job_uuid(identity_source, job_id)

    legacy_identity_source = job.get("source", "")
    legacy_job_uuid = (
        _make_job_uuid(legacy_identity_source, job_id)
        if legacy_identity_source != identity_source
        else None
    )

    # Fast path for already-known canonical jobs.
    #
    # A row with a recorded notification status has already completed
    # classification, so only the unfinished notification workflow
    # needs to be resumed. A row with a terminal Final Decision but no
    # notification status is a completed non-notifying job (Rejected).
    #
    # The important recovery case is a row with NO Final Decision.
    # That means the process may have crashed after the durable row was
    # created but before classification finished. Treat that row as an
    # incomplete classification and continue through the pipeline
    # instead of returning and letting the ingestion watermark/seen
    # cache permanently discard it.
    existing = await logger.run(logger.get_job, job_uuid)
    existing_incomplete = False

    if existing is not None:
        notification_status = existing.get("Notification Status") or ""
        final_decision = existing.get("Final Decision") or ""

        if notification_status:
            await _resume_pending_notifications(job_uuid, existing)
            return

        if final_decision == "Accepted":
            # A crash can also occur in the tiny window after the final
            # decision is saved but before notification is marked
            # Pending. Accepted jobs must not be mistaken for completed
            # work in that state.
            await logger.run(
                logger.update_job,
                job_uuid,
                notification_status="Pending",
                save=True,
            )
            existing["Notification Status"] = "Pending"
            await _resume_pending_notifications(job_uuid, existing)
            return

        if final_decision:
            return

        existing_incomplete = True

    # Historical rows may still use the pre-stable-identity UUID.
    # They remain lookup-only compatibility records. Only perform this
    # compatibility lookup when there is no canonical row; an existing
    # incomplete canonical row must be resumed.
    if (
        existing is None
        and legacy_job_uuid is not None
        and await logger.run(
            logger.has_job, legacy_job_uuid
        )
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

    if not existing_incomplete:
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
            # can be resumed if necessary, or leave a still-incomplete
            # classification for that invocation to finish.
            existing = await logger.run(logger.get_job, job_uuid)
            if existing is not None:
                if existing.get("Notification Status"):
                    await _resume_pending_notifications(job_uuid, existing)
                elif existing.get("Final Decision"):
                    return
            return

    # Claim the canonical project ID after the durable job row has
    # been created. The claim itself is atomic inside StateManager's
    # single-worker executor, so concurrent FreeHub/Telegram calls
    # cannot both win.
    #
    # The state claim is idempotent for the same job_uuid, so an
    # incomplete row can safely re-enter this step after a crash:
    # - if this job already owns the claim, it remains valid;
    # - if another source claimed the project while this job was down,
    #   this job loses the claim and is rejected as a duplicate.
    if project_id:
        claimed = await state.async_claim_cross_source_project(
            project_id,
            job_uuid,
        )

        if not claimed:
            await logger.run(
                logger.update_job,
                job_uuid,
                final_decision="Rejected",
                decision_reason="Duplicate project from another source",
                save=True,
            )
            print(
                f"[DEDUP] Project {project_id} was already claimed by "
                "another source -- skipping duplicate."
            )
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
