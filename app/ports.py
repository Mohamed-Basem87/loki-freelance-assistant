"""Complete application port contracts. Concrete SDKs/backends implement these interfaces."""
from abc import ABC, abstractmethod
from typing import Any

class JobSource(ABC):
    id: str
    @property
    @abstractmethod
    def identity_source(self) -> str: raise NotImplementedError
    @abstractmethod
    async def poll(self) -> list[dict[str, Any]]: raise NotImplementedError
    @abstractmethod
    def normalize(self, job: dict[str, Any]) -> dict[str, Any]: raise NotImplementedError
    def job_identity(self, job: dict[str, Any]) -> str:
        value = job.get("job_id", job.get("uid"))
        if value is None or str(value).strip() == "":
            raise ValueError(f"Source {self.id!r} returned a job without a stable identity")
        return str(value)
    async def mark_seen(self, job: dict[str, Any]) -> None: return None
    async def backfill(self) -> list[dict[str, Any]]: return []
    async def aclose(self) -> None:
        """Release any resources (HTTP sessions, clients) this source owns.

        Default is a no-op so sources with nothing to release (e.g. the
        example adapter) need not implement it. Adapters that own a
        transport/session override this to close it.
        """
        return None

class HttpTransport(ABC):
    @abstractmethod
    async def request(self, method: str, url: str, *, headers: dict[str, str] | None = None, json: Any = None, timeout: float | None = None) -> Any: raise NotImplementedError
    async def get_json(self, url: str, *, timeout: float | None = None, headers: dict[str,str] | None = None) -> Any:
        response = await self.request("GET", url, headers=headers, timeout=timeout)
        try:
            return await response.json()
        finally:
            # Guarantee the underlying connection is released even if the
            # body read or JSON decode raises (see AioHttpResponse.aclose /
            # release -- a leaked response would otherwise pin a connection
            # on the shared pool).
            close = getattr(response, "aclose", None)
            if close is not None:
                await close()
    async def close(self) -> None:
        """Release owned connections/sessions. Default is a no-op for
        transports with nothing to release; a reusable-session transport
        (e.g. AioHttpTransport) overrides this."""
        return None

class StateStore(ABC):
    @abstractmethod
    def load(self): raise NotImplementedError
    @abstractmethod
    async def get_last_message_id(self, channel_id): raise NotImplementedError
    @abstractmethod
    async def async_set_last_message_id(self, channel_id, message_id): raise NotImplementedError
    @abstractmethod
    async def get_freehub_pending(self, source): raise NotImplementedError
    @abstractmethod
    async def async_set_freehub_pending(self, source, values): raise NotImplementedError
    @abstractmethod
    async def get_freehub_backfill_page(self, source): raise NotImplementedError
    @abstractmethod
    async def async_set_freehub_backfill_page(self, source, page): raise NotImplementedError
    @abstractmethod
    async def claim_cross_source_project(self, project_id, job_uuid): raise NotImplementedError

class DedupStore(ABC):
    @abstractmethod
    async def claim_cross_source_project(self, project_id: str, job_uuid: str) -> bool: raise NotImplementedError
    @abstractmethod
    async def get_seen(self, source: str) -> list[str]: raise NotImplementedError
    @abstractmethod
    async def set_seen(self, source: str, values: list[str]) -> None: raise NotImplementedError
    @abstractmethod
    async def get_pending(self, source: str) -> list[dict]: raise NotImplementedError
    @abstractmethod
    async def set_pending(self, source: str, values: list[dict]) -> None: raise NotImplementedError
    @abstractmethod
    async def get_backfill_page(self, source: str) -> int: raise NotImplementedError
    @abstractmethod
    async def set_backfill_page(self, source: str, page: int) -> None: raise NotImplementedError

class JobRepository(ABC):
    """Semantic persistence port.

    Implementations expose operations the application actually needs. Legacy
    storage APIs belong behind the adapter boundary and are not part of this
    contract.
    """

    # Trimmed for the ingest/classify/guard node: notification delivery,
    # subscriber fan-out, the delivery-retry loop, AND all user/
    # subscription-profile management moved out. Fan-out/delivery moved
    # to a separate service that consumes this node's Redis stream (see
    # JobStreamPublisher below); user profile management (Telegram
    # command surface, categories/sources preferences, /start /stop)
    # moved to a separate Node.js service that owns the users/
    # subscription_events/user_notifications tables directly. This node
    # only reads/writes `jobs`, `categories` (the job-category catalog --
    # jobs."Category ID" has a FK to it), `notification_guard`, and the
    # append-only `gemini`/`errors` logs.
    _METHODS = (
        "initialize", "save", "get_job", "create_job_if_absent", "update_job",
        "has_job", "log_error", "log_gemini",
        "log_notification_guard", "get_latest_guard_decision", "get_latest_guard_decision_with_category",
        "get_incomplete_notification_jobs", "get_incomplete_classification_jobs",
        "claim_pending_classification",
    )

    # Every operation exposed to application code is declared here. The
    # signatures stay intentionally semantic; infrastructure-specific
    # execution details remain inside the adapter.
    def initialize(self): raise NotImplementedError
    def save(self): raise NotImplementedError
    def get_job(self, job_uuid): raise NotImplementedError
    def create_job_if_absent(self, **kwargs): raise NotImplementedError
    def update_job(self, job_uuid, **kwargs): raise NotImplementedError
    def has_job(self, job_uuid): raise NotImplementedError
    def log_error(self, *args, **kwargs): raise NotImplementedError
    def log_gemini(self, *args, **kwargs): raise NotImplementedError
    def log_notification_guard(self, *args, **kwargs): raise NotImplementedError
    def get_latest_guard_decision(self, job_uuid): raise NotImplementedError
    def get_latest_guard_decision_with_category(self, job_uuid): raise NotImplementedError
    def get_incomplete_notification_jobs(self): raise NotImplementedError
    def get_incomplete_classification_jobs(self): raise NotImplementedError
    def claim_pending_classification(self, job_uuid, lease_until): raise NotImplementedError

    # NOTE: The Postgres backend commits each call immediately (one
    # engine.begin() block per method). The `save` parameter on many
    # methods is retained for API compatibility but has NO effect on
    # transaction boundaries. True atomic batching is only available via
    # `create_job_if_absent`, which wraps its check-and-insert in a single
    # explicit transaction.


class JobStreamPublisher(ABC):
    """Publishes a guard-approved job to the downstream notification
    pipeline (a separate service). This node's responsibility ends at a
    successful publish; fan-out and delivery are owned entirely by the
    consumer on the other end of the stream."""

    @abstractmethod
    async def publish(self, job_row: dict) -> None: raise NotImplementedError



class CrashRecovery(ABC):
    @abstractmethod
    async def recover(self): raise NotImplementedError

class Worker(ABC):
    id: str
    @abstractmethod
    async def run(self): raise NotImplementedError
