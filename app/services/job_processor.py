import asyncio
import re
import time
import uuid
import weakref

from app.domain.categories.registry import get_category, arbitration_only_categories
from app.domain.classification import classify_and_select
from app.llm.manager import arbitrate_category
from app.wiring.dependencies import logger, state, dedup, stream_publisher, resolver, guard_allow, url_shortener
from app.infra.heartbeat import STATE_RUNNING, sleep_with_beats
from app.infra.runtime_config import RUNTIME
from app.infra.timeouts import call_with_timeout


class ClassificationPendingError(RuntimeError):
    """Raised after durable LLM-pending state is recorded."""


# How long a single classification attempt is allowed to hold its claim
# on a durably-pending job before another caller is allowed to retry it.
# This is deliberately independent of RUNTIME.notification_retry_interval
# (the *backoff* applied after a real failure): the lease only needs to
# outlast one LLM arbitration call, not a full retry cycle. It exists so
# that a crash mid-attempt self-heals instead of leaving the row claimed
# forever, while still being long enough that the FreeHub re-discovery
# path and classification_retry_loop cannot both win the same claim.
CLASSIFICATION_CLAIM_LEASE_SECONDS = 120


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
# app.services.state.StateManager.  The state executor serializes the
# check-and-claim operation, so FreeHub and Telegram cannot both win
# for the same project when they arrive concurrently.


def _extract_project_id(url: str):
    """Extract a numeric project ID from a freelance platform URL."""
    if not url:
        return None

    match = re.search(r"/project/(\d+)(?:[/?#\-\s]|$)", url)
    return match.group(1) if match else None


async def _resolve_notification_category(job_uuid: str, row: dict, category_id: str) -> str:
    """
    Hook point for the optional Notification Guard to potentially
    reclassify a clean keyword-direct match into "full_stack" before
    either notification leg runs (see _resume_pending_notifications_
    unlocked below, which always calls this first).

    In the standard (non-guarded) runtime this is a no-op that returns
    the category unchanged. The composition root (app.wiring.composition.compose)
    binds this slot to the real guard-aware resolver
    (app.notification_guard.integration.NotificationGuardIntegration.
    resolve_category). The deterministic tiering system and arbitration
    path never call this and are unaffected either way.
    """
    return await resolver(job_uuid, row, category_id)


def _guard_payload_from_row(job_uuid: str, row: dict) -> dict:
    return {
        "job_uuid": job_uuid,
        "source": row.get("Source") or "",
        "title": row.get("Title") or "",
        "description": row.get("Description") or "",
        "decision": row.get("Final Decision") or "Accepted",
        "category_id": row.get("Category ID") or "",
    }


async def _publish_and_mark_complete(job_uuid: str, row: dict):
    """Single choke point through which an approved job actually leaves
    this service: confirm it in Postgres, only then hand it to Redis.

    The row is already durable in Postgres by the time this runs (every
    field on it was committed via create_job_if_absent/update_job
    earlier in process_job) -- this function's job is purely the
    publish-then-mark-complete ordering, used identically whether the
    job got here via guard approval or the LLM-arbitration bypass below.

    Publish happens BEFORE the "Complete" write. If publish raises,
    "Notification Status" stays at "Pending" (untouched), so the next
    get_incomplete_notification_jobs() sweep pass republishes it -- this
    is what makes a crash between the Postgres commit and the XADD
    self-heal instead of silently dropping the job from the downstream
    pipeline.
    """
    await stream_publisher.publish(row)
    await logger.update_job(job_uuid, notification_status="Complete", save=True)


