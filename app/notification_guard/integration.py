from app.logger import logger as db_logger
from app.notification_guard.guard import NotificationGuard


class NotificationGuardIntegration:
    """
    Runtime adapter for the existing notification pipeline.

    The production application remains untouched. The adapter replaces
    the notification references inside app.job_processor at runtime.

    The guard decision belongs to the *job*, not to an individual
    notification attempt. It is looked up from the durable
    `notification_guard` table (the existing persistence architecture
    -- see app.logger / app.notification_guard.logger) before ever
    asking the guard's provider:

    - A previously persisted "notify" or "do_not_notify" is a valid,
      final decision and is reused forever for that job -- for the
      private/channel pair of a single process_job() invocation, for
      every later retry_incomplete_notifications() sweep pass, and
      across process restarts. The guard's provider (Groq) is never
      asked again for that job once a valid decision exists.
    - "error" (a transient provider/evaluation failure) is NOT a
      valid decision and is never reused -- the guard is evaluated
      fresh the next time this job needs a decision, whether that's
      later in the same invocation or a future retry sweep pass. This
      keeps a genuine content-based rejection distinguishable from a
      transient outage: only the former is durable enough to skip
      re-evaluation.
    - No row at all (guard never evaluated for this job) behaves the
      same as "error": evaluate fresh.

    Existing LLM-reviewed jobs bypass the guard completely and never
    touch this lookup.
    """

    def __init__(self, guard: NotificationGuard):

        self.guard = guard

    async def _allow(self, kwargs: dict) -> bool:

        # Existing LLM-reviewed jobs bypass this guard.
        if kwargs.get("ai_used", False):
            return True

        job_uuid = kwargs.get("job_uuid", "")

        persisted = await db_logger.run(
            db_logger.get_latest_guard_decision,
            job_uuid,
        )

        if persisted == "notify":
            return True

        if persisted == "do_not_notify":
            return False

        # persisted is None (never evaluated) or "error" (transient
        # failure, not reusable) -- evaluate fresh. guard.allow()
        # persists whatever it produces, including another "error",
        # so the next caller (the other notification leg, or a later
        # retry sweep pass) makes the same check again.
        job = {
            "job_uuid": job_uuid,
            "source": kwargs.get("source", ""),
            "title": kwargs.get("title", ""),
            "description": kwargs.get("description", ""),
        }

        return await self.guard.allow(
            job,
            original_decision=kwargs.get(
                "decision",
                "",
            ),
            category_id=kwargs.get("category_id", ""),
        )

    def wrap_private(self, original):

        async def wrapped(**kwargs):

            if not await self._allow(kwargs):
                return False

            return await original(**kwargs)

        return wrapped

    def wrap_routing(self, original):

        async def wrapped(job_uuid, category_id):
            if not category_id:
                return 0

            row = await db_logger.run(db_logger.get_job, job_uuid)
            if row is None:
                return 0

            allowed = await self._allow({
                "job_uuid": job_uuid,
                "source": row.get("Source", ""),
                "title": row.get("Title", ""),
                "description": row.get("Description", ""),
                "decision": row.get("Final Decision", "Accepted"),
                "ai_used": str(row.get("Needs Gemini") or "").strip().lower() in ("1", "true", "yes", "y"),
                "category_id": category_id,
            })

            if not allowed:
                return 0

            return await original(job_uuid, category_id)

        return wrapped



def install():

    import app.job_processor as job_processor

    integration = NotificationGuardIntegration(
        NotificationGuard()
    )

    job_processor.send_notification = (
        integration.wrap_private(
            job_processor.send_notification
        )
    )

    # Subscriber routing is the category-based delivery path. It must
    # use the same Guard decision as the private notification.
    job_processor.queue_for_category = integration.wrap_routing(
        job_processor.queue_for_category
    )

    return integration
