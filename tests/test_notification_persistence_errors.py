"""Regression tests for audit finding: notification persistence errors.

NotificationService.send() previously caught both the read of prior
notification state and the write of new per-sink state with a bare
`except Exception: pass`. If a sink genuinely sent a notification but
the write recording that success failed, the failure was invisible:
send() still reported success for that sink, and the next retry sweep
-- seeing no durable record of it -- would resend to a sink that had
already been notified.
"""
import asyncio

import pytest

from app.notifier import NotificationService


class _AlwaysSucceedsSink:
    id = "sink_a"

    async def send(self, **kwargs):
        return True


class _FailingRepository:
    """A repository whose reads work but whose writes always fail,
    simulating a transient DB/disk problem at the moment success needs
    to be persisted."""

    def __init__(self):
        self.update_calls = 0

    def get_job(self, job_uuid):
        return {"Notification Status": ""}

    def update_job(self, job_uuid, notification_status=None, save=True):
        self.update_calls += 1
        raise RuntimeError("disk full")

    def log_error(self, *args, **kwargs):
        return None


class _UnreadableRepository:
    def get_job(self, job_uuid):
        raise RuntimeError("connection reset")

    def update_job(self, *args, **kwargs):
        raise AssertionError("must not attempt to write without a valid read")

    def log_error(self, *args, **kwargs):
        return None


class _FalseReturningRepository:
    """A repository whose update_job() reports failure by returning
    False rather than raising -- e.g. because the job row could not be
    found/persisted. Before the F-2 fix, NotificationService.send()
    only treated an *exception* from update_job() as a persistence
    failure; a plain `False` return was never inspected, so the sink
    was reported as durably "Sent" even though nothing was actually
    written."""

    def __init__(self):
        self.update_calls = 0

    def get_job(self, job_uuid):
        return {"Notification Status": ""}

    def update_job(self, job_uuid, notification_status=None, save=True):
        self.update_calls += 1
        return False

    def log_error(self, *args, **kwargs):
        return None


def test_send_raises_instead_of_silently_reporting_success_when_persist_fails():
    repository = _FailingRepository()
    service = NotificationService(sinks=[_AlwaysSucceedsSink()], repository=repository)

    with pytest.raises(RuntimeError, match="failed to persist notification state"):
        asyncio.run(service.send(job_uuid="job-1"))

    assert repository.update_calls == 1, (
        "the persist attempt must actually have been made, not skipped"
    )


def test_send_raises_instead_of_guessing_prior_state_when_read_fails():
    repository = _UnreadableRepository()
    service = NotificationService(sinks=[_AlwaysSucceedsSink()], repository=repository)

    with pytest.raises(RuntimeError, match="connection reset"):
        asyncio.run(service.send(job_uuid="job-2"))


def test_send_raises_instead_of_silently_reporting_success_when_update_job_returns_false():
    """Regression test for F-2: update_job() returning False must be
    treated exactly like an exception -- the sink must never be
    considered durably sent when its state was never actually
    persisted."""
    repository = _FalseReturningRepository()
    service = NotificationService(sinks=[_AlwaysSucceedsSink()], repository=repository)

    with pytest.raises(RuntimeError, match="failed to persist notification state"):
        asyncio.run(service.send(job_uuid="job-3"))

    assert repository.update_calls == 1, (
        "the persist attempt must actually have been made, not skipped"
    )
