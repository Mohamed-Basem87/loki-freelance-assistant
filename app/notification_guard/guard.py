import asyncio
import importlib
import time

from app.notification_guard import config as guard_config
from app.notification_guard.groq import GroqNotificationGuard
from app.notification_guard.logger import log_guard_decision
from app.categories.registry import get_category


FULL_STACK_CATEGORY_ID = "full_stack"


# ---------------------------------------------------------------------------
# Guard provider orchestration.
#
# Mirror of app.llm.manager: every registered guard provider is tried,
# in order, stopping at the first success. Adding another guard
# provider means:
#   1. implement app.notification_guard.provider.GuardProvider (see
#      app.notification_guard.groq for the pattern -- providers build
#      their own candidate list and use the shared
#      app.llm.rotation.run_with_rotation executor internally, so every
#      provider gets the same cooldown/classification behavior for free
#      without reimplementing it);
#   2. add its (provider_id, factory) here.
# Nothing else in this file, or in app.job_processor, needs to change.
#
# GroqNotificationGuard is referenced by bare name inside the factory
# lambdas (not tucked inside a class or list comprehension) so tests
# can keep monkeypatching it directly by name -- same convention
# app.llm.manager uses for gemini_evaluate/groq_evaluate.
_GUARD_PROVIDERS = [
    ("groq", lambda: GroqNotificationGuard()),
]


def _evaluate_guard(title: str, description: str, system_prompt: str):
    """Try every registered guard provider, first success wins.

    Mirrors app.llm.manager._EVALUATE_PROVIDERS orchestration. The
    calling wrapper (NotificationGuard.allow) runs this in a worker
    thread so blocking provider I/O never stalls the event loop.

    Returns (allowed, winning_provider_id, winning_model) so the
    wrapper can log which backend actually decided -- manager.py only
    needs the raw result, the guard's persistence layer records
    provider/model.

    Raises RuntimeError, with every provider's failure message joined,
    if every registered provider fails (or none are registered).
    """
    failures = []
    last_exception = None

    for index, (provider_id, factory) in enumerate(_GUARD_PROVIDERS):
        provider = factory()
        try:
            allowed = provider.evaluate(title, description, system_prompt)
            return allowed, provider.id, provider.model
        except Exception as e:
            print(f"{provider_id.capitalize()} guard failed: {e}")
            failures.append(f"{provider_id}: {e}")
            last_exception = e
            if index + 1 < len(_GUARD_PROVIDERS):
                next_id = _GUARD_PROVIDERS[index + 1][0]
                print(f"Falling back to {next_id.capitalize()} guard...")
            continue

    raise RuntimeError(
        "All guard providers failed. " + " | ".join(failures)
    ) from last_exception


def _evaluate_guard_with_category(
    title: str,
    description: str,
    system_prompt: str,
    original_category_id: str,
):
    """Try every registered guard provider, first success wins.

    Returns (allowed, resolved_category_id, winning_provider_id,
    winning_model). resolved_category_id is always either
    original_category_id or "full_stack" (see GuardProvider.
    evaluate_with_category). Same fallback/fail behavior as
    _evaluate_guard.
    """
    failures = []
    last_exception = None

    for index, (provider_id, factory) in enumerate(_GUARD_PROVIDERS):
        provider = factory()
        try:
            allowed, resolved_category_id = provider.evaluate_with_category(
                title, description, system_prompt, original_category_id
            )
            return (
                allowed,
                resolved_category_id,
                provider.id,
                provider.model,
            )
        except Exception as e:
            print(f"{provider_id.capitalize()} guard failed: {e}")
            failures.append(f"{provider_id}: {e}")
            last_exception = e
            if index + 1 < len(_GUARD_PROVIDERS):
                next_id = _GUARD_PROVIDERS[index + 1][0]
                print(f"Falling back to {next_id.capitalize()} guard...")
            continue

    raise RuntimeError(
        "All guard providers failed. " + " | ".join(failures)
    ) from last_exception


def _build_combined_system_prompt(
    original_prompt: str,
    full_stack_prompt: str,
    original_category_id: str,
) -> str:
    """
    Combine the original category's guard prompt with full_stack's
    into one prompt that asks for a category choice instead of a
    plain yes/no. Both source prompts end with their own standalone
    "return {"decision": ...}" output-format instruction, written for
    single-category use; those are explicitly overridden here rather
    than left to conflict, matching the same pattern
    app.llm.manager uses when composing multiple category prompts for
    arbitration (a category's own embedded output-format instructions
    do not apply once another prompt is concatenated alongside it).

    Deliberately loads exactly two prompts, never more: the tiering
    system already narrowed this job to one specialist category, so
    the only real ambiguity worth an extra guard call on is
    "this specialist, or actually full_stack" -- not a re-run of full
    multi-category arbitration.
    """
    return f"""You are choosing between exactly two possible outcomes for this job,
each governed by its own scope/rejection criteria below. Use each
section only to judge whether that section's scope fits. Ignore any
output-format instructions inside either section (e.g. any
"return {{\"decision\": ...}}" line they contain) -- the output format
for THIS decision is given at the end of this prompt instead.

=== OPTION A: "{original_category_id}" ===
{original_prompt}

=== OPTION B: "full_stack" ===
{full_stack_prompt}

=== YOUR TASK ===
A deterministic keyword classifier already matched this job to
"{original_category_id}" as a clean, direct, single-category match.
Decide:

1. Should this job be notified at all? Apply whichever of the two
   sections above is the better fit for the work actually described.
2. If notifying: is it genuinely just "{original_category_id}" work,
   or does the work actually span multiple layers such that
   "full_stack" (per OPTION B's own criteria for when full_stack
   should win over a specialist match) is the more accurate category?

Return exactly one JSON object and nothing else:
{{"decision": "notify" | "do_not_notify", "category": "{original_category_id}" | "full_stack"}}

If "do_not_notify", "category" is not read and can be any value.
Do not return markdown, explanations, or additional fields.
""".strip()


