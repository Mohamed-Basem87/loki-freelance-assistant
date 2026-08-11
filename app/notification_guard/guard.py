import asyncio
import time

from app.notification_guard.config import NOTIFICATION_GUARD_ENABLED
from app.notification_guard.groq import GroqNotificationGuard
from app.notification_guard.logger import log_guard_decision


class NotificationGuard:

    def __init__(self):

        self.enabled = NOTIFICATION_GUARD_ENABLED

        self.provider = (
            GroqNotificationGuard()
            if self.enabled
            else None
        )

    async def allow(
        self,
        job: dict,
        *,
        original_decision="",
    ) -> bool:
        """
        Return True only when the job is allowed to reach the notifier.

        The guard is fail-closed:
        provider errors, malformed responses, and unexpected failures
        suppress the notification.

        Every actual guard evaluation is recorded in the dedicated
        NotificationGuard sheet through the existing ExcelLogger.
        """

        if not self.enabled:
            return True

        started = time.perf_counter()

        try:

            allowed = await asyncio.to_thread(
                self.provider.evaluate,
                job.get("title", ""),
                job.get("description", ""),
            )

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision=(
                    "notify"
                    if allowed
                    else "do_not_notify"
                ),
                provider="Groq",
                model=self.provider.model,
                response_time_ms=response_time_ms,
            )

            return allowed

        except Exception as exc:

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision="error",
                provider="Groq",
                model=(
                    self.provider.model
                    if self.provider is not None
                    else "",
                ),
                response_time_ms=response_time_ms,
                error=str(exc),
            )

            return False


notification_guard = NotificationGuard()
