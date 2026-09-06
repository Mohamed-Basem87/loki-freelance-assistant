"""
The interface every LLM provider (Gemini, Groq, and any future
addition) implements, so app.llm.manager can try each one in turn
without knowing anything provider-specific -- no hardcoded per-
provider try/except chain, no special-casing which provider needs a
compact vs. full-depth prompt. Adding a third provider means writing
one new class implementing this interface and adding it to
app.llm.manager.PROVIDERS; nothing else in the pipeline needs to
change.

Each provider owns its own internal candidate rotation (Gemini rotates
across API keys; Groq rotates across models on one key) via
app.llm.rotation.run_with_rotation, and owns its own prompt-building
strategy internally -- a provider with tighter request-size limits
(like Groq) is free to build a more compact prompt than one without,
without app.llm.manager needing to know that distinction exists.
"""

from abc import ABC, abstractmethod


class LLMProvider(ABC):

    #: Short, stable identifier -- used both as the "provider" value
    #: recorded on the job row (see app.job_processor) and as the
    #: candidate-id namespace prefix in app.llm.rate_limit_tracker
    #: (e.g. "gemini-key1", "groq-model-...").
    id: str

    @abstractmethod
    def evaluate_job(self, text: str, filter_result: dict, system_prompt: str = None) -> dict:
        """Single-category accept/reject evaluation. Returns a dict
        matching app.llm.utils.REQUIRED_KEYS. Raises on total failure
        (every internal candidate exhausted)."""
        raise NotImplementedError

    @abstractmethod
    def evaluate_category_arbitration(
        self, text: str, candidates: list[dict], system_prompt: str = None
    ) -> dict:
        """Multi-category arbitration. Returns a dict matching
        app.llm.utils.ARBITRATION_REQUIRED_KEYS. Raises on total
        failure (every internal candidate exhausted)."""
        raise NotImplementedError
