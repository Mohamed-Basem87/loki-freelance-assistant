import json
import os

from groq import Groq

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_fixed,
)

from app.llm import rate_limit_tracker
from app.llm.rotation import run_with_rotation
from app.notification_guard.config import (
    NOTIFICATION_GUARD_API_KEYS,
    NOTIFICATION_GUARD_MODELS,
    NOTIFICATION_GUARD_MAX_RETRIES,
)
from app.notification_guard.provider import GuardProvider
from app.notification_guard.prompt import build_prompt
from app.categories.registry import arbitration_only_categories
from app.runtime_config import LLM_PROVIDERS, RUNTIME


# BUG #2: bounded HTTP lifetime on the guard's Groq SDK (see app.llm.gemini
# for the full reasoning) so a synchronous guard call can never indefinitely
# block the dedicated asyncio.to_thread worker that runs it.
_HTTP_TIMEOUT_SECONDS = float(RUNTIME.http_timeout_seconds)


CLIENTS = []

def _clients():
    global CLIENTS
    if not CLIENTS:
        CLIENTS = [
            Groq(
                api_key=key,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            for key in NOTIFICATION_GUARD_API_KEYS
        ]
    return CLIENTS


@retry(
    retry=retry_if_exception(rate_limit_tracker.is_transient),
    stop=stop_after_attempt(NOTIFICATION_GUARD_MAX_RETRIES),
    wait=wait_fixed(next(p.retry.wait_seconds for p in LLM_PROVIDERS if p.provider_id == "groq")),
    reraise=True,
)
def _generate_response(
    client: Groq,
    model: str,
    title: str,
    description: str,
    system_prompt: str,
    max_tokens: int | None = None,
):
    """
    max_tokens=500 default: the guard's response schema is tiny
    ({"decision": ...} or {"decision": ..., "category": ...}), but
    Groq's output-tokens-per-minute pre-flight check assumes the
    model's own default maximum when no cap is given -- see the
    matching comment in app.llm.groq._generate_response for the exact
    production log line this is defending against (a reasoning model
    rejected outright for an assumed-large output that was never
    actually needed). 500 leaves ample headroom for a reasoning
    model's inline chain-of-thought before its final JSON while
    staying well under the range that triggered that rejection.
    """
    if max_tokens is None:
        max_tokens = int(os.getenv("GROQ_NOTIFICATION_GUARD_MAX_OUTPUT_TOKENS", "500"))

    return client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                # Single source of truth for the guard's user-turn
                # prompt structure (including the untrusted-content
                # framing) -- see app.notification_guard.prompt.
                "content": build_prompt(title, description),
            },
        ],
        response_format={
            "type": "json_object",
        },
        max_tokens=max_tokens,
    )


def _raise_if_truncated(response):
    """Same check as app.llm.groq._raise_if_truncated -- the guard
    never had this protection before, despite sharing the exact same
    Groq SDK and the exact same finish_reason=='length' failure mode.
    Added here as part of unifying every provider/rotation on the
    shared app.llm.rotation executor, which is the first point all
    three (Gemini, main-pipeline Groq, guard Groq) go through the same
    classification logic instead of each maintaining its own partial
    copy.
    """
    finish_reason = response.choices[0].finish_reason
    if finish_reason == "length":
        raise rate_limit_tracker.TruncatedResponseError(
            "Completion was cut off by max_tokens before finishing its "
            "JSON (finish_reason='length')."
        )


def _parse_decision(content: str) -> bool:
    data = json.loads(content)
    decision = data.get("decision")
    if decision == "notify":
        return True
    if decision == "do_not_notify":
        return False
    raise ValueError(f"Invalid guard decision: {decision!r}")


def _parse_decision_with_category(content: str, original_category_id: str) -> tuple[bool, str]:
    data = json.loads(content)
    decision = data.get("decision")
    category = data.get("category")

    if decision not in ("notify", "do_not_notify"):
        raise ValueError(f"Invalid guard decision: {decision!r}")

    if decision == "do_not_notify":
        # The category field is meaningless for a suppressed
        # notification -- nothing is delivered under it either way --
        # so it isn't validated here.
        return False, original_category_id

    meta_categories = {p.id for p in arbitration_only_categories()}
    if category not in ({original_category_id} | meta_categories):
        raise ValueError(f"Invalid guard category: {category!r}")

    return True, category


