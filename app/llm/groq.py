from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GROQ_API_KEY
from app.categories.registry import get_category
from app.llm.utils import build_prompt, parse_response


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
def _generate_response(model: str, prompt: str, profile):

    return CLIENT.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": profile.llm_prompt.SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        response_format={"type": "json_object"},
    )


def evaluate_job(text: str, filter_result: dict, category=None):

    if category is None:
        category = filter_result.get("category_id", "data_analysis")
    profile = get_category(category) if isinstance(category, str) else category
    prompt = build_prompt(text, filter_result)

    last_exception = None

    for model in GROQ_MODELS:

        print(f"Using Groq model: {model}")

        try:

            response = _generate_response(model, prompt, profile)

            return parse_response(
                response.choices[0].message.content
            )

        except Exception as e:

            print(f"Groq model '{model}' failed: {e}")

            last_exception = e

            continue

    if last_exception:
        raise last_exception

    raise RuntimeError("No Groq models are configured.")