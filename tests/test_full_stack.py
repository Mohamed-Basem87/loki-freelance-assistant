"""Tests for the full_stack category implementation."""

import json

import pytest

from app.categories.registry import enabled_categories, get_category
from app.categories.full_stack.keywords import (
    POSITIVE_KEYWORDS,
    NEGATIVE_KEYWORDS,
    HARD_REJECT_KEYWORDS,
    NOISE_KEYWORDS,
)
from app.categories.full_stack.profile import PROFILE as FULL_STACK_PROFILE
from app.llm.utils import parse_arbitration_response, build_arbitration_prompt
from app.llm.manager import (
    build_category_arbitration_system_prompt,
    build_compact_arbitration_system_prompt,
)


def test_full_stack_registered():
    """Full stack category is registered and enabled."""
    profiles = enabled_categories()
    ids = [p.id for p in profiles]
    assert "full_stack" in ids
    profile = get_category("full_stack")
    assert profile is not None
    assert profile.id == "full_stack"
    assert profile.name == "Full Stack Development"


def test_full_stack_has_empty_deterministic_vocabulary():
    """Full stack has no deterministic keywords - cannot be selected by keyword classifier."""
    assert POSITIVE_KEYWORDS == {}
    assert NEGATIVE_KEYWORDS == {}
    assert HARD_REJECT_KEYWORDS == set()
    assert NOISE_KEYWORDS == set()


def test_full_stack_profile_structure():
    """Full stack profile has all required fields."""
    assert FULL_STACK_PROFILE.id == "full_stack"
    assert FULL_STACK_PROFILE.name == "Full Stack Development"
    assert FULL_STACK_PROFILE.description
    assert FULL_STACK_PROFILE.arbitration_context
    assert FULL_STACK_PROFILE.guard_prompt_module == "app.categories.full_stack.guard_prompt"
    assert FULL_STACK_PROFILE.positive_keywords == {}
    assert FULL_STACK_PROFILE.negative_keywords == {}
    assert FULL_STACK_PROFILE.hard_reject_keywords == set()


def test_full_stack_cannot_be_direct_match():
    """Full stack cannot be selected by deterministic classification (no keywords)."""
    from app.classification import classify_and_select
    
    # Even with full-stack related text, no deterministic match should occur
    result = classify_and_select(
        "Build a complete SaaS product with React frontend, Node.js backend, PostgreSQL database, Docker deployment",
        title="Full Stack SaaS Product",
    )
    # Full stack has no keywords, so it should never be a direct match
    assert result["category_id"] != "full_stack"
    assert result["has_direct_match"] is False or result["category_id"] != "full_stack"


def test_arbitration_parser_accepts_full_stack():
    """Arbitration response parser accepts 'full_stack' as valid outcome."""
    raw = json.dumps({
        "selected_category": "full_stack",
        "confidence": 85,
        "reason": "Primary deliverable is a complete new product spanning frontend, backend, database, and deployment.",
    })
    # Parser should accept full_stack even though it's not in deterministic candidates
    result = parse_arbitration_response(raw, {"data_analysis", "backend", "frontend"})
    assert result["selected_category"] == "full_stack"


def test_arbitration_parser_rejects_unknown_category():
    """Arbitration response parser still rejects arbitrary unknown categories."""
    raw = json.dumps({
        "selected_category": "unknown_category",
        "confidence": 85,
        "reason": "Test",
    })
    with pytest.raises(ValueError, match="Invalid arbitrated category"):
        parse_arbitration_response(raw, {"data_analysis", "backend"})


def test_arbitration_prompt_includes_full_stack():
    """Arbitration prompt mentions full_stack as valid outcome."""
    candidates = [
        {
            "id": "backend",
            "name": "Backend Development",
            "description": "Backend services.",
            "arbitration_context": "Primary deliverable is backend work.",
            "result": {"reason": "mixed signals", "categories": ["api"], "negative_categories": []},
        },
    ]
    prompt = build_arbitration_prompt("Build a SaaS product", candidates)
    assert "full_stack" in prompt
    assert "or \"full_stack\"" in prompt


def test_gemini_arbitration_system_prompt_includes_full_stack():
    """Gemini arbitration system prompt allows full_stack as outcome."""
    candidates = [
        {
            "id": "backend",
            "name": "Backend Development",
            "description": "Backend services.",
            "arbitration_context": "Primary deliverable is backend work.",
            "result": {"reason": "mixed signals", "categories": ["api"], "negative_categories": []},
        },
    ]
    prompt = build_category_arbitration_system_prompt(candidates)
    assert "full_stack" in prompt
    assert "or \"full_stack\"" in prompt


def test_groq_compact_arbitration_prompt_includes_full_stack():
    """Groq compact arbitration prompt allows full_stack as outcome."""
    candidates = [
        {
            "id": "backend",
            "name": "Backend Development",
            "description": "Backend services.",
            "arbitration_context": "Primary deliverable is backend work.",
        },
    ]
    prompt = build_compact_arbitration_system_prompt(candidates)
    assert "full_stack" in prompt
    assert "or \"full_stack\"" in prompt


def test_full_stack_llm_prompt_exists():
    """Full stack has an LLM arbitration prompt."""
    from app.categories.full_stack.llm_prompt import SYSTEM_PROMPT
    assert SYSTEM_PROMPT
    assert "Full Stack Development" in SYSTEM_PROMPT
    assert "PRIMARY DELIVERABLE" in SYSTEM_PROMPT
    assert "ACCEPT" in SYSTEM_PROMPT
    assert "REJECT" in SYSTEM_PROMPT


def test_full_stack_guard_prompt_exists():
    """Full stack has a notification guard prompt."""
    from app.categories.full_stack.guard_prompt import SYSTEM_PROMPT
    assert SYSTEM_PROMPT
    assert "Full Stack" in SYSTEM_PROMPT
    assert "notify" in SYSTEM_PROMPT
    assert "do_not_notify" in SYSTEM_PROMPT


def test_full_stack_keywords_do_not_collide_with_noise():
    """Full stack noise keywords don't collide with scored keywords (vacuously true)."""
    from app.categories.full_stack.keywords import _all_scored_keywords
    scored = set(_all_scored_keywords())
    assert scored == set()  # No scored keywords at all
    assert NOISE_KEYWORDS.intersection(scored) == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])