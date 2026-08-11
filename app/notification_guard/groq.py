from groq import Groq

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_fixed,
)

from app.notification_guard.config import (
    NOTIFICATION_GUARD_API_KEY,
    NOTIFICATION_GUARD_MODELS,
    NOTIFICATION_GUARD_MAX_RETRIES,
)
from app.notification_guard.prompt import SYSTEM_PROMPT


CLIENT = Groq(
    api_key=NOTIFICATION_GUARD_API_KEY,
)


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
    model: str,
    title: str,
    description: str,
):
    return CLIENT.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    f"TITLE:\n{title}\n\n"
                    f"DESCRIPTION:\n{description}"
                ),
            },
        ],
        response_format={
            "type": "json_object",
        },
    )


class GroqNotificationGuard:

    def __init__(self):
        self.models = list(NOTIFICATION_GUARD_MODELS)

        # Kept for compatibility with the existing guard logger.
        self.model = self.models[0] if self.models else ""

    def evaluate(
        self,
        title: str,
        description: str,
    ) -> bool:

        last_exception = None

        for model in self.models:

            print(
                f"Using Groq guard model: {model}"
            )

            try:

                response = _generate_response(
                    model,
                    title,
                    description,
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
                    f"Groq guard model '{model}' failed: {e}"
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
