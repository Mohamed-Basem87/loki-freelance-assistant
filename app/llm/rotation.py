"""
Shared "try each candidate in order, skip cooled-down ones, classify
failures the same way" executor.

Before this module existed, Gemini's key rotation, Groq's model
rotation, and the notification guard's key x model rotation each
hand-rolled their own copy of this exact loop -- same four-way failure
classification (truncated response / quota exhaustion / transient
overload / everything else), same cooldown-skip logic, same success
handling, duplicated three times with only cosmetic differences. This
is that loop, written once. Every current and future LLMProvider (see
app.llm.provider) is expected to build its own list of candidates and
call run_with_rotation instead of writing its own version of this.
"""

from app.llm import rate_limit_tracker


def run_with_rotation(provider_label, candidates, mark_unknown_as_permanent=True):
    """Try each candidate in order until one succeeds.

    provider_label: a short display name for log lines, e.g. "Gemini",
        "Groq", "Groq guard".

    candidates: list of (candidate_id, display_label, thunk) tuples,
        tried in the given order.
          - candidate_id: stable string used as the rate_limit_tracker
            key (e.g. "gemini-key1", "groq-model-openai/gpt-oss-120b").
            Must be unique per candidate for the lifetime of the
            process; see app.llm.rate_limit_tracker's own docstring
            for why it's positional/model-derived rather than a raw
            secret.
          - display_label: human-readable text for log lines (e.g.
            "key #1", "model: openai/gpt-oss-120b").
          - thunk: a zero-argument callable that performs the actual
            request AND parses its response, returning the final
            result. Raise on any failure -- including a parse failure
            -- rather than returning a partial/invalid result.

    mark_unknown_as_permanent: whether a failure that's neither a
        truncation, quota exhaustion, nor a transient overload should
        be treated as permanently broken (Groq's historical choice --
        a dead/decommissioned model name can't self-resolve) or left
        unmarked (Gemini's historical, deliberately conservative
        choice -- an ambiguous one-off failure there, e.g. a
        malformed response, says nothing certain enough about the key
        itself to justify costing it 6 hours of availability). Kept
        as a per-provider flag specifically to preserve that
        established difference across this refactor rather than
        silently unifying two intentionally different judgment calls.

    Returns (result, display_label_of_the_winning_candidate) on
    success. On success, mark_success is called for that specific
    candidate_id (see rate_limit_tracker.mark_success for why: it
    resets that candidate's daily-quota escalation, so a candidate
    that failed several times isn't still penalized once it's
    actually working again).

    Raises RuntimeError, with every candidate's failure message
    joined together, if every available candidate fails (or none were
    configured at all).
    """
    all_ids = [candidate_id for candidate_id, _, _ in candidates]
    available_ids = set(rate_limit_tracker.filter_available(all_ids))
    skipped = len(all_ids) - len(available_ids)
    if skipped:
        print(
            f"Skipping {skipped} {provider_label} candidate(s) still in "
            f"cooldown from a recent failure."
        )

    failures = []
    last_exception = None

    for candidate_id, display_label, thunk in candidates:

        if candidate_id not in available_ids:
            continue

        print(f"Using {provider_label} {display_label}")

        try:
            result = thunk()
            rate_limit_tracker.mark_success(candidate_id)
            return result, display_label

        except Exception as e:

            failures.append(f"{display_label}: {e}")
            last_exception = e
            error_text = str(e)

            if isinstance(e, rate_limit_tracker.TruncatedResponseError):
                print(
                    f"{provider_label} {display_label} response was cut "
                    f"off by its output-token cap before finishing its "
                    f"JSON: {e}\n"
                    f"Not marking {display_label} unavailable -- this is "
                    f"a per-request response-length issue, not a problem "
                    f"with the candidate itself. If this keeps "
                    f"happening, the cap for this call site may need "
                    f"raising."
                )

            elif rate_limit_tracker.is_quota_exhaustion(error_text):
                cooldown = rate_limit_tracker.mark_rate_limited(
                    candidate_id, error_text
                )
                print(
                    f"{provider_label} {display_label} failed: {e}\n"
                    f"Marking {display_label} unavailable for "
                    f"{cooldown:.0f}s before it's tried again."
                )

            elif rate_limit_tracker.is_transient(e):
                # Momentary overload -- not informative about this
                # candidate's own state, so it is deliberately left
                # untouched in the cooldown tracker rather than marked
                # either way. Already retried once within this call
                # via tenacity; the next job's call simply tries it
                # fresh.
                print(
                    f"{provider_label} {display_label} failed "
                    f"(transient, not quota-related, not marked "
                    f"unavailable): {e}"
                )

            elif mark_unknown_as_permanent:
                rate_limit_tracker.mark_permanently_broken(candidate_id)
                print(
                    f"{provider_label} {display_label} failed: {e}\n"
                    f"This does not look like a rate limit, truncation, "
                    f"or transient overload -- marking {display_label} "
                    f"unavailable for a while rather than retrying it "
                    f"on every future job."
                )

            else:
                print(f"{provider_label} {display_label} failed: {e}")

            continue

    if failures:
        raise RuntimeError(
            f"All {provider_label} candidates failed. " + " | ".join(failures)
        ) from last_exception

    raise RuntimeError(f"No {provider_label} candidates are configured.")
