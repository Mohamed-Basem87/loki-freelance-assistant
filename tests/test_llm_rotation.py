"""
Regression tests for app.llm.rotation / app.llm.rate_limit_tracker's
cooldown reporting, the aggressive-rotation all-cooled-down fallback,
and the win correlation / cooldown-clear on success.

The aggressive rotation design (per explicit design decision) keeps
attempting configured candidates even when every one of them is
cooling down -- filter_available returns the full list rather than an
empty one -- so a job is never skipped merely because of a local
cooldown estimate. run_with_rotation therefore has no fast-fail "all
in cooldown" branch: it reports the situation in an informational log
line (using rate_limit_tracker.earliest_cooldown_expiry_seconds) and
attempts the call anyway. This file pins that whole contract down:
  1. the aggressive fallback must keep working end to end,
  2. the informational report must be emitted with the earliest
     cooldown expiry,
  3. a success must retire the winner's active cooldown (so the very
     next call may reuse it), and
  4. run_with_rotation must return the winning candidate_id so
     callers (e.g. the notification guard) can correlate the win back
     to the concrete model/key that produced it.
"""

from app.llm import rate_limit_tracker as tracker
from app.llm import rotation


def test_filter_available_keeps_all_candidates_when_all_cooled_down():
    """Aggressive-rotation fallback (explicit design decision): when
    every candidate is in cooldown, filter_available must return the
    full list so the call is still attempted -- never an empty one."""
    tracker.clear()
    tracker.mark_rate_limited("a", "rate_limit_exceeded: please try again in 45s")
    tracker.mark_rate_limited("b", "quota exceeded: retry in 10s")
    assert tracker.filter_available(["a", "b"]) == ["a", "b"]


def test_run_with_rotation_still_attempts_candidates_when_all_in_cooldown(capsys):
    """The aggressive fallback end to end: with every candidate cooling
    down, run_with_rotation must try them in order and return the first
    success -- it must NOT fast-fail a job on the basis of local
    cooldown estimates. It must also say so in the log line rather than
    silently attempting (the cooldown is a local guess, not
    authoritative provider state)."""
    tracker.clear()
    tracker.mark_rate_limited("a", "rate_limit_exceeded: please try again in 45s")
    tracker.mark_rate_limited("b", "quota exceeded: retry in 10s")

    calls = []

    def thunk_a():
        calls.append("a")
        raise RuntimeError("boom")

    def thunk_b():
        calls.append("b")
        return "ok"

    result, candidate_id, label = rotation.run_with_rotation(
        "test",
        [("a", "A", thunk_a), ("b", "B", thunk_b)],
        mark_unknown_as_permanent=False,
    )

    assert result == "ok"
    assert candidate_id == "b", "the winning candidate_id must be returned"
    assert label == "B"
    assert calls == ["a", "b"], (
        "both candidates must be attempted even when both are in "
        "cooldown (aggressive rotation)"
    )

    captured = capsys.readouterr().out
    assert "attempting anyway" in captured, (
        "the informational all-cooled-down note must be logged when the "
        "aggressive fallback lets an all-cooled call through"
    )
    assert "expected back" in captured


def test_run_with_rotation_reports_earliest_cooldown_expiry_when_all_cooled(capsys):
    """The informational log line must report how long until the
    earliest cooling candidate is expected back (a rough local
    estimate, documented as such, never used to gate the attempt)."""
    tracker.clear()
    tracker.mark_rate_limited("a", "rate_limit_exceeded: please try again in 45s")
    tracker.mark_rate_limited("b", "quota exceeded: retry in 10s")

    def thunk(which):
        def call():
            return "ok-" + which
        return call

    rotation.run_with_rotation(
        "test",
        [("a", "A", thunk("a")), ("b", "B", thunk("b"))],
        mark_unknown_as_permanent=False,
    )

    captured = capsys.readouterr().out
    assert "Earliest candidate is currently expected back in ~10s" in captured


def test_earliest_cooldown_expiry_seconds_reports_closest_expiry():
    """The helper run_with_rotation uses for the informational
    all-cooled-down log must exist and report the soonest cooldown
    expiry among the given ids (purely informational -- it never gates
    whether an attempt happens)."""
    tracker.clear()
    tracker.mark_rate_limited("a", "rate_limit_exceeded: please try again in 45s")
    tracker.mark_rate_limited("b", "quota exceeded: retry in 10s")

    earliest = tracker.earliest_cooldown_expiry_seconds(["a", "b"])
    assert earliest is not None
    assert 0 < earliest <= 10.5
    assert earliest > 5, (
        f"expected ~10s (the closer of the two cooldowns), got {earliest}"
    )

    # A candidate with no cooldown is not reported as "expiring".
    assert tracker.earliest_cooldown_expiry_seconds(["fresh"]) is None
    assert tracker.earliest_cooldown_expiry_seconds([]) is None


