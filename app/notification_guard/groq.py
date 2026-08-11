import json

from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.notification_guard.config import (
    NOTIFICATION_GUARD_API_KEY,
    NOTIFICATION_GUARD_MAX_RETRIES,
    NOTIFICATION_GUARD_MODEL,
)
from app.notification_guard.prompt import SYSTEM_PROMPT, build_prompt


_TRANSIENT_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "resource_exhausted",
    "rate limit",
    "quota exceeded",
    "unavailable",
    "timeout",
    "timed out",
)


def _is_transient(exception: Exception) -> bool:
    text = str(exception).lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


class GroqNotificationGuard:

    def __init__(self):
        if not NOTIFICATION_GUARD_API_KEY:
            raise RuntimeError(
                "GROQ_NOTIFICATION_GUARD_API_KEY is required "
                "when the notification guard is enabled."
            )

        self.client = Groq(api_key=NOTIFICATION_GUARD_API_KEY)
        self.model = NOTIFICATION_GUARD_MODEL

    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(
            max(1, NOTIFICATION_GUARD_MAX_RETRIES)
        ),
        wait=wait_fixed(1),
        reraise=True,
    )
    def _generate(self, prompt: str):

        return self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={"type": "json_object"},
        )

    def evaluate(self, title: str, description: str) -> bool:

        response = self._generate(
            build_prompt(title, description)
        )

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError(
                "Notification guard returned an empty response."
            )

        payload = json.loads(content)

        if set(payload) != {"decision"}:
            raise RuntimeError(
                "Notification guard returned an invalid schema."
            )

        if payload["decision"] not in {
            "notify",
            "do_not_notify",
        }:
            raise RuntimeError(
                "Notification guard returned an invalid decision."
            )

        return payload["decision"] == "notify"
