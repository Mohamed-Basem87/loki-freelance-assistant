from google import genai
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import get_gemini_api_keys
from app.runtime_config import LLM_PROVIDERS, RUNTIME
from app.llm import rate_limit_tracker
from app.llm.provider import LLMProvider
from app.llm.rotation import run_with_rotation
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response, GENERIC_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# BUG #2: the Gemini SDK's synchronous request must have a bounded lifetime.
#
# Previously _clients() built each genai.Client with only an api_key, so a
# stalled network connection could block the SDK call forever. In the
# pipeline that call runs on a dedicated thread via asyncio.to_thread wrapped
# in asyncio.wait_for (app.job_processor / app.notification_guard.guard) --
# and wait_for CANNOT cancel a blocked thread: it fires, the Future is
# cancelled, but the underlying thread keeps spinning indefinitely on the
# dead socket, silently leaking one thread per stuck provider call and
# poisoning the shared default executor.
#
# The real fix (per the remediation contract: "the underlying synchronous
# provider operation must have a bounded lifetime -- SDK/HTTP-layer timeout,
# not merely catching TimeoutError") is to give the SDK a hard HTTP timeout so
# the synchronous call ALWAYS returns -- with a raised error that the existing
# tenacity retry / run_with_rotation cooldown machinery already routes through
# (a timeout is classified transient) -- rather than ever hanging the thread.
# ---------------------------------------------------------------------------
_HTTP_TIMEOUT_SECONDS = float(RUNTIME.http_timeout_seconds)


CLIENTS = None

def _clients():
    global CLIENTS
    if CLIENTS is None:
        CLIENTS = [
            genai.Client(
                api_key=key,
                http_options=genai.types.HttpOptions(
                    timeout=_HTTP_TIMEOUT_SECONDS,
                ),
            )
            for key in get_gemini_api_keys()
        ]
    return CLIENTS


@retry(
    # Only retry the *same* key/request for errors that plausibly
    # succeed on a second attempt (rate limit, transient
    # unavailability). A malformed request, auth failure, or a
    # response-parsing error will never succeed by just waiting a
    # second and asking again -- retrying those only adds latency
    # before we (correctly) move on to the next key.
    retry=retry_if_exception(rate_limit_tracker.is_transient),
    stop=stop_after_attempt(next(p.retry.max_attempts for p in LLM_PROVIDERS if p.provider_id == "gemini")),
    wait=wait_fixed(next(p.retry.wait_seconds for p in LLM_PROVIDERS if p.provider_id == "gemini")),
    reraise=True,
)
def _generate_response(
    client: genai.Client,
    model: str,
    contents: str,
    system_instruction: str,
    max_output_tokens: int,
):
    # system_instruction is passed via GenerateContentConfig rather
    # than concatenated into `contents` -- Gemini's supported
    # mechanism for the same evaluator/data separation the Groq path
    # already gets for free from its system/user message roles (see
    # app.llm.groq). `contents` still carries only the untrusted-job-
    # containing user prompt, matching Groq's "user" message exactly.
    #
    # max_output_tokens mirrors Groq's own explicit per-call cap (see
    # app.llm.groq._generate_response's docstring): config/project.json
    # defines a max_output_tokens/arbitration_max_output_tokens policy
    # for Gemini too, passed here as GenerateContentConfig.
    # max_output_tokens, so output size is actually bounded and
    # _raise_if_truncated()'s FinishReason check below is live protection
    # rather than a clause that can never fire.
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        ),
    )


def _raise_if_truncated(response):
    """Gemini's equivalent of Groq's finish_reason == "length" check
    (see app.llm.groq) -- FinishReason.MAX_TOKENS means the completion
    was cut off by its output-token cap before finishing its JSON.
    _generate_response passes config/project.json's
    max_output_tokens/arbitration_max_output_tokens policy as an
    explicit GenerateContentConfig.max_output_tokens cap, so this check
    is live protection (rendered JSON that silently dropped its tail
    would otherwise be accepted as a complete evaluation).
    """
    try:
        finish_reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError):
        return
    if finish_reason == genai.types.FinishReason.MAX_TOKENS:
        raise rate_limit_tracker.TruncatedResponseError(
            "Completion was cut off by the output-token cap before "
            "finishing its JSON (finish_reason=MAX_TOKENS)."
        )


