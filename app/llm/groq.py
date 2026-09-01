from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GROQ_API_KEY
from app.llm import rate_limit_tracker
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response


CLIENT = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
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


def _record_failure(model: str, error: Exception):
    """Skip a model that's known-bad on the *next* call instead of
    re-paying the request (and, for transient errors, tenacity's
    retry-and-wait) on every single job that needs Groq fallback for
    the rest of the cooldown window. See app.llm.rate_limit_tracker.

    Three-way classification, in order:
      1. Quota exhaustion (429-shaped, per-minute or per-day) --
         genuinely can't succeed again on this model until its quota
         window clears, so it's marked in the cooldown tracker.
      2. Transient infra overload (503-shaped, "the model is
         overloaded") -- says nothing about whether this model will
         still be bad a moment later, so it is NOT marked at all;
         already retried once within this call via tenacity, and the
         next job's call will simply try it fresh.
      3. Anything else (e.g. an unrecognized/decommissioned model
         name) -- can't self-resolve by waiting, so it gets a long
         cooldown until a human fixes the config.
    """
    error_text = str(error)
    candidate_id = f"groq-model-{model}"

    if rate_limit_tracker.is_quota_exhaustion(error_text):
        cooldown = rate_limit_tracker.mark_rate_limited(candidate_id, error_text)
        print(
            f"Groq model '{model}' failed: {error}\n"
            f"Marking '{model}' unavailable for {cooldown:.0f}s "
            f"before it's tried again."
        )
    elif _is_transient(error):
        # Overload/timeout -- momentary, not informative about this
        # model's own state, so it is deliberately left untouched in
        # the cooldown tracker rather than marked either way.
        print(
            f"Groq model '{model}' failed (transient, not quota-"
            f"related, not marked unavailable): {error}"
        )
    else:
        rate_limit_tracker.mark_permanently_broken(candidate_id)
        print(
            f"Groq model '{model}' failed: {error}\n"
            f"This does not look like a rate limit or transient "
            f"overload -- marking '{model}' unavailable for a while "
            f"rather than retrying it on every future job."
        )


def _available_models():
    all_ids = [f"groq-model-{model}" for model in GROQ_MODELS]
    available_ids = set(rate_limit_tracker.filter_available(all_ids))
    skipped = len(all_ids) - len(available_ids)
    if skipped:
        print(
            f"Skipping {skipped} Groq model(s) still in cooldown "
            f"from a recent failure."
        )
    return [
        model
        for model, candidate_id in zip(GROQ_MODELS, all_ids)
        if candidate_id in available_ids
    ]


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None):

    if system_prompt is None:
        from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT
    prompt = build_prompt(text, filter_result)

    failures = []
    last_exception = None

    for model in _available_models():

        print(f"Using Groq model: {model}")

        try:

            response = _generate_response(model, prompt, system_prompt)

            rate_limit_tracker.mark_success(f"groq-model-{model}")
            return parse_response(
                response.choices[0].message.content
            )

        except Exception as e:

            _record_failure(model, e)

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

    for model in _available_models():
        print(f"Using Groq model for category arbitration: {model}")
        try:
            response = _generate_response(model, prompt, system_prompt)
            rate_limit_tracker.mark_success(f"groq-model-{model}")
            return parse_arbitration_response(
                response.choices[0].message.content,
                allowed,
            )
        except Exception as e:
            _record_failure(model, e)
            failures.append(f"{model}: {e}")
            last_exception = e
            continue

    if failures:
        raise RuntimeError(
            "All Groq models failed for category arbitration. "
            + " | ".join(failures)
        ) from last_exception
    raise RuntimeError("No Groq models are configured.")