class NotificationGuard:

    def __init__(self):

        self.enabled = guard_config.NOTIFICATION_GUARD_ENABLED

    async def allow(
        self,
        job: dict,
        *,
        original_decision="",
        category_id="",
    ) -> bool:
        """
        Return True only when the job is allowed to reach the notifier.

        The guard is fail-closed:
        provider errors, malformed responses, and unexpected failures
        suppress the notification.

        Every actual guard evaluation is recorded in the dedicated
        notification_guard table through the existing DB logger.

        This single-category form is kept for compatibility with any
        caller that only needs a plain notify/suppress check. The
        production notification pipeline calls decide() below instead,
        which also lets the guard reclassify the job as full_stack.
        """

        if not self.enabled:
            return True

        started = time.perf_counter()

        try:

            profile = get_category(category_id)
            if profile is None:
                raise ValueError(f"Unknown category for notification guard: {category_id}")

            prompt_module = importlib.import_module(profile.guard_prompt_module)
            system_prompt = prompt_module.SYSTEM_PROMPT

            allowed, provider_id, model = await asyncio.to_thread(
                _evaluate_guard,
                job.get("title", ""),
                job.get("description", ""),
                system_prompt,
            )

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision=(
                    "notify"
                    if allowed
                    else "do_not_notify"
                ),
                provider=provider_id,
                model=model,
                response_time_ms=response_time_ms,
                guard_category=category_id if allowed else "",
            )

            return allowed

        except Exception as exc:

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision="error",
                provider="",
                model="",
                response_time_ms=response_time_ms,
                error=str(exc),
            )

            return False

    async def decide(
        self,
        job: dict,
        *,
        original_decision="",
        category_id="",
    ) -> dict:
        """
        Superset of allow(): also lets the guard reclassify the job
        from its original keyword-matched category to "full_stack"
        when the work genuinely spans multiple layers rather than
        being purely "{category_id}" work.

        Returns {"allowed": bool, "category_id": str}. category_id is
        always either the original category_id or "full_stack" --
        never blindly trusted from the provider response (see
        GuardProvider.evaluate_with_category).

        Every fail-closed / persistence behavior matches allow()
        exactly; this additionally persists which category a
        "notify" decision applies to (the "Guard Category" column),
        so a resumed job can reapply a durable reclassification
        without re-asking the provider.
        """

        if not self.enabled:
            return {"allowed": True, "category_id": category_id}

        started = time.perf_counter()

        try:

            profile = get_category(category_id)
            if profile is None:
                raise ValueError(f"Unknown category for notification guard: {category_id}")

            full_stack_profile = get_category(FULL_STACK_CATEGORY_ID)
            if full_stack_profile is None:
                raise ValueError("full_stack category is not registered")

            original_module = importlib.import_module(profile.guard_prompt_module)
            full_stack_module = importlib.import_module(
                full_stack_profile.guard_prompt_module
            )

            combined_prompt = _build_combined_system_prompt(
                original_module.SYSTEM_PROMPT,
                full_stack_module.SYSTEM_PROMPT,
                category_id,
            )

            allowed, resolved_category_id, provider_id, model = (
                await asyncio.to_thread(
                    _evaluate_guard_with_category,
                    job.get("title", ""),
                    job.get("description", ""),
                    combined_prompt,
                    category_id,
                )
            )

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision=(
                    "notify"
                    if allowed
                    else "do_not_notify"
                ),
                provider=provider_id,
                model=model,
                response_time_ms=response_time_ms,
                guard_category=resolved_category_id if allowed else "",
            )

            return {
                "allowed": allowed,
                "category_id": resolved_category_id if allowed else category_id,
            }

        except Exception as exc:

            response_time_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            await log_guard_decision(
                job_uuid=job.get("job_uuid", ""),
                source=job.get("source", ""),
                title=job.get("title", ""),
                original_decision=original_decision,
                guard_decision="error",
                provider="",
                model="",
                response_time_ms=response_time_ms,
                error=str(exc),
            )

            return {"allowed": False, "category_id": category_id}


notification_guard = NotificationGuard()
