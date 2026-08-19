from dataclasses import dataclass
from types import ModuleType


@dataclass(frozen=True)
class CategoryProfile:
    """Domain-specific configuration consumed by Loki's shared engines."""

    id: str
    name: str
    keywords: ModuleType
    llm_prompt: ModuleType
    guard_prompt: ModuleType
