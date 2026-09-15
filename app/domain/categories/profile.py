"""Shared category metadata and classification policy.

Category packages should only declare data. Core orchestration must not
know category IDs or category-specific implementation details.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ClassificationPolicy:
    supporting_positive_min_for_gemini: int = 12
    supporting_negative_downgrade_threshold: int = 14
    min_supporting_positive_for_lone_core: int = 5
    title_positive_supporting_negative_threshold: int = 10


@dataclass(frozen=True)
class CategoryProfile:
    id: str
    name: str
    description: str
    arbitration_context: str
    positive_keywords: dict
    negative_keywords: dict
    hard_reject_keywords: set
    guard_prompt_module: str
    arbitration_only: bool = False
    enabled: bool = True
    classification_policy: ClassificationPolicy = ClassificationPolicy()
    # arbitration_role distinguishes the primary arbitration-only category
    # (full_stack) from potential future arbitration-only categories.
    # Only one category should have arbitration_role="primary".
    # This makes the architecture honest instead of assuming full_stack is
    # the "first" arbitration-only category.
    arbitration_role: str = ""

    @property
    def supporting_positive_min_for_gemini(self) -> int:
        return self.classification_policy.supporting_positive_min_for_gemini

    @property
    def supporting_negative_downgrade_threshold(self) -> int:
        return self.classification_policy.supporting_negative_downgrade_threshold

    @property
    def min_supporting_positive_for_lone_core(self) -> int:
        return self.classification_policy.min_supporting_positive_for_lone_core

    @property
    def title_positive_supporting_negative_threshold(self) -> int:
        return self.classification_policy.title_positive_supporting_negative_threshold