async def _resume_pending_notifications_unlocked(job_uuid: str, row: dict):
    """Resolve whether this job should reach the downstream notification
    service and, if so, publish it.

    Fan-out (who gets notified) and delivery (actually sending) both
    moved to a separate service that consumes the Redis stream
    JobStreamPublisher publishes to -- this node's responsibility ends
    at a durable "Complete"/"Suppressed" write.
    """
    status = row.get("Notification Status") or ""
    if status in ("Complete", "Suppressed"):
        return

    # A job the LLM arbitrated already had its content directly
    # reviewed -- the guard exists to catch what the deterministic
    # keyword tiers missed, not to re-litigate a call the LLM already
    # made. Skip category resolution and the guard entirely for these:
    # straight to Postgres-confirmed + Redis-published, no guard lookup
    # or provider call in between. (This must key off
    # "Category Selection Method" == "llm", not "Needs Gemini" -- the
    # latter reflects one category's own deterministic tier verdict,
    # which can be False even when arbitration genuinely ran, e.g. two
    # clean "notify_directly" categories that still needed arbitration
    # to pick between them.)
    if (row.get("Category Selection Method") or "") == "llm":
        await _publish_and_mark_complete(job_uuid, row)
        return

    # Resolve the category to actually publish under *before* the guard
    # allow-check, so a durable reclassification (e.g. to "full_stack")
    # is reflected in both the guard decision and the published row. A
    # no-op in the standard runtime (see _resolve_notification_category).
    category_id = row.get("Category ID") or ""
    if category_id:
        resolved_category_id = await _resolve_notification_category(
            job_uuid, row, category_id
        )
        if resolved_category_id != category_id:
            row = dict(row)
            row["Category ID"] = resolved_category_id
            category_id = resolved_category_id

    payload = _guard_payload_from_row(job_uuid, row)
    allowed = await guard_allow(payload)

    if not allowed:
        await logger.update_job(job_uuid, notification_status="Suppressed", save=True)
        return

    await _publish_and_mark_complete(job_uuid, row)


async def _resume_pending_notifications(job_uuid: str, row: dict):
    """Resume one job's notification workflow under its per-job lock.

    The row returned by the retry sweep is only a snapshot.  Once the
    lock is acquired, re-read the durable row so a waiter that lost a
    race with a live process_job() sees the latest notification state
    and does not send an already-resolved notification a second time.
    """
    lock = _get_notification_lock(job_uuid)
    async with lock:
        latest = await logger.get_job(job_uuid)
        if latest is None:
            return

        current_status = latest.get("Notification Status") or ""
        if current_status in ("Complete", "Suppressed"):
            return

        await _resume_pending_notifications_unlocked(job_uuid, latest)


async def retry_incomplete_classifications():
    """Retry jobs durably left in the LLM-pending state."""
    rows = await logger.get_incomplete_classification_jobs()
    for row in rows:
        job_uuid = row.get("Job UUID")
        if not job_uuid:
            continue
        try:
            job = {
                "title": row.get("Title") or "",
                "description": row.get("Description") or "",
                "raw_text": row.get("Raw Message") or f"{row.get('Title') or ''}\n\n{row.get('Description') or ''}",
                "source": row.get("Source") or "",
                "url": row.get("URL") or "",
                "budget": "",
                "company": row.get("Company") or "",
            }
            await process_job(
                job=job,
                job_id=str(row.get("Job ID") or ""),
                identity_source=str(row.get("Identity Source") or row.get("Source") or ""),
            )
        except ClassificationPendingError:
            continue
        except Exception as exc:
            await logger.log_error("Classification Retry Sweep", exc, job_uuid, save=False)
    return len(rows)


async def classification_retry_loop(interval_seconds: int):
    """Continuously retry durable LLM-pending classifications.

    Reports per-worker liveness while idling (see heartbeat.sleep_with_beats)
    so the healthcheck can observe this worker as alive even when the
    retry interval exceeds the staleness window.
    """
    while True:
        try:
            retried = await retry_incomplete_classifications()
            if retried:
                print(f"[CLASSIFICATION RETRY] resumed {retried} pending job(s)")
        except Exception as exc:
            await logger.log_error("Classification Retry Loop", exc, save=False)
        await sleep_with_beats(interval_seconds, "classification_retry", STATE_RUNNING)


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
    rows = await logger.get_incomplete_notification_jobs()

    for row in rows:
        job_uuid = row.get("Job UUID")
        if not job_uuid:
            continue

        try:
            await _resume_pending_notifications(job_uuid, row)
        except Exception as e:
            await logger.log_error("Notification Retry Sweep", e, job_uuid, save=False)

    return len(rows)


