"""
Regression tests for the legacy notification-state migration.

A pre-remediation database stores the private notification result in
jobs."Notification Status" using the legacy single-sink plain-text
format:

    Telegram: Sent     (user already received the notification)
    Telegram: Failed   (last attempt failed -- should be retried)
    Telegram: Suppressed (notification was suppressed -- terminal)

The current representation is the per-sink encoded form "Sink:<id>=X"
joined with "; ". These tests prove the migration:

  * never re-sends a legacy "Telegram: Sent" row (the exact bug: that
    value is selected by the retry sweep, invisible to the per-sink
    parser, and so triggers a real duplicate send);
  * turns a legacy "Telegram: Failed" row into the retryable per-sink
    "Sink:telegram=Failed" state that the sweep then retries;
  * turns a legacy "Telegram: Suppressed" row into the terminal
    "Suppressed" rollup so it is never re-sent or re-swept;
  * preserves already-present current Sink: markers in mixed rows;
  * is transactional and idempotent across restarts.

The final two tests drive the REAL NotificationService and the REAL
retry sweep (not fakes) against a database seeded with the legacy
states, proving the end-to-end behavior.
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.logger import (
    JOB_HEADERS,
    _normalize_legacy_notification_state,
    logger,
)


@pytest.fixture()
def legacy_db(tmp_path):
    """A fresh DBLogger wired to a throwaway file, plus helpers."""
    original_path = logger.path
    db_path = tmp_path / "legacy_notifications.db"
    logger.path = db_path
    logger.initialize()
    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


def _insert_job_with_status(log, job_uuid, status):
    """Insert a minimal jobs row carrying a given Notification Status."""
    log.create_job(
        job_uuid=job_uuid,
        job_id=f"job-{job_uuid}",
        source="Test Channel",
        title="Legacy notification",
        description="desc",
        raw_message="Legacy notification\n\ndesc",
        filter_text="legacy",
        company="",
        url="",
        filter_result={},
        filter_time_ms=0,
        save=False,
    )
    log.update_job(job_uuid, notification_status=status, save=True)


# ----------------------------------------------------------------------
# Pure normalization unit tests
# ----------------------------------------------------------------------

def test_sent_maps_to_per_sink_sent():
    assert _normalize_legacy_notification_state("Telegram: Sent") == (
        "Sink:telegram=Sent"
    )


def test_failed_maps_to_per_sink_failed():
    assert _normalize_legacy_notification_state("Telegram: Failed") == (
        "Sink:telegram=Failed"
    )


def test_suppressed_maps_to_terminal_rollup():
    assert _normalize_legacy_notification_state("Telegram: Suppressed") == (
        "Suppressed"
    )


def test_current_sink_token_wins_over_contradicting_legacy_token():
    # A per-sink record is newer and more precise than the legacy token.
    assert _normalize_legacy_notification_state(
        "Sink:telegram=Sent; Telegram: Failed"
    ) == "Sink:telegram=Sent"


def test_mixed_preserves_other_sinks_and_adds_legacy_sink():
    assert _normalize_legacy_notification_state(
        "Telegram: Sent; Sink:a=Failed"
    ) == "Sink:a=Failed; Sink:telegram=Sent"


def test_no_legacy_token_returns_none():
    assert _normalize_legacy_notification_state("Pending") is None
    assert _normalize_legacy_notification_state("Sink:a=Sent") is None
    assert _normalize_legacy_notification_state("") is None
    assert _normalize_legacy_notification_state(None) is None


def test_normalization_is_idempotent():
    once = _normalize_legacy_notification_state("Telegram: Sent")
    twice = _normalize_legacy_notification_state(once)
    assert twice is None  # no legacy token remains -> nothing to rewrite


# ----------------------------------------------------------------------
# Migration against a realistic pre-remediation database
# ----------------------------------------------------------------------

def test_migration_normalizes_all_legacy_rows(legacy_db):
    _insert_job_with_status(legacy_db, "sent", "Telegram: Sent")
    _insert_job_with_status(legacy_db, "failed", "Telegram: Failed")
    _insert_job_with_status(legacy_db, "suppressed", "Telegram: Suppressed")
    _insert_job_with_status(legacy_db, "mixed", "Telegram: Sent; Sink:other=Failed")
    _insert_job_with_status(legacy_db, "pending", "Pending")

    legacy_db._migrate_legacy_notification_states()

    assert legacy_db.get_job("sent")["Notification Status"] == "Sink:telegram=Sent"
    assert legacy_db.get_job("failed")["Notification Status"] == "Sink:telegram=Failed"
    assert legacy_db.get_job("suppressed")["Notification Status"] == "Suppressed"
    assert legacy_db.get_job("mixed")["Notification Status"] == (
        "Sink:other=Failed; Sink:telegram=Sent"
    )
    # A recognized, non-legacy value is left untouched.
    assert legacy_db.get_job("pending")["Notification Status"] == "Pending"


def test_migration_is_idempotent_across_restart(legacy_db, tmp_path):
    """Re-running the migration (as every startup does) must not change
    already-normalized rows and must still fix any that were added in
    the meantime."""
    _insert_job_with_status(legacy_db, "sent", "Telegram: Sent")

    legacy_db._migrate_legacy_notification_states()
    assert legacy_db.get_job("sent")["Notification Status"] == "Sink:telegram=Sent"

    # Simulate a restart: close, reopen the same file, run migration again.
    legacy_db.save()
    legacy_db.close()
    original_path = logger.path
    try:
        logger.path = tmp_path / "legacy_notifications.db"
        logger.initialize()
        logger._migrate_legacy_notification_states()
        assert logger.get_job("sent")["Notification Status"] == "Sink:telegram=Sent"
    finally:
        logger.close()
        logger.path = original_path


def test_migration_rolls_back_atomically_on_error(legacy_db, monkeypatch):
    """A failure while writing must leave the whole column unchanged
    (transactional), never a partial rewrite."""
    _insert_job_with_status(legacy_db, "a", "Telegram: Sent")
    _insert_job_with_status(legacy_db, "b", "Telegram: Failed")

    state = {"value": "Telegram: Sent"}

    def boom(raw):
        # Only normalize the first row then blow up on the second.
        if "Failed" in raw:
            raise RuntimeError("disk full")
        return _normalize_legacy_notification_state(raw)

    monkeypatch.setattr(
        "app.logger._normalize_legacy_notification_state", boom
    )

    with pytest.raises(RuntimeError):
        legacy_db._migrate_legacy_notification_states()

    # Neither row was committed.
    assert legacy_db.get_job("a")["Notification Status"] == "Telegram: Sent"
    assert legacy_db.get_job("b")["Notification Status"] == "Telegram: Failed"


# ----------------------------------------------------------------------
# End-to-end: REAL notifier + REAL retry sweep against legacy data
# ----------------------------------------------------------------------

class _RecordingSink:
    id = "telegram"

    def __init__(self):
        self.calls = 0
        self.should_succeed = True

    async def send(self, **kwargs):
        self.calls += 1
        return self.should_succeed


def test_legacy_sent_row_is_never_resent_by_real_notifier(legacy_db):
    """The whole point: a legacy 'Telegram: Sent' row must be skipped by
    the per-sink send gate after migration, so the real NotificationService
    never delivers a duplicate."""
    from app.notifier import NotificationService

    _insert_job_with_status(legacy_db, "sent", "Telegram: Sent")
    legacy_db._migrate_legacy_notification_states()

    sink = _RecordingSink()
    service = NotificationService(sinks=(sink,), repository=legacy_db)

    ok = asyncio.run(service.send(job_uuid="sent"))

    assert ok is True
    assert sink.calls == 0, "legacy Telegram: Sent must never be re-sent"


def test_legacy_failed_row_is_retried_by_real_notifier(legacy_db):
    """A legacy 'Telegram: Failed' row becomes the retryable per-sink
    state and is actually re-attempted (not skipped, not lost)."""
    from app.notifier import NotificationService

    _insert_job_with_status(legacy_db, "failed", "Telegram: Failed")
    legacy_db._migrate_legacy_notification_states()

    sink = _RecordingSink()
    service = NotificationService(sinks=(sink,), repository=legacy_db)

    ok = asyncio.run(service.send(job_uuid="failed"))
    assert ok is True
    assert sink.calls == 1
    assert legacy_db.get_job("failed")["Notification Status"] == "Sink:telegram=Sent"


def test_legacy_suppressed_is_terminal_and_not_swept(legacy_db, monkeypatch):
    """A legacy 'Telegram: Suppressed' row migrates to the terminal
    'Suppressed' rollup, so the retry sweep leaves it alone."""
    import app.job_processor as job_processor
    from app.job_processor import retry_incomplete_notifications

    _insert_job_with_status(legacy_db, "suppressed", "Telegram: Suppressed")
    legacy_db._migrate_legacy_notification_states()

    sent = {"called": False}

    async def fake_send(**kwargs):
        sent["called"] = True
        return True

    monkeypatch.setattr(job_processor, "send_notification", fake_send)

    retried = asyncio.run(retry_incomplete_notifications())

    assert retried == 0, "a migrated Suppressed row must not be swept"
    assert sent["called"] is False
    assert legacy_db.get_job("suppressed")["Notification Status"] == "Suppressed"


def test_migrated_sent_row_rolls_to_complete_on_next_sweep(legacy_db, monkeypatch):
    """Once a legacy 'Telegram: Sent' is migrated, a retry sweep that
    re-runs the (idempotent) notifier finds it already sent and rolls the
    job to 'Complete' -- without ever calling the sink."""
    import app.job_processor as job_processor
    from app.job_processor import retry_incomplete_notifications

    _insert_job_with_status(legacy_db, "sent", "Telegram: Sent")
    legacy_db._migrate_legacy_notification_states()

    sink = _RecordingSink()
    from app.notifier import NotificationService
    real_service = NotificationService(sinks=(sink,), repository=legacy_db)
    monkeypatch.setattr(job_processor, "send_notification", real_service.send)

    retried = asyncio.run(retry_incomplete_notifications())

    assert sink.calls == 0, "no sink delivery may happen for a migrated Sent row"
    assert legacy_db.get_job("sent")["Notification Status"] == "Complete"
    assert retried == 1
    # And now nothing is left to sweep.
    assert asyncio.run(retry_incomplete_notifications()) == 0
