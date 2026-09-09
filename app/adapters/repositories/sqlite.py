"""SQLite persistence adapter.

The concrete backing store is injected by the repository registry (which owns
the wiring to the DBLogger persistence backend); the adapter exposes the small
semantic surface consumed by the application and owns the internal execution
bridge. The application sees only semantic operations.
"""
from app.ports import JobRepository


class SQLiteRepository(JobRepository):
    id = "sqlite"

    def __init__(self, db):
        self._db = db

    def _call(self, name, *args, **kwargs):
        return self._db.run(getattr(self._db, name), *args, **kwargs)

    def get_job(self, *args, **kwargs): return self._call('get_job', *args, **kwargs)
    def create_job_if_absent(self, *args, **kwargs): return self._call('create_job_if_absent', *args, **kwargs)
    def update_job(self, *args, **kwargs): return self._call('update_job', *args, **kwargs)
    def has_job(self, *args, **kwargs): return self._call('has_job', *args, **kwargs)
    def log_job(self, *args, **kwargs): return self._call('log_job', *args, **kwargs)
    def log_error(self, *args, **kwargs): return self._call('log_error', *args, **kwargs)
    def log_notification(self, *args, **kwargs): return self._call('log_notification', *args, **kwargs)
    def log_gemini(self, *args, **kwargs): return self._call('log_gemini', *args, **kwargs)
    def log_notification_guard(self, *args, **kwargs): return self._call('log_notification_guard', *args, **kwargs)
    def get_latest_guard_decision(self, *args, **kwargs): return self._call('get_latest_guard_decision', *args, **kwargs)
    def get_latest_guard_decision_with_category(self, *args, **kwargs): return self._call('get_latest_guard_decision_with_category', *args, **kwargs)
    def get_incomplete_notification_jobs(self, *args, **kwargs): return self._call('get_incomplete_notification_jobs', *args, **kwargs)
    def get_incomplete_classification_jobs(self, *args, **kwargs): return self._call('get_incomplete_classification_jobs', *args, **kwargs)
    def claim_pending_classification(self, *args, **kwargs): return self._call('claim_pending_classification', *args, **kwargs)
    def queue_user_notifications(self, *args, **kwargs): return self._call('queue_user_notifications', *args, **kwargs)
    def ensure_user(self, *args, **kwargs): return self._call('ensure_user', *args, **kwargs)
    def record_subscription_event(self, *args, **kwargs): return self._call('record_subscription_event', *args, **kwargs)
    def get_user_sources(self, *args, **kwargs): return self._call('get_user_sources', *args, **kwargs)
    def get_user_categories(self, *args, **kwargs): return self._call('get_user_categories', *args, **kwargs)
    def set_user_source(self, *args, **kwargs): return self._call('set_user_source', *args, **kwargs)
    def set_user_category(self, *args, **kwargs): return self._call('set_user_category', *args, **kwargs)
    def ensure_channel_destination(self, *args, **kwargs): return self._call('ensure_channel_destination', *args, **kwargs)
    def get_destination(self, *args, **kwargs): return self._call('get_destination', *args, **kwargs)
    def reset_sending_user_notifications(self, *args, **kwargs): return self._call('reset_sending_user_notifications', *args, **kwargs)
    def cancel_pending_user_notifications(self, *args, **kwargs): return self._call('cancel_pending_user_notifications', *args, **kwargs)
    def update_user_notification(self, *args, **kwargs): return self._call('update_user_notification', *args, **kwargs)
    def set_destination_active(self, *args, **kwargs): return self._call('set_destination_active', *args, **kwargs)
    def claim_pending_user_notifications(self, *args, **kwargs): return self._call('claim_pending_user_notifications', *args, **kwargs)
    def initialize(self, *args, **kwargs): return self._call('initialize', *args, **kwargs)
    def save(self, *args, **kwargs): return self._call('save', *args, **kwargs)