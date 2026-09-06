from google import genai
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GEMINI_API_KEYS
from app.llm import rate_limit_tracker
from app.llm.provider import LLMProvider
from app.llm.rotation import run_with_rotation
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response


CLIENTS = [
    genai.Client(api_key=key)
    for key in GEMINI_API_KEYS
]


@retry(
    # Only retry the *same* key/request for errors that plausibly
    # succeed on a second attempt (rate limit, transient
    # unavailability). A malformed request, auth failure, or a
    # response-parsing error will never succeed by just waiting a
    # second and asking again -- retrying those only adds latency
    # before we (correctly) move on to the next key.
    retry=retry_if_exception(rate_limit_tracker.is_transient),
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    reraise=True,
)
def _generate_response(client: genai.Client, contents: str, system_instruction: str):
    # system_instruction is passed via GenerateContentConfig rather
    # than concatenated into `contents` -- Gemini's supported
    # mechanism for the same evaluator/data separation the Groq path
    # already gets for free from its system/user message roles (see
    # app.llm.groq). `contents` still carries only the untrusted-job-
    # containing user prompt, matching Groq's "user" message exactly.
    return client.models.generate_content(
        model="gemini-3.5-flash",
        contents=contents,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
        ),
    )


def _raise_if_truncated(response):
    """Gemini's equivalent of Groq's finish_reason == "length" check
    (see app.llm.groq) -- FinishReason.MAX_TOKENS means the completion
    was cut off by its output-token cap before finishing its JSON.
    Gemini's calls don't currently set an explicit output-token cap
    (unlike Groq, which needed one after a real production rejection --
    see app.llm.groq's own comment), so this is presently a defensive
    no-op; it exists so the same protection is already in place the
    moment a cap is ever added here, rather than needing to be
    rediscovered the way Groq's gap was.
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

    def _candidates(self, thunk_factory):
        """(candidate_id, display_label, thunk) triples, one per
        configured API key, in order. thunk_factory(client) must
        return a zero-argument callable performing that key's actual
        request + parse."""
        return [
            (f"gemini-key{index}", f"key #{index}", thunk_factory(client))
            for index, client in enumerate(CLIENTS, start=1)
        ]

    def evaluate_job(self, text: str, filter_result: dict, system_prompt: str = None) -> dict:

        if system_prompt is None:
            from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
            system_prompt = SYSTEM_PROMPT
        prompt = build_prompt(text, filter_result)

        def make_thunk(client):
            def thunk():
                response = _generate_response(client, prompt, system_prompt)
                _raise_if_truncated(response)
                return parse_response(response.text)
            return thunk

        result, _ = run_with_rotation(
            "Gemini",
            self._candidates(make_thunk),
            mark_unknown_as_permanent=self._mark_unknown_as_permanent,
        )
        return result

    def evaluate_category_arbitration(
        self, text: str, candidates: list[dict], system_prompt: str = None
    ) -> dict:
        prompt = build_arbitration_prompt(text, candidates)
        allowed = {item["id"] for item in candidates}

        def make_thunk(client):
            def thunk():
                response = _generate_response(client, prompt, system_prompt)
                _raise_if_truncated(response)
                return parse_arbitration_response(response.text, allowed)
            return thunk

        result, _ = run_with_rotation(
            "Gemini",
            self._candidates(make_thunk),
            mark_unknown_as_permanent=self._mark_unknown_as_permanent,
        )
        return result


# Module-level singleton + free functions, kept for backward
# compatibility with existing callers/tests that import
# app.llm.gemini.evaluate_job / evaluate_category_arbitration directly
# (and that monkeypatch app.llm.gemini.CLIENTS -- GeminiProvider reads
# the module-level CLIENTS list at call time, not at construction, so
# that monkeypatch pattern keeps working unchanged).
_provider = GeminiProvider()


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None) -> dict:
    return _provider.evaluate_job(text, filter_result, system_prompt)


def evaluate_category_arbitration(
    text: str, candidates: list[dict], system_prompt: str = None
) -> dict:
    return _provider.evaluate_category_arbitration(text, candidates, system_prompt)
