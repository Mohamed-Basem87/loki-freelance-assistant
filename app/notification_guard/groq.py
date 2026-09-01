from groq import Groq

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_fixed,
)

from app.llm import rate_limit_tracker
from app.notification_guard.config import (
    NOTIFICATION_GUARD_API_KEYS,
    NOTIFICATION_GUARD_MODELS,
    NOTIFICATION_GUARD_MAX_RETRIES,
)
from app.notification_guard.prompt import build_prompt


CLIENTS = [
    Groq(api_key=key)
    for key in NOTIFICATION_GUARD_API_KEYS
]


_TRANSIENT_ERROR_MARKERS = (
    "429",
    "503",
    "resource_exhausted",
    "quota exceeded",
    "unavailable",
    "timeout",
    "timed out",
)


def _is_transient(exception: Exception) -> bool:
    text = str(exception).lower()

    return any(
        marker in text
        for marker in _TRANSIENT_ERROR_MARKERS
    )


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(NOTIFICATION_GUARD_MAX_RETRIES),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(
    client: Groq,
    model: str,
    title: str,
    description: str,
    system_prompt: str,
):
    return client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                # Single source of truth for the guard's user-turn
                # prompt structure (including the untrusted-content
                # framing) -- see app.notification_guard.prompt.
                "content": build_prompt(title, description),
            },
        ],
        response_format={
            "type": "json_object",
        },
    )