def test_mark_success_clears_active_cooldown():
    """Regression test: mark_success must clear the candidate's active
    cooldown in addition to its daily-quota escalation. Without that, a
    candidate the aggressive fallback just succeeded on (all candidates
    were cooling; the call went through anyway and the cooldown
    estimate turned out stale/wrong) would keep being skipped on the
    very next call even though it just proved it can succeed."""
    tracker.clear()
    tracker.mark_rate_limited("a", "rate_limit_exceeded: please try again in 45s")
    assert tracker.is_available("a") is False

    tracker.mark_success("a")
    assert tracker.is_available("a") is True, (
        "a success must retire the candidate's active cooldown"
    )


def test_mark_success_is_a_noop_for_unknown_candidate():
    tracker.clear()
    tracker.mark_success("never-seen-before")
    assert tracker.is_available("never-seen-before") is True


# ------------------------------------------------------------------
# P1/P2: Deadline-aware rotation (LLM outer-timeout / background thread fix)
#
# The outer timeout (asyncio.wait_for) cannot forcibly terminate the
# underlying thread created through asyncio.to_thread(). Therefore, the
# rotation layer receives a deadline (monotonic timestamp) and checks it
# before starting each NEW candidate attempt. In-flight provider calls
# are bounded by their own HTTP timeouts, not by this deadline.
# ------------------------------------------------------------------


def test_run_with_rotation_respects_deadline_before_new_attempt():
    """When a deadline is passed and has already passed, run_with_rotation
    must not start any new candidate attempts and must raise RuntimeError."""
    import time
    tracker.clear()

    # Deadline in the past
    deadline = time.monotonic() - 10

    calls = []

    def thunk_a():
        calls.append("a")
        return "ok-a"

    def thunk_b():
        calls.append("b")
        return "ok-b"

    try:
        rotation.run_with_rotation(
            "test",
            [("a", "A", thunk_a), ("b", "B", thunk_b)],
            deadline=deadline,
        )
        raise AssertionError("Expected RuntimeError when deadline is exceeded")
    except RuntimeError as e:
        assert "Deadline reached" in str(e)

    # No candidates should have been attempted
    assert calls == []


def test_run_with_rotation_allows_inflight_attempt_to_complete():
    """When deadline expires DURING a candidate's execution, that attempt
    should be allowed to complete (bounded by its own HTTP timeout).
    The deadline only prevents STARTING new attempts."""
    import time
    tracker.clear()

    # Deadline expires after a short delay
    deadline = time.monotonic() + 0.05

    calls = []
    results = []

    def thunk_a():
        calls.append("a_start")
        time.sleep(0.1)  # Longer than deadline
        calls.append("a_end")
        return "ok-a"

    def thunk_b():
        calls.append("b")
        return "ok-b"

    # First attempt runs past deadline, but should complete
    result, candidate_id, _ = rotation.run_with_rotation(
        "test",
        [("a", "A", thunk_a), ("b", "B", thunk_b)],
        deadline=deadline,
        mark_unknown_as_permanent=False,
    )

    assert result == "ok-a"
    assert candidate_id == "a"
    assert calls == ["a_start", "a_end"]


def test_run_with_rotation_stops_before_second_candidate_when_deadline_expired():
    """When first candidate fails and deadline expires before second
    candidate starts, rotation must stop and not attempt the second."""
    import time
    tracker.clear()

    # Deadline expires very quickly - use a deadline that's already
    # effectively in the past by the time we get to the second candidate
    # (the first candidate's failure processing takes some time)
    deadline = time.monotonic() + 0.001

    calls = []

    def thunk_a():
        calls.append("a")
        # Small delay to ensure deadline expires
        time.sleep(0.01)
        raise RuntimeError("fail")

    def thunk_b():
        calls.append("b")
        return "ok-b"

    try:
        rotation.run_with_rotation(
            "test",
            [("a", "A", thunk_a), ("b", "B", thunk_b)],
            deadline=deadline,
            mark_unknown_as_permanent=False,
        )
        raise AssertionError("Expected RuntimeError")
    except RuntimeError as e:
        assert "Deadline reached" in str(e)

    # First candidate attempted, second NOT started because deadline expired
    assert calls == ["a"]


def test_run_with_rotation_deadline_none_means_no_deadline():
    """When deadline=None (default), rotation behaves as before with no
    deadline checks."""
    tracker.clear()

    calls = []

    def thunk_a():
        calls.append("a")
        raise RuntimeError("fail")

    def thunk_b():
        calls.append("b")
        return "ok-b"

    result, candidate_id, _ = rotation.run_with_rotation(
        "test",
        [("a", "A", thunk_a), ("b", "B", thunk_b)],
        deadline=None,
        mark_unknown_as_permanent=False,
    )

    assert result == "ok-b"
    assert candidate_id == "b"
    assert calls == ["a", "b"]