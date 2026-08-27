import asyncio
import importlib
import time

from app.notification_guard.config import NOTIFICATION_GUARD_ENABLED
from app.notification_guard.groq import GroqNotificationGuard
from app.notification_guard.logger import log_guard_decision
from app.categories.registry import get_category


FULL_STACK_CATEGORY_ID = "full_stack"


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
    the only real ambiguity worth spending a Groq call on is
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

        self.enabled = NOTIFICATION_GUARD_ENABLED

        self.provider = (
            GroqNotificationGuard()
            if self.enabled
            else None
        )

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

            allowed = await asyncio.to_thread(
                self.provider.evaluate,
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
                provider="Groq",
                model=self.provider.model,
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
                provider="Groq",
                model=(
                    self.provider.model
                    if self.provider is not None
                    else "",
                ),
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
        GroqNotificationGuard._parse_decision_with_category).

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

            allowed, resolved_category_id = await asyncio.to_thread(
                self.provider.evaluate_with_category,
                job.get("title", ""),
                job.get("description", ""),
                combined_prompt,
                category_id,
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
                provider="Groq",
                model=self.provider.model,
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
                provider="Groq",
                model=(
                    self.provider.model
                    if self.provider is not None
                    else "",
                ),
                response_time_ms=response_time_ms,
                error=str(exc),
            )

            return {"allowed": False, "category_id": category_id}


notification_guard = NotificationGuard()
