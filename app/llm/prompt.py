"""Backward-compatible prompt module.

The category-aware LLM path now receives prompt content explicitly.
This module remains only as a compatibility import for external callers.
"""

from app.categories.data_analysis.llm_prompt import SYSTEM_PROMPT

__all__ = ["SYSTEM_PROMPT"]
