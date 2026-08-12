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


async def process_job(job: dict, job_id: str, identity_source: str = None):
    """
    `identity_source`, together with `job_id`, is what job_uuid is
    derived from -- it must be a value that stays constant for "the
    same underlying message/project" across restarts, retries, and
    metadata edits, which `job["source"]` is not guaranteed to be
    (see app.message_processor and app.freehub_worker: it can be a
    Telegram channel *title*, or a FreeHub project's *live* "platform"
    field, either of which can change independently of the
    message/project actually being the same one). Callers that have a
    genuinely stable identity value pass it explicitly; if omitted,
    this falls back to `job.get("source", "")` for backward
    compatibility with any caller that doesn't have a better option.

    Legacy-identity dedup compatibility: before identity_source
    existed, job_uuid was always derived from job["source"] directly
    (the mutable/display value) -- so every job logged prior to this
    change has a job_uuid computed that way, not via the new stable
    identity_source. job["source"] is still passed by every caller
    (it's still needed for display/logging -- see message_processor.py
    and freehub_worker.py), so it doubles as exactly the legacy
    identity value without callers needing to pass anything new. See
    the legacy-lookup block below for how this is used -- it is a
    dedup *lookup* only: new jobs are always logged under the new
    canonical job_uuid, never under the legacy one.
    """

    start = time.perf_counter()

    if identity_source is None:
        identity_source = job.get("source", "")

    job_uuid = _make_job_uuid(identity_source, job_id)

    legacy_identity_source = job.get("source", "")
    # Only worth a second lookup when the legacy identity would
    # actually produce a *different* UUID than the canonical one --
    # i.e. only when a caller passed a genuinely different
    # identity_source (Telegram's chat_id, FreeHub's _poll_source).
    # When they're equal (identity_source wasn't overridden, or a
    # FreeHub project's "platform" field happens to already match its
    # _poll_source), the legacy and canonical UUIDs are identical and
    # checking twice would be redundant.
    legacy_job_uuid = (
        _make_job_uuid(legacy_identity_source, job_id)
        if legacy_identity_source != identity_source
        else None
    )

    if await logger.run(logger.has_job, job_uuid):
        # Already logged under the current, canonical identity --
        # this is a reprocessing of the same source message/project,
        # not a new job. Skip it rather than creating a duplicate row
        # and sending a duplicate notification.
        return

    if legacy_job_uuid is not None and await logger.run(
        logger.has_job, legacy_job_uuid
    ):
        # Found under the *pre-stable-identity* UUID scheme instead --
        # this is a historical job that was logged before job_uuid
        # derivation switched from job["source"] (title / live
        # "platform" field) to a stable identity_source. Recognize it
        # as the same job rather than reprocessing/re-notifying it
        # under a brand new UUID. The existing row is left exactly as
        # it is -- this is a lookup-only compatibility check, never a
        # rewrite of stored data, and every newly-logged job still
        # only ever uses the canonical job_uuid above.
        print(
            f"[DEDUP] Recognized job {job_id!r} via legacy identity "
            f"(pre-stable-identity UUID) -- skipping reprocessing."
        )
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

        # This branch is reached whenever the classifier rejected the
        # job (hard_reject is False, notify_directly is False, and
        # needs_gemini is False) but `matched` was still True -- e.g.
        # a core-negative in the body alongside some supporting-
        # positive hits, or supporting-positive evidence that fell
        # below SUPPORTING_POSITIVE_MIN_FOR_GEMINI. `matched` only
        # asks "was there any positive evidence at all", not "did
        # positive evidence win the decision", so it does not imply
        # the job was actually a borderline Gemini case. The
        # classifier already computed the real reason (e.g.
        # "core_negative_no_core_positive", "insufficient_signal",
        # "title_core_negative_no_body_positive") -- preserve it
        # instead of collapsing every such reject into the same
        # generic, misleading label, since the Jobs sheet's Decision
        # Reason column is what the classifier's own tuning workflow
        # (see app/keywords.py comments) relies on to diagnose reject
        # rows. This does not change `decision`/`should_notify`/
        # whether Gemini is called -- only the logged reason string.
        decision_reason = result["reason"] or "Below Gemini Threshold"

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