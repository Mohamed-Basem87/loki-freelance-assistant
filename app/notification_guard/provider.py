"""
The interface every notification-guard LLM provider (currently Groq,
and any future addition) implements, so app.notification_guard.guard
can build and delegate to a provider without knowing anything
provider-specific -- no hardcoded per-provider try/except chain, no
special-casing which backend the guard is backed by. Adding a third
guard provider means writing one new class implementing this
interface and registering it in app.notification_guard.guard's
provider registry; nothing else in the guard pipeline needs to
change.

This is the direct mirror of app.llm.provider.LLMProvider for the
notification guard. The two intended differences are the shape of the
return values -- the guard answers yes/no (optionally with a category
reclassification), not the main pipeline's full classification dict --
and that each guard provider owns its own internal (key[, model])
candidate rotation via app.llm.rotation.run_with_rotation, exactly
like the main pipeline's providers.

A guard provider's methods are synchronous/blocking (they make the
actual provider request and parse its response); app.notification_
guard.guard runs them through asyncio.to_thread, mirroring how the
providers are invoked there.
"""

from abc import ABC, abstractmethod


class GuardProvider(ABC):

    #: Short, stable identifier -- used as the "provider" value
    #: recorded on the guard log row and as the candidate-id namespace
    #: prefix in app.llm.rate_limit_tracker (e.g. "groq-guard-key1-...").
    id: str

    #: Human-readable model name reported to the guard logger. Kept on
    #: the provider (not computed by the wrapper) so each provider can
    #: decide how to describe whichever backend it actually used, in
    #: the same way it owns its own candidate rotation.
    model: str

    @abstractmethod
    def evaluate(self, title: str, description: str, system_prompt: str, deadline=None) -> bool:
        """Single-category notify/suppress evaluation.

        Returns True to allow notification, False to suppress it.
        Raises on total failure (every internal candidate exhausted) --
        the wrapper is fail-closed, so any exception denies.

        deadline: optional monotonic timestamp (time.monotonic()) by
        which the rotation must not start any NEW candidate attempts.
        In-flight provider calls are bounded by their own HTTP timeouts.
        """
        raise NotImplementedError

    @abstractmethod
    def evaluate_with_category(
        self,
        title: str,
        description: str,
        system_prompt: str,
        original_category_id: str,
        deadline=None,
    ) -> tuple[bool, str]:
        """Like evaluate(), but the provider may also reclassify the job
        as "full_stack" rather than its original single-category match.

        `system_prompt` instructs the model to return both a decision
        and a category. Returns (allowed, resolved_category_id), where
        resolved_category_id is always either `original_category_id` or
        "full_stack" -- never blindly trusted from the response.
        Raises on total failure (fail-closed).

        deadline: optional monotonic timestamp (time.monotonic()) by
        which the rotation must not start any NEW candidate attempts.
        In-flight provider calls are bounded by their own HTTP timeouts.
        """
        raise NotImplementedError
