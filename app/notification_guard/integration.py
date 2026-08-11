from dataclasses import dataclass

from app.notification_guard.guard import NotificationGuard


@dataclass
class _Decision:
    allowed: bool
    remaining: int


class NotificationGuardIntegration:
    """
    Runtime adapter for the existing notification pipeline.

    The production application remains untouched. The adapter replaces
    the notification references inside app.job_processor at runtime.

    The guard is evaluated exactly once per DIRECT_NOTIFY job and the
    same result is reused for the private and channel notifications.

    Existing LLM-reviewed jobs bypass the guard completely.
    """

    def __init__(self, guard: NotificationGuard):

        self.guard = guard
        self._decisions: dict[str, _Decision] = {}

    async def _allow(self, kwargs: dict) -> bool:

        # Existing LLM-reviewed jobs bypass this guard.
        if kwargs.get("ai_used", False):
            return True

        job_uuid = kwargs.get("job_uuid", "")

        cached = self._decisions.get(job_uuid)

        if cached is not None:

            allowed = cached.allowed

            cached.remaining -= 1

            if cached.remaining <= 0:
                self._decisions.pop(job_uuid, None)

            return allowed

        job = {
            "job_uuid": job_uuid,
            "source": kwargs.get("source", ""),
            "title": kwargs.get("title", ""),
            "description": kwargs.get("description", ""),
        }

        allowed = await self.guard.allow(
            job,
            original_decision=kwargs.get(
                "decision",
                "",
            ),
        )

        # process_job normally calls both notification functions once.
        # Reuse the same Groq decision for the second notification.
        self._decisions[job_uuid] = _Decision(
            allowed=allowed,
            remaining=1,
        )

        return allowed

    def wrap_private(self, original):

        async def wrapped(**kwargs):

            if not await self._allow(kwargs):
                return False

            return await original(**kwargs)

        return wrapped

    def wrap_channel(self, original):

        async def wrapped(**kwargs):

            if not await self._allow(kwargs):
                return False

            return await original(**kwargs)

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

    job_processor.send_channel_notification = (
        integration.wrap_channel(
            job_processor.send_channel_notification
        )
    )

    return integration
