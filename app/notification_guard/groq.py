from groq import Groq

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_fixed,
)

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

    def evaluate(
        self,
        title: str,
        description: str,
        system_prompt: str,
    ) -> bool:

        last_exception = None

        for client_index, client in enumerate(self.clients):

            for model in self.models:

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

                    # A valid decision is final.
                    # Do NOT rotate models after a valid
                    # notify/do_not_notify response.
                    return decision

                except Exception as e:

                    print(
                        f"Groq guard key #{client_index + 1}, model '{model}' failed: {e}"
                    )

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

        for client_index, client in enumerate(self.clients):

            for model in self.models:

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

                    return self._parse_decision_with_category(
                        content,
                        original_category_id,
                    )

                except Exception as e:

                    print(
                        f"Groq guard key #{client_index + 1}, model '{model}' failed: {e}"
                    )

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
