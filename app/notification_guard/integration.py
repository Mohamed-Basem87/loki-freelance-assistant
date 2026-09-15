from app.notification_guard.guard import NotificationGuard


class NotificationGuardIntegration:
    """
    Guard decision + category-resolution surface for job_processor.py.

    The guard decision belongs to the *job*, not to an individual
    notification attempt. It is looked up from the durable
    `notification_guard` table before ever asking the guard's provider:

    - A previously persisted "notify" or "do_not_notify" is a valid,
      final decision and is reused forever for that job -- for the
      initial process_job() invocation, for every later
      retry_incomplete_notifications() sweep pass, and across process
      restarts. The guard's provider is never asked again for that job
      once a valid decision exists.
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

    A "notify" decision also carries a category (see "Guard Category"
    on the `notification_guard` table): either the job's original
    keyword-matched category, unchanged, or "full_stack" when the guard
    determined the work is broader than a single specialist category.
    resolve_category() is the single place this is decided and
    persisted -- it runs once, before allow(), from
    app.services.job_processor._resume_pending_notifications_unlocked.
    allow() never triggers a fresh evaluation itself; it only ever
    consults the decision resolve_category() already made and
    persisted.
    """

    def __init__(self, guard: NotificationGuard, repository=None):
        self.guard = guard
        if repository is None:
            from app.wiring.dependencies import logger
            repository = logger
        self.repository = repository

    async def allow(self, kwargs: dict) -> bool:

        # Disabled guard is a transparent no-op. It must never turn an
        # otherwise deliverable notification into a suppression.
        if not getattr(self.guard, "enabled", True):
            return True

        # Existing LLM-reviewed jobs bypass this guard.
        if kwargs.get("ai_used", False):
            return True

        job_uuid = kwargs.get("job_uuid", "")

        persisted = await self.repository.get_latest_guard_decision(job_uuid)

        if persisted == "notify":
            return True

        if persisted == "do_not_notify":
            return False

        # persisted is None (never evaluated) or "error" (transient
        # failure, not reusable). In the current pipeline this should
        # not be reachable here: resolve_category() below always runs
        # first for any accepted, category-bearing job and leaves a
        # durable "notify"/"do_not_notify" behind. Fail closed rather
        # than silently notifying on an unresolved decision.
        return False

    async def resolve_category(
        self,
        job_uuid: str,
        row: dict,
        category_id: str,
    ) -> str:
        """
        Determine (and durably persist) whether this job should be
        delivered under its original keyword-matched category or
        reclassified to "full_stack", and return the category to
        actually use. Installed in place of app.services.job_processor's
        _resolve_notification_category no-op; called once per
        _resume_pending_notifications_unlocked invocation, before
        allow().

        This is the only place a fresh guard evaluation happens --
        allow() only ever consults the persisted result afterward.
        """

        if not getattr(self.guard, "enabled", True):
            return category_id

        ai_used = str(row.get("Needs Gemini") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
        )
        if ai_used or not category_id:
            # Existing LLM-reviewed jobs bypass the guard completely,
            # exactly as today -- never touched by full_stack
            # reclassification either.
            return category_id

        decision, persisted_category = await self.repository.get_latest_guard_decision_with_category(job_uuid)

        if decision == "notify":
            resolved = persisted_category or category_id
        elif decision == "do_not_notify":
            resolved = category_id
        else:
            # Never evaluated, or only a transient "error" so far --
            # ask the guard fresh. decide() persists whatever it
            # produces (including another "error"), so a later
            # resumed pass makes the same check again.
            job = {
                "job_uuid": job_uuid,
                "source": row.get("Source", ""),
                "title": row.get("Title", ""),
                "description": row.get("Description", ""),
            }

            result = await self.guard.decide(
                job,
                original_decision=row.get("Final Decision", "Accepted"),
                category_id=category_id,
            )

            resolved = result["category_id"] if result["allowed"] else category_id

        if resolved != category_id:
            # Idempotent and self-healing: safe to redo on every call,
            # including a resumed pass after a crash between this
            # write and the notification_guard log write above -- the
            # durable notification_guard row is the source of truth
            # this reapplies from, so a partial previous attempt heals
            # itself rather than compounding or getting stuck.
            full_stack_name = _category_display_name(resolved)
            await self.repository.update_job(job_uuid, category_id=resolved, category_selection_method="llm", categories=[full_stack_name] if full_stack_name else [], save=True)

        return resolved


def _category_display_name(category_id: str) -> str:
    from app.domain.categories.registry import get_category

    profile = get_category(category_id)
    return profile.name if profile is not None else ""
