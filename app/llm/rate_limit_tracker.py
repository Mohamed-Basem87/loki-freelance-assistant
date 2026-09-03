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

# Applied when an external LLM request itself times out. A timeout is
# different from quota exhaustion: the candidate may recover soon, but
# should not be retried immediately on every job while the provider/network
# is unhealthy.
_TIMEOUT_COOLDOWN_SECONDS = 60

# Applied for failures that are not a quota/rate-limit at all and
# cannot resolve on their own (e.g. a model name the provider doesn't
# recognize, as seen in production logs for a stale
# NOTIFICATION_GUARD_MODELS entry) -- long enough that a human
# correcting the config, not a passing quota window, is what actually
# fixes it, without permanently wedging the candidate out of rotation
# forever if it's ever fixed while the process keeps running.
_PERMANENT_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours

# Gemini (confirmed against production logs) reports a short
# retryDelay -- typically 25-60s -- even when the failure is a
# GenerateRequestsPerDayPerProjectPerModel-FreeTier quota, i.e. a
# genuine once-per-day exhaustion, not a per-minute limit. Per-minute
# limits really do clear in ~45-60s and the reported delay is honest
# for those; per-day limits do not reset for up to 24 hours, and the
# short delay is not a reflection of that -- trusting it verbatim
# causes a retry-fail-retry loop every ~30-60s for hours, which is
# the exact wasted-request pattern this whole tracker exists to
# prevent. When a failure's error text carries this specific marker,
# an escalating cooldown (see _DAILY_QUOTA_BASE_COOLDOWN_SECONDS
# below) is applied instead of the parsed delay.
_DAILY_QUOTA_MARKER = "perday"

# The exact reset time for a "per day" quota isn't something this
# process can know for certain -- it may be a fixed calendar-day
# boundary or a rolling 24h window from first use, and can differ per
# provider/plan -- so rather than guess a single "right" wait and
# apply it flat all day (which either wastes requests if too short,
# as seen in production, or risks sitting idle too long if too long),
# each consecutive daily-quota failure for the *same* candidate
# doubles the previous wait, starting from this base and stopping at
# _DAILY_QUOTA_MAX_COOLDOWN_SECONDS. A candidate that's still
# exhausted keeps getting checked, just at a rapidly shrinking
# frequency -- roughly 30m, 1h, 2h, 4h, 4h, 4h... rather than a flat
# 30m retry for the entire rest of the day. The escalation resets to
# the base the moment this candidate succeeds again (see
# mark_success), so a new day's quota isn't penalized by yesterday's
# exhaustion.
_DAILY_QUOTA_BASE_COOLDOWN_SECONDS = 30 * 60
_DAILY_QUOTA_MAX_COOLDOWN_SECONDS = 4 * 60 * 60

# consecutive daily-quota failure count per candidate, used only to
# compute the escalating cooldown above. Cleared on success
# (mark_success) or on a non-daily-quota failure for that candidate
# (a per-minute limit or a transient overload says nothing about the
# daily window, so it shouldn't affect this count either way -- only
# genuinely resets on an actual success).
_daily_quota_failure_counts: dict[str, int] = {}

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


# Markers for genuine quota/rate-limit exhaustion -- confirmed against
# real Gemini and Groq error text: Gemini's RESOURCE_EXHAUSTED (429)
# with a QuotaFailure detail, and Groq's rate_limit_exceeded (429) for
# both RPM/TPM (per-minute) and RPD/TPD (per-day) budgets. A candidate
# that fails with one of these genuinely cannot succeed again until
# its specific quota window clears -- that's exactly the case this
# tracker exists to skip on future calls.
_QUOTA_EXHAUSTION_MARKERS = (
    "429",
    "resource_exhausted",
    "quota exceeded",
    "quotaid",
    "rate_limit_exceeded",
    "rate limit reached",
    "tokens per day",
    "requests per day",
    "tokens per minute",
    "requests per minute",
)

# Markers for a momentary infrastructure hiccup -- confirmed against
# real Gemini ("503 UNAVAILABLE... The model is overloaded") and Groq
# ("Groq infrastructure issue, model overloaded") error text. This is
# NOT a statement about this specific key/model's quota -- it says
# nothing about whether the next call, a few seconds later, will
# succeed -- so it must never be treated as quota exhaustion even if a
# message happens to also contain a coincidentally-matching word.
# Checked first and takes priority over the quota markers above.
_OVERLOAD_MARKERS = (
    "503",
    "overloaded",
    "service unavailable",
    "unavailable",
)


def is_quota_exhaustion(error_message: str) -> bool:
    """True only for a genuine quota/rate-limit exhaustion (429-shaped,
    per-minute or per-day) -- never for a transient infra overload
    (503-shaped), even though both are worth retrying within the same
    call (see each caller's own _is_transient/tenacity retry, which is
    intentionally broader than this). Only this function's result
    should ever gate a call to mark_rate_limited/filter_available --
    marking a candidate unavailable over a passing 503 would wrongly
    skip a key/model on the next call for a problem that most likely
    already resolved itself.
    """
    text = error_message.lower()
    if any(marker in text for marker in _OVERLOAD_MARKERS):
        return False
    return any(marker in text for marker in _QUOTA_EXHAUSTION_MARKERS)


def is_available(candidate_id: str) -> bool:
    with _lock:
        until = _cooldowns.get(candidate_id)
    return until is None or time.monotonic() >= until


def mark_rate_limited(candidate_id: str, error_message: str) -> float:
    """Record that `candidate_id` failed with a rate-limit/quota error.

    Returns the cooldown duration actually applied, mainly so callers
    can log it alongside the existing failure message.

    If the error text identifies this as a per-day quota exhaustion
    (see _DAILY_QUOTA_MARKER), the provider's own parsed retry delay
    is deliberately overridden -- that delay is confirmed unreliable
    for this specific quota type (see the constant's docstring) -- and
    an escalating backoff is applied instead (see
    _DAILY_QUOTA_BASE_COOLDOWN_SECONDS for why escalating rather than
    a single flat wait).
    """
    if _DAILY_QUOTA_MARKER in error_message.lower().replace(" ", "").replace("_", ""):
        with _lock:
            failure_count = _daily_quota_failure_counts.get(candidate_id, 0) + 1
            _daily_quota_failure_counts[candidate_id] = failure_count
            delay = min(
                _DAILY_QUOTA_BASE_COOLDOWN_SECONDS * (2 ** (failure_count - 1)),
                _DAILY_QUOTA_MAX_COOLDOWN_SECONDS,
            )
            _cooldowns[candidate_id] = time.monotonic() + delay
        return delay

    delay = _parse_retry_after_seconds(error_message)
    if delay is None:
        delay = _DEFAULT_COOLDOWN_SECONDS

    with _lock:
        _cooldowns[candidate_id] = time.monotonic() + delay

    return delay


def mark_timeout(candidate_id: str) -> float:
    """Temporarily skip a candidate after an API request timeout."""
    delay = _TIMEOUT_COOLDOWN_SECONDS
    with _lock:
        _cooldowns[candidate_id] = time.monotonic() + delay
    return delay


def mark_success(candidate_id: str):
    """Call when `candidate_id` succeeds, to reset its daily-quota
    escalation (see _daily_quota_failure_counts) back to the base
    cooldown -- otherwise a candidate that failed several times
    yesterday would start today already escalated, even though
    today's quota window has nothing to do with yesterday's.

    Safe to call unconditionally on every success, including
    candidates that were never marked in the first place (a no-op in
    that case).
    """
    with _lock:
        _daily_quota_failure_counts.pop(candidate_id, None)


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
        _daily_quota_failure_counts.clear()
