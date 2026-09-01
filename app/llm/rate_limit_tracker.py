"""
Shared, process-lifetime cooldown tracker for LLM provider keys/models.

Both app.llm.gemini and app.notification_guard.groq loop over several
(key[, model]) candidates on every call, retrying each one from
scratch even when a previous call *in the same process* already
learned that candidate is rate-limited/quota-exhausted until a known
future time. On a tight daily quota (Gemini's free tier: 20
requests/day/key/model; Groq's on-demand tier: a fixed daily token
budget per model) that means every arbitration/guard call after the
first exhaustion re-pays the same network round-trip -- and, for
errors app/llm/gemini.py's and app/notification_guard/groq.py's own
tenacity retry classifies as transient, an extra retry-and-wait on
top -- against a request that cannot possibly succeed until the quota
window resets. That cost repeats for every job needing arbitration or
guard review for the rest of the day, which is exactly the pattern
seen in production logs: the same exhausted key/model failing
identically dozens of times in a row before the rotation finally
reaches a candidate with quota left.

This module lets a caller mark a candidate "in cooldown until <time>"
after a rate-limit response, and skip it on subsequent calls until
that time passes. It is deliberately NOT persisted across restarts --
quota windows are provider-side state this process has no
authoritative insight into, so re-learning current state from a fresh
failure after a restart is safer than trusting a stale on-disk
cooldown that might already be wrong.
"""

import re
import threading
import time


_lock = threading.Lock()

# candidate_id -> unavailable-until, in time.monotonic() seconds.
# time.monotonic() (not wall-clock time) so this is immune to system
# clock adjustments during the process's lifetime.
_cooldowns: dict[str, float] = {}

# Applied when a failure is rate-limit-shaped (see the transient-error
# markers already used by app.llm.gemini/app.notification_guard.groq)
# but no provider-reported retry delay could be parsed out of the
# error message.
_DEFAULT_COOLDOWN_SECONDS = 60

# Applied for failures that are not a quota/rate-limit at all and
# cannot resolve on their own (e.g. a model name the provider doesn't
# recognize, as seen in production logs for a stale
# NOTIFICATION_GUARD_MODELS entry) -- long enough that a human
# correcting the config, not a passing quota window, is what actually
# fixes it, without permanently wedging the candidate out of rotation
# forever if it's ever fixed while the process keeps running.
_PERMANENT_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours

# Gemini: "...Please retry in 36.691225523s."
_GEMINI_RETRY_RE = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)

# Groq (daily token-budget errors): "...Please try again in 24m35.712s."
# The minutes group is optional -- shorter waits are reported as
# e.g. "8m13.344s" or plausibly just "45.2s" with no minutes at all.
_GROQ_RETRY_RE = re.compile(
    r"try again in (?:([\d.]+)m)?([\d.]+)s", re.IGNORECASE
)


def _parse_retry_after_seconds(error_message: str):
    """Best-effort extraction of a provider-reported retry delay.

    Returns None if neither known pattern matches -- callers fall
    back to _DEFAULT_COOLDOWN_SECONDS in that case rather than
    treating a parse miss as "no cooldown needed".
    """
    match = _GEMINI_RETRY_RE.search(error_message)
    if match:
        return float(match.group(1))

    match = _GROQ_RETRY_RE.search(error_message)
    if match:
        minutes = float(match.group(1)) if match.group(1) else 0.0
        seconds = float(match.group(2))
        return minutes * 60 + seconds

    return None


def is_available(candidate_id: str) -> bool:
    with _lock:
        until = _cooldowns.get(candidate_id)
    return until is None or time.monotonic() >= until


def mark_rate_limited(candidate_id: str, error_message: str) -> float:
    """Record that `candidate_id` failed with a rate-limit/quota error.

    Returns the cooldown duration actually applied (parsed from the
    provider's own message when possible), mainly so callers can log
    it alongside the existing failure message.
    """
    delay = _parse_retry_after_seconds(error_message)
    if delay is None:
        delay = _DEFAULT_COOLDOWN_SECONDS

    with _lock:
        _cooldowns[candidate_id] = time.monotonic() + delay

    return delay


def mark_permanently_broken(candidate_id: str) -> float:
    """Record a non-rate-limit failure that can't self-resolve on its
    own (e.g. an unrecognized model name). See module docstring for
    why this is a long cooldown rather than an indefinite one.
    """
    with _lock:
        _cooldowns[candidate_id] = time.monotonic() + _PERMANENT_COOLDOWN_SECONDS

    return _PERMANENT_COOLDOWN_SECONDS


def filter_available(candidate_ids: list) -> list:
    """Available candidates, preserving original order.

    If every candidate is currently in cooldown, returns the full
    original list unfiltered rather than an empty list -- we'd rather
    attempt a call that might still fail than raise "nothing
    available" purely because our own cooldown estimate is
    optimistic/stale (e.g. a default 60s guess when the real quota
    window resets sooner, or a quota that resets earlier than
    expected).
    """
    available = [c for c in candidate_ids if is_available(c)]
    return available if available else list(candidate_ids)


def clear():
    """Reset all tracked cooldowns. Exists for test isolation (see
    tests/conftest.py) -- not called anywhere in the running app.
    """
    with _lock:
        _cooldowns.clear()
