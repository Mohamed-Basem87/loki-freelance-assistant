from google import genai
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import GEMINI_API_KEYS
from app.llm.prompt import SYSTEM_PROMPT
from app.llm.utils import build_prompt, parse_response


CLIENTS = [
    genai.Client(api_key=key)
    for key in GEMINI_API_KEYS
]


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(client: genai.Client, contents: str):
    return client.models.generate_content(
        model="gemini-3.5-flash",
        contents=contents,
    )


def _should_try_next_key(exception: Exception) -> bool:

    text = str(exception).lower()

    retryable_errors = (
        "429",
        "503",
        "resource_exhausted",
        "quota exceeded",
        "unavailable",
    )

    return any(error in text for error in retryable_errors)


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

            if _should_try_next_key(e):
                print(f"Gemini key #{index} failed: {e}")
                print("Trying next key...")
                continue

            raise

    if last_exception:
        raise last_exception

    raise RuntimeError("No Gemini API keys are configured.")