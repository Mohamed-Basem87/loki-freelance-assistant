import asyncio

from app.notification_guard.integration import (
    NotificationGuardIntegration,
)


class FakeGuard:

    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = 0
        self.jobs = []

    async def allow(self, job, *, original_decision=""):
        self.calls += 1
        self.jobs.append({
            "job": job,
            "original_decision": original_decision,
        })
        return self.allowed


async def fake_private(**kwargs):
    return True


async def fake_channel(**kwargs):
    return True


async def run_guard_test(
    allowed,
    ai_used=False,
):

    guard = FakeGuard(allowed)

    integration = NotificationGuardIntegration(guard)

    private = integration.wrap_private(fake_private)
    channel = integration.wrap_channel(fake_channel)

    kwargs = {
        "job_uuid": "job-1",
        "title": "Power BI dashboard",
        "description": "Build a sales dashboard from Excel data.",
        "source": "test",
        "decision": "Accepted",
        "ai_used": ai_used,
    }

    private_result = await private(**kwargs)
    channel_result = await channel(**kwargs)

    return (
        guard,
        private_result,
        channel_result,
    )


def test_direct_notification_allowed():

    guard, private, channel = asyncio.run(
        run_guard_test(True)
    )

    assert guard.calls == 1
    assert private is True
    assert channel is True

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"
    assert guard.jobs[0]["original_decision"] == "Accepted"


def test_direct_notification_rejected():

    guard, private, channel = asyncio.run(
        run_guard_test(False)
    )

    assert guard.calls == 1
    assert private is False
    assert channel is False

    assert guard.jobs[0]["job"]["job_uuid"] == "job-1"


def test_llm_review_bypasses_guard():

    guard, private, channel = asyncio.run(
        run_guard_test(
            False,
            ai_used=True,
        )
    )

    assert guard.calls == 0
    assert private is True
    assert channel is True