class GroqNotificationGuard(GuardProvider):

    id = "groq"

    def __init__(self, *, clients=None, models=None):
        # Resolve credentials at construction time rather than copying the
        # import-time CLIENTS placeholder. Production composition therefore
        # receives configured clients without relying on a prior monkeypatch.
        self.models = list(NOTIFICATION_GUARD_MODELS if models is None else models)
        self.clients = list(_clients() if clients is None else clients)

        # Kept for compatibility with the existing guard logger. With more
        # than one (client, model) combination this is only a nominal
        # default: the winning candidate's concrete model is written back
        # to `self.model` after every rotate call (see _winning_model_for),
        # so the decision log reports the model that actually produced the
        # decision rather than the first configured one.
        self.model = self.models[0] if self.models else ""

    def _winning_model_for(self, candidate_id: str) -> str:
        """Map a run_with_rotation candidate_id back to its concrete
        model name. Built by replicating _candidates' exact
        key-major/model-minor enumeration (rather than string-splitting
        candidate_id, which is ambiguous because model names
        themselves can contain '-'): ``groq-guard-key{client_index+1}
        -{model}`` -> model."""
        return {
            f"groq-guard-key{client_index + 1}-{model}": model
            for client_index in range(len(self.clients))
            for model in self.models
        }.get(candidate_id, self.model)

    def _candidates(self, thunk_factory):
        """(candidate_id, display_label, thunk) triples, one per
        (client, model) combination, in the usual key-major/model-minor
        order. thunk_factory(client, model) must return a zero-argument
        callable performing that combination's actual request + parse.

        See app.llm.rotation.run_with_rotation for the cooldown-skip
        and failure-classification behavior this now shares with every
        other provider in the codebase (Gemini's key rotation, the
        main pipeline's Groq model rotation) instead of maintaining its
        own separate copy of that logic.
        """
        return [
            (
                f"groq-guard-key{client_index + 1}-{model}",
                f"key #{client_index + 1}, model: {model}",
                thunk_factory(client, model),
            )
            for client_index, client in enumerate(self.clients)
            for model in self.models
        ]

    def evaluate(self, title: str, description: str, system_prompt: str, deadline=None) -> bool:

        def make_thunk(client, model):
            def thunk():
                response = _generate_response(client, model, title, description, system_prompt)
                _raise_if_truncated(response)
                return _parse_decision(response.choices[0].message.content)
            return thunk

        result, candidate_id, _ = run_with_rotation("Groq guard", self._candidates(make_thunk), deadline=deadline)
        self.model = self._winning_model_for(candidate_id)
        return result

    def evaluate_with_category(
        self,
        title: str,
        description: str,
        system_prompt: str,
        original_category_id: str,
        deadline=None,
    ) -> tuple[bool, str]:
        """
        Like evaluate(), but the guard is also allowed to say the job
        is better classified as "full_stack" than the tiering
        system's original single-category match. `system_prompt` here
        is the combined prompt built by app.notification_guard.guard
        (original category's guard_prompt.py + full_stack's), which
        instructs the model to return {"decision": ..., "category":
        ...} instead of just {"decision": ...}.

        Returns (allowed, resolved_category_id). resolved_category_id
        is always either original_category_id or "full_stack" --
        never blindly trusted from the response, since a malformed or
        adversarial value here would otherwise let an untrusted job
        posting redirect delivery to a category the tiering system
        never matched (see the same defense-in-depth pattern in
        app.llm.utils.parse_arbitration_response).
        """

        def make_thunk(client, model):
            def thunk():
                response = _generate_response(client, model, title, description, system_prompt)
                _raise_if_truncated(response)
                return _parse_decision_with_category(
                    response.choices[0].message.content, original_category_id
                )
            return thunk

        result, candidate_id, _ = run_with_rotation("Groq guard", self._candidates(make_thunk), deadline=deadline)
        self.model = self._winning_model_for(candidate_id)
        return result


# Module-level singleton + free functions, kept for backward
# compatibility with existing callers/tests that import
# app.notification_guard.groq.evaluate / evaluate_with_category
# directly (and that monkeypatch app.notification_guard.groq.CLIENTS /
# NOTIFICATION_GUARD_MODELS -- GroqNotificationGuard reads the module-
# level CLIENTS/models list at construction, from `self.models`/
# `self.clients`, so a fresh provider instance picks up changes).
_provider = None


def _provider_instance():
    global _provider
    if _provider is None:
        _provider = GroqNotificationGuard()
    return _provider


def evaluate(title: str, description: str, system_prompt: str, deadline=None) -> bool:
    return _provider_instance().evaluate(title, description, system_prompt, deadline=deadline)


def evaluate_with_category(
    title: str,
    description: str,
    system_prompt: str,
    original_category_id: str,
    deadline=None,
) -> tuple[bool, str]:
    return _provider_instance().evaluate_with_category(
        title, description, system_prompt, original_category_id, deadline=deadline
    )