async def notification_retry_loop(interval_seconds: int):
    """Background task: retry incomplete notifications on a fixed
    interval for the lifetime of the process. See
    retry_incomplete_notifications(). Reports per-worker liveness while
    idling so the healthcheck can observe this worker as alive even when
    the retry interval exceeds the staleness window."""
    while True:
        try:
            retried = await retry_incomplete_notifications()
            if retried:
                print(f"[NOTIFY RETRY] resumed {retried} incomplete job(s)")
        except Exception as e:
            await logger.log_error("Notification Retry Loop", e, save=False)

        await sleep_with_beats(interval_seconds, "notification_retry", STATE_RUNNING)


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
    existing = await logger.get_job(job_uuid)
    existing_incomplete = False

    if existing is not None:
        notification_status = existing.get("Notification Status") or ""
        final_decision = existing.get("Final Decision") or ""

        if notification_status:
            await _resume_pending_notifications(job_uuid, existing)
            return

        if final_decision == "Pending":
            # The previous arbitration attempt failed after the job row was
            # created. Keep it recoverable and rerun classification --
            # but only once its persisted backoff window has elapsed.
            #
            # Regression fix (audit finding P1-3): FreeHub keeps a
            # project in its own durable pending queue until it is
            # explicitly marked seen, which only happens after
            # process_job() returns successfully (see
            # app.wiring.source_worker.SourceWorker.run()). A project stuck
            # here as Pending/LLM Error therefore gets re-submitted to
            # process_job() on *every* FreeHub poll (every
            # freehub_poll_interval, e.g. 60s) -- entirely independent
            # of, and much more frequently than, the dedicated
            # classification_retry_loop sweep (every
            # notification_retry_interval, e.g. 300s). Before this
            # check, every one of those FreeHub-triggered
            # reprocessing attempts fell straight through to a fresh,
            # real LLM call, so a provider outage or quota exhaustion
            # multiplied into repeated real requests, repeated
            # failures, and repeated DB writes for the same job far
            # faster than the intended retry cadence. Skipping the
            # reattempt here (without touching anything else about the
            # row) means only the row's own persisted schedule -- set
            # once, at the point of failure below -- decides when the
            # next real attempt happens, regardless of which caller
            # (FreeHub rediscovery or the retry loop) re-invoked
            # process_job() for it.
            #
            # This same backoff check used to be a plain read of the
            # persisted "not before" timestamp, with no atomic claim
            # over the gap between that read and the real LLM call
            # further below. FreeHub rediscovery (every
            # freehub_poll_interval) and classification_retry_loop
            # (every notification_retry_interval) are two independent
            # callers of process_job() for the *same* job_uuid, so
            # once the backoff window elapsed both could observe it
            # eligible and both proceed to call the LLM provider for
            # the same pending job concurrently. claim_pending_
            # classification() closes that gap with a single
            # conditional UPDATE on the serialized DB writer: only the
            # caller whose UPDATE actually matches a still-eligible row
            # wins the claim, and every other concurrent caller's
            # rowcount is 0.
            lease_until = time.time() + CLASSIFICATION_CLAIM_LEASE_SECONDS
            claimed = await logger.claim_pending_classification(job_uuid, lease_until)
            if not claimed:
                raise ClassificationPendingError(
                    f"Classification for {job_uuid} is pending or already "
                    "claimed by another worker."
                )
            existing_incomplete = True
        elif final_decision == "Accepted":
            # A crash can also occur in the tiny window after the final
            # decision is saved but before notification is marked
            # Pending. Accepted jobs must not be mistaken for completed
            # work in that state.
            await logger.update_job(job_uuid, notification_status="Pending", save=True)
            existing["Notification Status"] = "Pending"
            await _resume_pending_notifications(job_uuid, existing)
            return

        if final_decision and final_decision != "Pending":
            return

        existing_incomplete = True

    # Historical rows may still use the pre-stable-identity UUID.
    # They remain lookup-only compatibility records. Only perform this
    # compatibility lookup when there is no canonical row; an existing
    # incomplete canonical row must be resumed.
    if (
        existing is None
        and legacy_job_uuid is not None
        and await logger.has_job(legacy_job_uuid)
    ):
        print(
            f"[DEDUP] Recognized job {job_id!r} via legacy identity "
            "(pre-stable-identity UUID) -- skipping reprocessing."
        )
        return

    filter_text = f"{job['title']}\n{job['description']}"
    # Deterministic profiling is CPU-heavy (thousands of regex passes over the
    # text) and would freeze the shared event loop -- and with it heartbeats
    # and the healthcheck -- for seconds to minutes on large descriptions. Run
    # it in the executor so the loop stays responsive; keyword_filter is
    # thread-safe by design (immutable cached profiles + a compile lock).
    classification = await asyncio.to_thread(
        classify_and_select,
        filter_text,
        title=job["title"],
    )

    category_results = classification["categories"]
    candidate_ids = [
        category_id
        for category_id, item in category_results.items()
        if item["result"]["decision"] in {"notify_directly", "needs_gemini"}
    ]

    selected_category_id = classification["category_id"]
    arbitration_required = classification["needs_category_arbitration"]

    if selected_category_id and selected_category_id in category_results:
        result = dict(category_results[selected_category_id]["result"])
    elif arbitration_required and candidate_ids:
        result = dict(category_results[candidate_ids[0]]["result"])
    elif category_results:
        # No candidate means every enabled category rejected the job.
        # Keep the first result only for the existing audit fields; the
        # final category remains empty and no notification is possible.
        result = dict(next(iter(category_results.values()))["result"])
    else:
        result = {}

    result["category_id"] = selected_category_id or ""
    result["category_selection_method"] = (
        "keyword_direct" if selected_category_id else ""
    )
    result["category_candidates"] = ", ".join(candidate_ids)

    filter_time = round(
        (time.perf_counter() - start) * 1000,
        2,
    )

    if not existing_incomplete:
        # Create the durable job row FIRST, with the original (long) URL.
        # Shortening runs later, only for accepted jobs (inside the
        # should_notify block), so rejected jobs never call the shortener.
        #
        # Ordering is the crash-safety invariant: the shortener keeps its
        # own independent "have I seen this URL" memory that is not
        # transactional with Loki's database. Creating the row first means
        # a crash can never orphan a job -- the original URL is already
        # durable, and shortening only populates the separate "Short URL"
        # column afterwards, never touching "URL" itself.
        # project_id was already extracted from the *original* URL above, so
        # cross-source dedup is unaffected either way.
        created = await logger.create_job_if_absent(legacy_job_uuid=legacy_job_uuid, job_uuid=job_uuid, job_id=job_id, source=job["source"], identity_source=identity_source, title=job["title"], description=job["description"], raw_message=job["raw_text"], filter_text=filter_text, company=job.get("company", ""), url=job["url"], filter_result=result, filter_time_ms=filter_time, save=False)

        if not created:
            # Another concurrent invocation won the atomic create race.
            # Re-read the durable row so a pending notification workflow
            # can be resumed if necessary, or leave a still-incomplete
            # classification for that invocation to finish.
            existing = await logger.get_job(job_uuid)
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
        claimed = await dedup.claim_cross_source_project(
            project_id,
            job_uuid,
        )

        if not claimed:
            await logger.update_job(job_uuid, final_decision="Rejected", decision_reason="Duplicate project from another source", save=True)
            print(
                f"[DEDUP] Project {project_id} was already claimed by "
                "another source -- skipping duplicate."
            )
            return

    final_decision = "Rejected"
    decision_reason = ""
    should_notify = False

    if not category_results:
        decision_reason = "No Enabled Categories"

    elif not candidate_ids:
        # Preserve the classifier's original audit reason when every
        # enabled category rejects the job. This keeps the historical
        # single-category reasons (Hard Reject / No Matching Keywords /
        # the classifier's own reason such as insufficient_signal) while
        # still remaining meaningful with multiple categories.
        # The historical single-category profile was data_analysis; its
        # result (not the alphabetically-first category's) is the
        # representative source for preserved rejection reasons.
        rejection_result = (
            category_results.get(RUNTIME.rejection_reason_category_id)
            or next(iter(category_results.values()))
        )["result"]
        if rejection_result.get("hard_reject"):
            decision_reason = "Hard Reject"
        elif not rejection_result.get("matched"):
            decision_reason = "No Matching Keywords"
        else:
            decision_reason = rejection_result.get("reason") or "No Matching Categories"

    elif arbitration_required:
        arbitration_start = time.perf_counter()
        candidates = []
        for category_id in candidate_ids:
            profile = get_category(category_id)
            if profile is None:
                raise RuntimeError(f"Enabled category disappeared: {category_id}")
            candidates.append({
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "arbitration_context": profile.arbitration_context,
                "result": category_results[category_id]["result"],
            })

        # Arbitration-only categories are intentionally absent from the
        # deterministic classifier. They still need to be present in the
        # arbitration candidate context so Gemini receives the full policy
        # and Groq receives its compact arbitration_context.
        for profile in arbitration_only_categories():
            candidates.append({
                "id": profile.id,
                "name": profile.name,
                "description": profile.description,
                "arbitration_context": profile.arbitration_context,
                "result": {
                    "decision": "needs_gemini",
                    "reason": "Arbitration-only category",
                    "categories": [],
                    "negative_categories": [],
                },
            })

        # One provider call for the complete candidate set. The shared LLM
        # manager builds the system policy from each candidate's live
        # category-specific llm_prompt.py, so those profiles directly govern
        # production arbitration without making one provider call per category.
        # Deadline for the entire arbitration rotation (provider + model + key
        # attempts). This bounds the total wall-clock time the rotation layer
        # may spend starting new attempts, preventing abandoned threads from
        # continuing through large rotations after the outer timeout fires.
        # In-flight provider calls are still bounded by their own HTTP timeouts.
        arbitration_deadline = time.monotonic() + RUNTIME.external_call_timeout_seconds
        try:
            arbitration = await call_with_timeout(
                asyncio.to_thread(
                    arbitrate_category,
                    filter_text,
                    candidates,
                    deadline=arbitration_deadline,
                ),
                label="LLM arbitration call",
            )
        except Exception as e:
            await logger.log_error("LLM Arbitration", e, job_uuid, save=False)
            final_decision = "Pending"
            decision_reason = "LLM Error"
            # See the "Classification Retry Not Before" check above:
            # this is what actually paces retries to
            # notification_retry_interval regardless of how often a
            # source's own rediscovery (e.g. FreeHub polling) re-
            # surfaces this same still-pending job in the meantime.
            retry_not_before = time.time() + RUNTIME.notification_retry_interval
            await logger.update_job(job_uuid, final_decision=final_decision, decision_reason=decision_reason, category_id="", category_selection_method="", category_candidates=", ".join(candidate_ids), classification_retry_not_before=str(retry_not_before), save=True)
            raise ClassificationPendingError(
                f"Classification for {job_uuid} is pending after LLM provider failure"
            ) from e
        else:
            arbitration_time = round(
                (time.perf_counter() - arbitration_start) * 1000,
                2,
            )
            selected = arbitration["selected_category"]
            decision_reason = arbitration["reason"]

            if selected == "none":
                final_decision = "Rejected"
                decision_reason = decision_reason or "LLM Arbitration: No Category"
            else:
                # parse_arbitration_response already constrains this to
                # the candidate set plus arbitration-only categories; keep a
                # second local check as a defense against future provider/parser
                # changes.
                arbitration_only_ids = {
                    profile.id for profile in arbitration_only_categories()
                }
                if selected not in candidate_ids and selected not in arbitration_only_ids:
                    raise RuntimeError(
                        f"Arbitration selected non-candidate category: {selected}"
                    )
                selected_category_id = selected
                # For arbitration-only categories (e.g., full_stack), there is
                # no deterministic filter result. Build a minimal result from
                # the category profile for audit fields.
                if selected in category_results:
                    result = dict(category_results[selected]["result"])
                else:
                    # Arbitration-only category: synthesize a minimal filter
                    # result with empty evidence for logging.
                    profile = get_category(selected)
                    if profile is None:
                        raise RuntimeError(f"Arbitration selected unknown category: {selected}")
                    result = {
                        "decision": "needs_gemini",
                        "reason": decision_reason,
                        "categories": [],
                        "negative_categories": [],
                        "has_core_positive": False,
                        "has_core_negative": False,
                        "core_positive_hit_count": 0,
                        "supporting_positive_weight": 0,
                        "supporting_negative_weight": 0,
                        "title_core_positive": False,
                        "title_core_negative": False,
                        "positive_core_matches": [],
                        "positive_supporting_matches": [],
                        "negative_core_matches": [],
                        "negative_supporting_matches": [],
                        "hard_reject": False,
                        "hard_reject_matches": [],
                        "notify_directly": False,
                        "needs_gemini": True,
                    }
                result["category_id"] = selected
                result["category_selection_method"] = "llm"
                result["category_candidates"] = ", ".join(candidate_ids)
                final_decision = "Accepted"
                should_notify = True

            await logger.update_job(job_uuid, gemini_decision=selected, save=False)
            await logger.log_gemini(job_uuid=job_uuid, decision_before=result.get("decision", ""), reason_before=result.get("reason", ""), prompt_tokens="", completion_tokens="", response_time_ms=arbitration_time, decision=selected, confidence=arbitration["confidence"], provider=arbitration.get("provider", ""), save=False)

    elif selected_category_id:
        final_decision = "Accepted"
        decision_reason = result.get("reason") or "Direct Category Match"
        should_notify = True

    await logger.update_job(job_uuid, final_decision=final_decision, decision_reason=decision_reason, category_id=result.get("category_id", ""), category_selection_method=result.get("category_selection_method", ""), category_candidates=result.get("category_candidates", ""), save=False)

    if should_notify:
        final_category_id = result.get("category_id", "")

        # Persist the fact that this job requires notification BEFORE
        # creating either the subscriber queue or the private external
        # side effect. Recovery can therefore re-establish both paths.
        await logger.update_job(job_uuid, notification_status="Pending", save=True)

        # Best-effort URL shortening, run only now that the job is
        # Accepted -- rejected jobs never call the shortener, so no orphan
        # short links or wasted upstream calls are created for jobs that
        # will never be delivered. job_uuid, not the source-local job_id
        # (which is only unique within its own source), is passed through
        # as the shortener's own jobId key -- the shortener upserts on it,
        # so this call never needs to check for prior existence itself.
        #
        # The shortened link is written to its own "Short URL" column, never
        # to "URL": job["url"] stays the original link, so the channel and
        # notification path (sourced from row["URL"]) is unaffected, and only
        # the per-user DM renderer consumes the short link (see
        # app.adapters.user.renderer). _resume_pending_notifications below
        # re-reads the durable row (via logger.get_job), so the committed
        # Short URL is what the renderer sees.
        #
        # Failure follows URL_SHORTENER_FAILURE_MODE inside
        # url_shortener.shorten(): fail_open returns the original URL and the
        # no-op guard below skips the write; fail_closed raises
        # UrlShorteningError, deliberately NOT caught here, so the source
        # worker leaves the job un-seen and it resumes from this durable row
        # on the next poll instead of being lost (see app.source_worker.
        # SourceWorker.run).
        shortened = await url_shortener.shorten(job_uuid, job["url"])
        if shortened and shortened != job["url"]:
            await logger.update_job(job_uuid, short_url=shortened, save=True)

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
                "Category ID": final_category_id,
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
    await logger.save()
