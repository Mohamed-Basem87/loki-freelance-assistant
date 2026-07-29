from groq import Groq
from tenacity import retry, stop_after_attempt, wait_fixed

from app.config import GROQ_API_KEY
from app.llm.prompt import SYSTEM_PROMPT
from app.llm.utils import build_prompt, parse_response


CLIENT = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "qwen/qwen3.6-27b",
]


@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(model: str, prompt: str):

    return CLIENT.chat.completions.create(
        model=model,
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


def evaluate_job(text: str, filter_result: dict):

    prompt = build_prompt(text, filter_result)

    last_exception = None

    for model in GROQ_MODELS:

        print(f"Using Groq model: {model}")

        try:

            response = _generate_response(model, prompt)

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