from google import genai
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import GEMINI_API_KEYS
from app.llm import rate_limit_tracker
from app.llm.utils import build_prompt, build_arbitration_prompt, parse_response, parse_arbitration_response


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
def _generate_response(client: genai.Client, contents: str, system_instruction: str):
    # system_instruction is passed via GenerateContentConfig rather
    # than concatenated into `contents` -- Gemini's supported
    # mechanism for the same evaluator/data separation the Groq path
    # already gets for free from its system/user message roles (see
    # app.llm.groq). Previously both were joined into a single
    # undifferentiated string here, giving the primary LLM path a
    # weaker instruction/data boundary than its own fallback despite
    # identical semantic content; `contents` still carries only the
    # untrusted-job-containing user prompt, matching Groq's "user"
    # message exactly.
    return client.models.generate_content(
        model="gemini-3.5-flash",
        contents=contents,
        config=genai.types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
        ),
    )


def evaluate_job(text: str, filter_result: dict, system_prompt: str = None):

    if system_prompt is None:
        from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT
        system_prompt = SYSTEM_PROMPT
    prompt = build_prompt(text, filter_result)

    failures = []
    last_exception = None

    # Candidate ids are positional ("gemini-key1", "gemini-key2", ...)
    # rather than the raw API key itself -- stable across calls for
    # the lifetime of this process (CLIENTS is built once at import
    # time in a fixed order) without ever putting a real secret into
    # the tracker's in-memory keys.
    all_ids = [f"gemini-key{i}" for i in range(1, len(CLIENTS) + 1)]
    available_ids = set(rate_limit_tracker.filter_available(all_ids))
    skipped = len(all_ids) - len(available_ids)
    if skipped:
        print(
            f"Skipping {skipped} Gemini key(s) still in cooldown "
            f"from a recent rate-limit/quota failure."
        )

    for index, client in enumerate(CLIENTS, start=1):

        candidate_id = f"gemini-key{index}"
        if candidate_id not in available_ids:
            continue

        print(f"Using Gemini API key #{index}")

        try:

            response = _generate_response(
                client,
                prompt,
                system_prompt,
            )

            rate_limit_tracker.mark_success(candidate_id)
            return parse_response(response.text)

        except Exception as e:

            failures.append(f"key #{index}: {e}")
            last_exception = e

            if rate_limit_tracker.is_quota_exhaustion(str(e)):
                cooldown = rate_limit_tracker.mark_rate_limited(
                    candidate_id, str(e)
                )
                print(
                    f"Gemini key #{index} failed: {e}\n"
                    f"Marking key #{index} unavailable for "
                    f"{cooldown:.0f}s before it's tried again."
                )
            else:
                # Covers both a momentary 503/overload (still worth
                # retrying fresh on the *next* call -- no reason to
                # believe this key is specifically bad) and a one-off
                # malformed/unparseable response, which says nothing
                # about whether this key will work for the next job.
                # Neither gets marked in the cooldown tracker.
                print(f"Gemini key #{index} failed: {e}")

            # Always try the remaining keys, regardless of *why* this
            # one failed -- a malformed/unparseable response from key
            # #1 says nothing about whether key #2 would work, so
            # there's no good reason to give up on the whole provider
            # over it. We only stop early once every key/model has
            # been tried (see the loop ending below), same as Groq.
            print("Trying next key..." if index < len(CLIENTS) else "No more Gemini keys.")
            continue

    if failures:
        # Persist every key's failure, not just the last one -- the
        # surviving error is what job_processor logs to the errors
        # table, and per-key detail is the only way to diagnose an
        # outage (quota vs malformed response vs auth) after the fact.
        raise RuntimeError(
            "All Gemini API keys failed. " + " | ".join(failures)
        ) from last_exception

    raise RuntimeError("No Gemini API keys are configured.")


def evaluate_category_arbitration(
    text: str,
    candidates: list[dict],
    system_prompt: str,
):
    prompt = build_arbitration_prompt(text, candidates)
    allowed = {item["id"] for item in candidates}
    failures = []
    last_exception = None

    all_ids = [f"gemini-key{i}" for i in range(1, len(CLIENTS) + 1)]
    available_ids = set(rate_limit_tracker.filter_available(all_ids))
    skipped = len(all_ids) - len(available_ids)
    if skipped:
        print(
            f"Skipping {skipped} Gemini key(s) still in cooldown "
            f"from a recent rate-limit/quota failure."
        )

    for index, client in enumerate(CLIENTS, start=1):

        candidate_id = f"gemini-key{index}"
        if candidate_id not in available_ids:
            continue

        print(f"Using Gemini API key #{index} for category arbitration")
        try:
            response = _generate_response(client, prompt, system_prompt)
            rate_limit_tracker.mark_success(candidate_id)
            return parse_arbitration_response(response.text, allowed)
        except Exception as e:
            failures.append(f"key #{index}: {e}")
            last_exception = e

            if rate_limit_tracker.is_quota_exhaustion(str(e)):
                cooldown = rate_limit_tracker.mark_rate_limited(
                    candidate_id, str(e)
                )
                print(
                    f"Gemini arbitration key #{index} failed: {e}\n"
                    f"Marking key #{index} unavailable for "
                    f"{cooldown:.0f}s before it's tried again."
                )
            else:
                print(f"Gemini arbitration key #{index} failed: {e}")

            continue

    if failures:
        raise RuntimeError(
            "All Gemini API keys failed for category arbitration. "
            + " | ".join(failures)
        ) from last_exception
    raise RuntimeError("No Gemini API keys are configured.")
