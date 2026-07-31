from google import genai
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GEMINI_API_KEYS
from app.llm.prompt import SYSTEM_PROMPT
from app.llm.utils import build_prompt, parse_response


CLIENTS = [
    genai.Client(api_key=key)
    for key in GEMINI_API_KEYS
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
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


@retry(
    # Only retry the *same* key/request for errors that plausibly
    # succeed on a second attempt (rate limit, transient
    # unavailability). A malformed request, auth failure, or a
    # response-parsing error will never succeed by just waiting a
    # second and asking again -- retrying those only adds latency
    # before we (correctly) move on to the next key.
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(client: genai.Client, contents: str):
    return client.models.generate_content(
        model="gemini-3.5-flash",
        contents=contents,
    )


def evaluate_job(text: str, filter_result: dict):

    prompt = build_prompt(text, filter_result)

    last_exception = None

    for index, client in enumerate(CLIENTS, start=1):

        print(f"Using Gemini API key #{index}")

        try:

            response = _generate_response(
                client,
                SYSTEM_PROMPT + "\n\n" + prompt,
            )

            return parse_response(response.text)

        except Exception as e:

            last_exception = e

            # Always try the remaining keys, regardless of *why* this
            # one failed -- a malformed/unparseable response from key
            # #1 says nothing about whether key #2 would work, so
            # there's no good reason to give up on the whole provider
            # over it. We only stop early once every key/model has
            # been tried (see the loop ending below), same as Groq.
            print(f"Gemini key #{index} failed: {e}")
            print("Trying next key..." if index < len(CLIENTS) else "No more Gemini keys.")
            continue

    if last_exception:
        raise last_exception

    raise RuntimeError("No Gemini API keys are configured.")
