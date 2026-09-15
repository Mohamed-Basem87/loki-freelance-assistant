"""Redis Streams adapter: publishes a guard-approved job for the
separate notification/delivery service to consume.

Publishes the full job row as stream fields (not just the job_uuid) so
the consumer never needs its own read back to Postgres to render and
fan out a notification.

Exceptions are never swallowed here: job_processor.py only marks a job
"Complete" after publish() returns successfully (see
_resume_pending_notifications_unlocked), so a raised exception leaves
the job's "Notification Status" at "Pending" and the existing
get_incomplete_notification_jobs() retry sweep picks it back up and
republishes it -- the same crash-safety mechanism that already existed
for the old direct-notify path, unchanged.
"""
from app.ports import JobStreamPublisher


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class RedisJobStreamPublisher(JobStreamPublisher):
    id = "redis"

    def __init__(self, client, stream: str = "jobs:notify", maxlen: int | None = 100_000):
        self._client = client
        self._stream = stream
        self._maxlen = maxlen

    async def publish(self, job_row: dict) -> None:
        fields = {str(key): _stringify(value) for key, value in job_row.items()}
        await self._client.xadd(
            self._stream,
            fields,
            maxlen=self._maxlen,
            approximate=True,
        )

    async def close(self) -> None:
        await self._client.aclose()