class GeminiProvider(LLMProvider):

    id = "gemini"

    # Gemini's historical, deliberately conservative choice (see
    # app.llm.rotation.run_with_rotation's own docstring for the full
    # reasoning): an ambiguous one-off failure here (e.g. a malformed
    # response) says nothing certain enough about a specific key to
    # justify costing it 6 hours of availability, unlike Groq's
    # decommissioned-model case, which genuinely can't self-resolve.
    _mark_unknown_as_permanent = False

    @property
    def models(self):
        return tuple(
            p.models for p in LLM_PROVIDERS if p.provider_id == "gemini"
        )[0]

    def _candidates(self, thunk_factory):
        """(candidate_id, display_label, thunk) triples, one per
        configured API key, in order. thunk_factory(client) must
        return a zero-argument callable performing that key's actual
        request + parse."""
        return [
            (
                f"gemini-key{key_index}-model-{model}",
                f"key #{key_index}, model: {model}",
                thunk_factory(client, model),
            )
            for key_index, client in enumerate(_clients(), start=1)
            for model in self.models
        ]

    def evaluate_job(self, text: str, filter_result: dict, system_prompt: str = None, deadline=None) -> dict:

        if system_prompt is None:
            system_prompt = GENERIC_SYSTEM_PROMPT
        prompt = build_prompt(text, filter_result)

        max_output_tokens = next(
            p.max_output_tokens for p in LLM_PROVIDERS if p.provider_id == "gemini"
        )

        def make_thunk(client, model):
            def thunk():
                response = _generate_response(
                    client, model, prompt, system_prompt, max_output_tokens
                )
                _raise_if_truncated(response)
                return parse_response(response.text)
            return thunk

        result, _, _ = run_with_rotation(
            "Gemini",
            self._candidates(make_thunk),
            mark_unknown_as_permanent=self._mark_unknown_as_permanent,
            deadline=deadline,
        )
        return result

    def evaluate_category_arbitration(
        self, text: str, candidates: list[dict], system_prompt: str = None, deadline=None
    ) -> dict:
        prompt = build_arbitration_prompt(text, candidates)
        allowed = {item["id"] for item in candidates}
        max_output_tokens = next(
            p.arbitration_max_output_tokens for p in LLM_PROVIDERS if p.provider_id == "gemini"
        )

        def make_thunk(client, model):
            def thunk():
                response = _generate_response(
                    client, model, prompt, system_prompt, max_output_tokens
                )
                _raise_if_truncated(response)
                return parse_arbitration_response(response.text, allowed)
            return thunk

        result, _, _ = run_with_rotation(
            "Gemini",
            self._candidates(make_thunk),
            mark_unknown_as_permanent=self._mark_unknown_as_permanent,
            deadline=deadline,
        )
        return result


# Module-level singleton + free functions, kept for backward
# compatibility with existing callers/tests that import
# app.llm.gemini.evaluate_job / evaluate_category_arbitration directly
# (and that monkeypatch app.llm.gemini.CLIENTS -- GeminiProvider reads
# the module-level CLIENTS list at call time, not at construction, so
# that monkeypatch pattern keeps working unchanged).
_provider = None

def _get_provider():
    global _provider
    if _provider is None:
        _provider = GeminiProvider()
    return _provider


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None, deadline=None) -> dict:
    return _get_provider().evaluate_job(text, filter_result, system_prompt, deadline=deadline)


def evaluate_category_arbitration(
    text: str, candidates: list[dict], system_prompt: str = None, deadline=None
) -> dict:
    return _get_provider().evaluate_category_arbitration(text, candidates, system_prompt, deadline=deadline)
