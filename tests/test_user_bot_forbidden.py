"""Regression tests for the Forbidden (user blocked the bot) handling fix.

When Telegram reports Forbidden, the destination is unreachable. The fix
deactivates the user AND cancels the user's entire not-yet-delivered backlog
(the same discard-on-unsubscribe semantics /stop uses) rather than leaving it
in a re-claimable "Failed"/"Pending" state that would all come back the moment
the user sends /start again.

Unit-testing _send_one directly by stubbing its module dependencies keeps the
assertions precise about what the Forbidden branch must call and with what
arguments.
"""
import asyncio

import pytest
from telegram.error import Forbidden

import app.user_bot as user_bot


class _Notification(dict):
    pass


def _notification():
    return _Notification(
        {
            "Notification ID": "notif-forbidden",
            "Telegram User ID": 999000,
            "Job UUID": "job-1",
            "Category ID": "data_analysis",
            "Attempts": "0",
        }
    )


def _stub_deps(monkeypatch, *, raises=Forbidden("blocked")):
    state = {
        "deactivated": None,
        "cancelled_for": None,
        "updated": [],
    }

    class _Logger:
        async def get_job(self, job_uuid):
            return {"Title": "SQL Dashboard", "job_uuid": job_uuid}

        async def set_destination_active(self, user_id, active, save=True):
            state["deactivated"] = (user_id, active)

        async def cancel_pending_user_notifications(self, user_id, save=True):
            state["cancelled_for"] = user_id

        async def update_user_notification(
            self, notification_id, status, attempts=None, last_error="", next_attempt_at=None, save=True
        ):
            state["updated"].append(
                (notification_id, status, attempts, last_error, next_attempt_at)
            )

    async def notify_user(user_id, payload):
        if raises is not None:
            raise raises

    monkeypatch.setattr(user_bot, "logger", _Logger())
    monkeypatch.setattr(
        user_bot, "user_renderer", type("R", (), {"render_user": lambda self, job, cat: {"text": "T", "button_url": None}})()
    )
    monkeypatch.setattr(
        user_bot, "user_messaging", type("M", (), {"notify_user": staticmethod(notify_user)})()
    )
    return state


def test_forbidden_deactivates_destination_and_cancels_recipient(monkeypatch):
    state = _stub_deps(monkeypatch)
    notification = _notification()

    asyncio.run(user_bot._send_one(notification))

    assert state["deactivated"] == (999000, False), (
        "a Forbidden (blocked bot) must deactivate the destination so no "
        "further notifications are queued for this user"
    )
    assert state["cancelled_for"] == 999000, (
        "the user's entire not-yet-delivered backlog must be cancelled so it "
        "cannot be re-claimed after a later /start"
    )
    # The current row is marked Cancelled terminally, with attempts untouched.
    notification_id, status, attempts, last_error, next_attempt = state["updated"][0]
    assert status == "Cancelled"
    assert attempts == "0"
    assert next_attempt is None


def test_forbidden_cancelled_rows_are_not_reclaimable_after_reactivation(monkeypatch):
    state = _stub_deps(monkeypatch)
    asyncio.run(user_bot._send_one(_notification()))

    # The Forbidden branch ends the current row in the terminal "Cancelled"
    # status, which is not in the claim set (Pending/Failed/RateLimited), so
    # even after Is Active flips back to 1 on /start the row is never
    # re-claimed and delivered late.
    status = state["updated"][0][1]
    assert status == "Cancelled"
    assert status not in ("Pending", "Failed", "RateLimited"), (
        "a Cancelled row must not re-enter the claim set after reactivation"
    )


def test_forbidden_on_a_failed_retry_still_cancels_without_spending_more_attempts(monkeypatch):
    state = _stub_deps(monkeypatch)
    notification = _notification()
    notification["Attempts"] = "3"

    asyncio.run(user_bot._send_one(notification))

    notification_id, status, attempts, last_error, next_attempt = state["updated"][0]
    assert status == "Cancelled"
    # The attempt count is preserved, not incremented: Cancelled is a
    # deliberate discard, not a delivery failure consuming the retry budget.
    assert attempts == "3"
