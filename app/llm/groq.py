from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GROQ_API_KEY
from app.llm import rate_limit_tracker
from app.llm.provider import LLMProvider
from app.llm.rotation import run_with_rotation
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response


CLIENT = Groq(api_key=GROQ_API_KEY)

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# Re-exported from rate_limit_tracker for backward compatibility --
# existing code/tests may still refer to app.llm.groq.TruncatedResponseError.
TruncatedResponseError = rate_limit_tracker.TruncatedResponseError


def _raise_if_truncated(response):
    finish_reason = response.choices[0].finish_reason
    if finish_reason == "length":
        raise TruncatedResponseError(
            f"Completion was cut off by max_tokens before finishing "
            f"its JSON (finish_reason='length')."
        )


@retry(
    # See app.llm.gemini for why this only retries transient errors.
    retry=retry_if_exception(rate_limit_tracker.is_transient),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(model: str, prompt: str, system_prompt: str, max_tokens: int):
    """
    max_tokens is capped well below the point where Groq's own
    output-tokens-per-minute (OTPM) pre-flight check starts rejecting
    the request outright -- confirmed directly in production logs:
    'Request too large for model `qwen/qwen3.6-27b`... on output
    tokens per minute (OTPM): Limit 1000, Requested 1164', for a
    response that only ever needed a small JSON object. Without an
    explicit cap, Groq's rate limiter has to assume the *model's own*
    default maximum output length when deciding whether a request
    fits the remaining per-minute budget -- for a reasoning-capable
    model like qwen3.6-27b, that assumed ceiling is large even though
    the actual answer is tiny, so the request gets rejected before
    generation even starts. Explicitly bounding it here lets Groq see
    the true (small) upper bound instead of the model's worst case.

    The cap is sized per caller (see GroqProvider.evaluate_job/
    evaluate_category_arbitration below), not a single shared
    constant, since evaluate_job's REQUIRED_KEYS response is
    materially larger (two free-text fields plus a list) than
    arbitration's three-field schema and needs more headroom to avoid
    truncating a genuine response mid-JSON.
    """
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
        max_tokens=max_tokens,
    )


class GroqProvider(LLMProvider):

    id = "groq"

    def _candidates(self, thunk_factory):
        """(candidate_id, display_label, thunk) triples, one per
        configured model, in order. thunk_factory(model) must return a
        zero-argument callable performing that model's actual
        request + parse."""
        return [
            (f"groq-model-{model}", f"model: {model}", thunk_factory(model))
            for model in GROQ_MODELS
        ]

    def evaluate_job(self, text: str, filter_result: dict, system_prompt: str = None) -> dict:

        if system_prompt is None:
            from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
            system_prompt = SYSTEM_PROMPT
        prompt = build_prompt(text, filter_result)

        def make_thunk(model):
            def thunk():
                response = _generate_response(model, prompt, system_prompt, max_tokens=600)
                _raise_if_truncated(response)
                return parse_response(response.choices[0].message.content)
            return thunk

        result, _ = run_with_rotation("Groq", self._candidates(make_thunk))
        return result

    def evaluate_category_arbitration(
        self, text: str, candidates: list[dict], system_prompt: str = None
    ) -> dict:
        prompt = build_arbitration_prompt(text, candidates)
        allowed = {item["id"] for item in candidates}

        def make_thunk(model):
            def thunk():
                response = _generate_response(model, prompt, system_prompt, max_tokens=500)
                _raise_if_truncated(response)
                return parse_arbitration_response(
                    response.choices[0].message.content, allowed
                )
            return thunk

        result, _ = run_with_rotation("Groq", self._candidates(make_thunk))
        return result


# Module-level singleton + free functions, kept for backward
# compatibility with existing callers/tests that import
# app.llm.groq.evaluate_job / evaluate_category_arbitration directly
# (and that monkeypatch app.llm.groq.CLIENT / GROQ_MODELS --
# GroqProvider reads these module-level globals at call time, not at
# construction, so that monkeypatch pattern keeps working unchanged).
_provider = GroqProvider()


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None) -> dict:
    return _provider.evaluate_job(text, filter_result, system_prompt)


def evaluate_category_arbitration(
    text: str, candidates: list[dict], system_prompt: str = None
) -> dict:
    return _provider.evaluate_category_arbitration(text, candidates, system_prompt)
