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
        if value is None: raise ValueError(f"Source {self.id!r} returned a job without a stable identity")
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

class MessageRenderer(ABC):
    @abstractmethod
    def render(self, payload: dict[str, Any]) -> dict[str, Any]: raise NotImplementedError

class UserMessageRenderer(ABC):
    @abstractmethod
    def render_user(self, job_row: dict[str, Any], category_id: str) -> dict[str, Any]: raise NotImplementedError

class NotificationSink(ABC):
    id: str
    @abstractmethod
    async def send(self, rendered: dict[str, Any] | None = None, **kwargs: Any) -> bool: raise NotImplementedError


class NotificationTransport(ABC):
    """Transport-neutral outbound message API used by notification sinks."""
    @abstractmethod
    async def send_message(self, **kwargs: Any) -> Any: raise NotImplementedError

class UserMessaging(ABC):
    @abstractmethod
    async def notify_user(self, user_id: int, rendered: dict[str, Any], **kwargs: Any) -> bool: raise NotImplementedError

class CommandSurface(ABC):
    @abstractmethod
    async def start(self) -> None: raise NotImplementedError
    @abstractmethod
    async def stop(self) -> None: raise NotImplementedError
    @abstractmethod
    def application(self) -> Any: raise NotImplementedError
    async def register_channel(self) -> None: return None

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

    _METHODS = (
        "initialize", "save", "get_job", "create_job_if_absent", "update_job",
        "has_job", "log_job", "log_error", "log_notification", "log_gemini",
        "log_notification_guard", "get_latest_guard_decision", "get_latest_guard_decision_with_category",
        "get_incomplete_notification_jobs", "get_incomplete_classification_jobs",
        "claim_pending_classification", "queue_user_notifications",
        "ensure_user", "record_subscription_event", "get_user_sources",
        "get_user_categories", "set_user_source", "set_user_category",
        "ensure_channel_destination", "get_destination",
        "reset_sending_user_notifications", "update_user_notification",
        "cancel_pending_user_notifications",
        "set_destination_active", "claim_pending_user_notifications",
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
    def log_job(self, *args, **kwargs): raise NotImplementedError
    def log_error(self, *args, **kwargs): raise NotImplementedError
    def log_notification(self, *args, **kwargs): raise NotImplementedError
    def log_gemini(self, *args, **kwargs): raise NotImplementedError
    def log_notification_guard(self, *args, **kwargs): raise NotImplementedError
    def get_latest_guard_decision(self, job_uuid): raise NotImplementedError
    def get_latest_guard_decision_with_category(self, job_uuid): raise NotImplementedError
    def get_incomplete_notification_jobs(self): raise NotImplementedError
    def get_incomplete_classification_jobs(self): raise NotImplementedError
    def claim_pending_classification(self, job_uuid, lease_until): raise NotImplementedError
    def queue_user_notifications(self, job_uuid, category_id, source="", save=True): raise NotImplementedError
    def ensure_user(self, *args, **kwargs): raise NotImplementedError
    def record_subscription_event(self, *args, **kwargs): raise NotImplementedError
    def get_user_sources(self, user_id): raise NotImplementedError
    def get_user_categories(self, user_id): raise NotImplementedError
    def set_user_source(self, *args, **kwargs): raise NotImplementedError
    def set_user_category(self, *args, **kwargs): raise NotImplementedError
    def ensure_channel_destination(self, *args, **kwargs): raise NotImplementedError
    def get_destination(self, *args, **kwargs): raise NotImplementedError
    def reset_sending_user_notifications(self, save=True): raise NotImplementedError
    def cancel_pending_user_notifications(self, telegram_user_id, save=True): raise NotImplementedError
    def update_user_notification(self, notification_id, status, **kwargs): raise NotImplementedError
    def set_destination_active(self, *args, **kwargs): raise NotImplementedError
    def claim_pending_user_notifications(self, limit=20): raise NotImplementedError

    # NOTE: The SQLite backend runs in autocommit mode (isolation_level=None).
    # The `save` parameter on many methods is retained for API compatibility
    # but has NO effect on transaction boundaries -- each statement is
    # committed immediately. True atomic batching is only available via
    # `create_job_if_absent` which wraps its check-and-insert in an
    # explicit BEGIN IMMEDIATE/COMMIT block.



class CrashRecovery(ABC):
    @abstractmethod
    async def recover(self): raise NotImplementedError

class Worker(ABC):
    id: str
    @abstractmethod
    async def run(self): raise NotImplementedError