class GroqNotificationGuard:

    def __init__(self):
        self.models = list(NOTIFICATION_GUARD_MODELS)
        self.clients = list(CLIENTS)

        # Kept for compatibility with the existing guard logger.
        self.model = self.models[0] if self.models else ""

    def _candidates(self):
        """(client_index, client, model) triples to try, in the usual
        key-major/model-minor order, with any candidate still in a
        rate-limit or permanent-failure cooldown (see
        app.llm.rate_limit_tracker) skipped -- unless *every*
        candidate is currently in cooldown, in which case the full,
        unfiltered list is returned (see filter_available's own
        docstring for why: attempting a call that might still fail
        beats hard-refusing to try anything over a cooldown estimate
        that could simply be wrong).

        Skipping matters a lot here specifically: this rotation is
        the notification guard, called once per keyword-direct-match
        job, every single one -- an exhausted daily-token-budget model
        (as opposed to a per-request rate limit) fails identically for
        every remaining job that day, so re-attempting it from
        scratch each time is pure wasted latency on a request that
        cannot succeed until the provider's quota window resets.
        """
        all_candidates = [
            (client_index, client, model)
            for client_index, client in enumerate(self.clients)
            for model in self.models
        ]
        all_ids = [
            f"groq-guard-key{client_index + 1}-{model}"
            for client_index, _, model in all_candidates
        ]
        available_ids = set(rate_limit_tracker.filter_available(all_ids))

        skipped = len(all_ids) - len(available_ids)
        if skipped:
            print(
                f"Skipping {skipped} Groq guard key/model combination(s) "
                f"still in cooldown from a recent failure."
            )

        return [
            (client_index, client, model)
            for (client_index, client, model), candidate_id in zip(
                all_candidates, all_ids
            )
            if candidate_id in available_ids
        ]

    @staticmethod
    def _record_failure(candidate_id: str, client_index: int, model: str, error: Exception):
        """See app.llm.groq._record_failure for the full reasoning --
        identical three-way classification (quota exhaustion / momentary
        overload / permanent), applied here to the guard's key+model
        rotation instead of the main pipeline's model-only rotation.
        """
        error_text = str(error)

        if rate_limit_tracker.is_quota_exhaustion(error_text):
            cooldown = rate_limit_tracker.mark_rate_limited(
                candidate_id, error_text
            )
            print(
                f"Groq guard key #{client_index + 1}, model '{model}' "
                f"failed: {error}\n"
                f"Marking it unavailable for {cooldown:.0f}s before "
                f"it's tried again."
            )
        elif _is_transient(error):
            # Overload/timeout -- momentary, not informative about
            # this key/model's own state, so it is deliberately left
            # untouched in the cooldown tracker rather than marked.
            print(
                f"Groq guard key #{client_index + 1}, model '{model}' "
                f"failed (transient, not quota-related, not marked "
                f"unavailable): {error}"
            )
        else:
            # Not a rate limit or an overload -- e.g. an unrecognized
            # model name (404 model_not_found) -- so waiting and
            # retrying it on every future call would fail identically
            # forever until the config itself is fixed.
            rate_limit_tracker.mark_permanently_broken(candidate_id)
            print(
                f"Groq guard key #{client_index + 1}, model '{model}' "
                f"failed: {error}\n"
                f"This does not look like a rate limit or transient "
                f"overload -- marking it unavailable for a while "
                f"rather than retrying it on every future job."
            )

    def evaluate(
        self,
        title: str,
        description: str,
        system_prompt: str,
    ) -> bool:

        last_exception = None

        for client_index, client, model in self._candidates():

            candidate_id = f"groq-guard-key{client_index + 1}-{model}"

            print(
                f"Using Groq guard key #{client_index + 1}, model: {model}"
            )

            try:

                response = _generate_response(
                    client,
                    model,
                    title,
                    description,
                    system_prompt,
                )

                content = (
                    response
                    .choices[0]
                    .message
                    .content
                )

                decision = self._parse_decision(
                    content
                )

                rate_limit_tracker.mark_success(candidate_id)

                # A valid decision is final.
                # Do NOT rotate models after a valid
                # notify/do_not_notify response.
                return decision

            except Exception as e:

                self._record_failure(candidate_id, client_index, model, e)

                last_exception = e

                # Continue to the next model regardless
                # of failure type, matching the main
                # project's Groq behavior.

                continue

        if last_exception:
            raise last_exception

        raise RuntimeError(
            "No Groq notification guard models are configured."
        )

    @staticmethod
    def _parse_decision(content: str) -> bool:
        import json

        data = json.loads(content)

        decision = data.get("decision")

        if decision == "notify":
            return True

        if decision == "do_not_notify":
            return False

        raise ValueError(
            f"Invalid guard decision: {decision!r}"
        )

    def evaluate_with_category(
        self,
        title: str,
        description: str,
        system_prompt: str,
        original_category_id: str,
    ) -> tuple[bool, str]:
        """
        Like evaluate(), but the guard is also allowed to say the job
        is better classified as "full_stack" than the tiering
        system's original single-category match. `system_prompt` here
        is the combined prompt built by app.notification_guard.guard
        (original category's guard_prompt.py + full_stack's), which
        instructs the model to return {"decision": ..., "category":
        ...} instead of just {"decision": ...}.

        Returns (allowed, resolved_category_id). resolved_category_id
        is always either original_category_id or "full_stack" --
        never blindly trusted from the response, since a malformed or
        adversarial value here would otherwise let an untrusted job
        posting redirect delivery to a category the tiering system
        never matched (see the same defense-in-depth pattern in
        app.llm.utils.parse_arbitration_response).
        """

        last_exception = None

        for client_index, client, model in self._candidates():

            candidate_id = f"groq-guard-key{client_index + 1}-{model}"

            print(
                f"Using Groq guard key #{client_index + 1}, model: {model}"
            )

            try:

                response = _generate_response(
                    client,
                    model,
                    title,
                    description,
                    system_prompt,
                )

                content = (
                    response
                    .choices[0]
                    .message
                    .content
                )

                rate_limit_tracker.mark_success(candidate_id)

                return self._parse_decision_with_category(
                    content,
                    original_category_id,
                )

            except Exception as e:

                self._record_failure(candidate_id, client_index, model, e)

                last_exception = e

                continue

        if last_exception:
            raise last_exception

        raise RuntimeError(
            "No Groq notification guard models are configured."
        )

    @staticmethod
    def _parse_decision_with_category(
        content: str,
        original_category_id: str,
    ) -> tuple[bool, str]:
        import json

        data = json.loads(content)

        decision = data.get("decision")
        category = data.get("category")

        if decision not in ("notify", "do_not_notify"):
            raise ValueError(f"Invalid guard decision: {decision!r}")

        if decision == "do_not_notify":
            # The category field is meaningless for a suppressed
            # notification -- nothing is delivered under it either
            # way -- so it isn't validated here.
            return False, original_category_id

        if category not in (original_category_id, "full_stack"):
            raise ValueError(f"Invalid guard category: {category!r}")

        return True, category
