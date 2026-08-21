from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GROQ_API_KEY
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response


CLIENT = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "qwen/qwen3.6-27b",
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
    # See app.llm.gemini for why this only retries transient errors.
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(model: str, prompt: str, system_prompt: str):

    return CLIENT.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        response_format={"type": "json_object"},
    )


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None):

    if system_prompt is None:
        from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT
    prompt = build_prompt(text, filter_result)

    failures = []
    last_exception = None

    for model in GROQ_MODELS:

        print(f"Using Groq model: {model}")

        try:

            response = _generate_response(model, prompt, system_prompt)

            return parse_response(
                response.choices[0].message.content
            )

        except Exception as e:

            print(f"Groq model '{model}' failed: {e}")

            failures.append(f"{model}: {e}")
            last_exception = e

            continue

    if failures:
        # Persist every model's failure -- the surviving error is what
        # job_processor logs to the errors table, and per-model detail
        # is the only way to diagnose an outage after the fact (the old
        # code kept only the last model's error, hiding the rest).
        raise RuntimeError(
            "All Groq models failed. " + " | ".join(failures)
        ) from last_exception

    raise RuntimeError("No Groq models are configured.")

def evaluate_category_arbitration(
    text: str,
    candidates: list[dict],
    system_prompt: str,
):
    prompt = build_arbitration_prompt(text, candidates)
    allowed = {item["id"] for item in candidates}
    failures = []
    last_exception = None

    for model in GROQ_MODELS:
        print(f"Using Groq model for category arbitration: {model}")
        try:
            response = _generate_response(model, prompt, system_prompt)
            return parse_arbitration_response(
                response.choices[0].message.content,
                allowed,
            )
        except Exception as e:
            print(f"Groq arbitration model '{model}' failed: {e}")
            failures.append(f"{model}: {e}")
            last_exception = e
            continue

    if failures:
        raise RuntimeError(
            "All Groq models failed for category arbitration. "
            + " | ".join(failures)
        ) from last_exception
    raise RuntimeError("No Groq models are configured.")
