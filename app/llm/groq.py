from groq import Groq
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import get_groq_api_key
from app.runtime_config import LLM_PROVIDERS, RUNTIME
from app.llm import rate_limit_tracker
from app.llm.provider import LLMProvider
from app.llm.rotation import run_with_rotation
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response, GENERIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# BUG #2: bounded HTTP lifetime on the Groq SDK (see the matching comment in
# app.llm.gemini for the full reasoning). The sync SDK call must always
# return within a bounded wall-clock window so it can never indefinitely
# block the dedicated asyncio.to_thread worker that runs it.
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT_SECONDS = float(RUNTIME.http_timeout_seconds)


CLIENT = None

def _client():
    global CLIENT
    if CLIENT is None:
        CLIENT = Groq(
            api_key=get_groq_api_key(),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    return CLIENT

GROQ_MODELS = next(p.models for p in LLM_PROVIDERS if p.provider_id == "groq")
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
    stop=stop_after_attempt(next(p.retry.max_attempts for p in LLM_PROVIDERS if p.provider_id == "groq")),
    wait=wait_fixed(next(p.retry.wait_seconds for p in LLM_PROVIDERS if p.provider_id == "groq")),
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
    return _client().chat.completions.create(
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

    def evaluate_job(self, text: str, filter_result: dict, system_prompt: str = None, deadline=None) -> dict:

        if system_prompt is None:
            system_prompt = GENERIC_SYSTEM_PROMPT
        prompt = build_prompt(text, filter_result)

        def make_thunk(model):
            def thunk():
                response = _generate_response(model, prompt, system_prompt, max_tokens=next(p.max_output_tokens for p in LLM_PROVIDERS if p.provider_id == "groq"))
                _raise_if_truncated(response)
                return parse_response(response.choices[0].message.content)
            return thunk

        result, _, _ = run_with_rotation("Groq", self._candidates(make_thunk), deadline=deadline)
        return result

    def evaluate_category_arbitration(
        self, text: str, candidates: list[dict], system_prompt: str = None, deadline=None
    ) -> dict:
        prompt = build_arbitration_prompt(text, candidates)
        allowed = {item["id"] for item in candidates}

        def make_thunk(model):
            def thunk():
                response = _generate_response(model, prompt, system_prompt, max_tokens=next(p.arbitration_max_output_tokens for p in LLM_PROVIDERS if p.provider_id == "groq"))
                _raise_if_truncated(response)
                return parse_arbitration_response(
                    response.choices[0].message.content, allowed
                )
            return thunk

        result, _, _ = run_with_rotation("Groq", self._candidates(make_thunk), deadline=deadline)
        return result


# Module-level singleton + free functions, kept for backward
# compatibility with existing callers/tests that import
# app.llm.groq.evaluate_job / evaluate_category_arbitration directly
# (and that monkeypatch app.llm.groq.CLIENT / GROQ_MODELS --
# GroqProvider reads these module-level globals at call time, not at
# construction, so that monkeypatch pattern keeps working unchanged).
_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = GroqProvider()
    return _provider


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None, deadline=None) -> dict:
    return _get_provider().evaluate_job(text, filter_result, system_prompt, deadline=deadline)


def evaluate_category_arbitration(
    text: str, candidates: list[dict], system_prompt: str = None, deadline=None
) -> dict:
    return _get_provider().evaluate_category_arbitration(text, candidates, system_prompt, deadline=deadline)
