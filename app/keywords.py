"""Backward-compatible exports for the active Data Analysis category.

Category-specific vocabulary now lives under app.categories.
This module remains as a compatibility surface for existing imports.
"""
from app.categories.data_analysis.keywords import (
    POSITIVE_KEYWORDS,
    NEGATIVE_KEYWORDS,
    HARD_REJECT_KEYWORDS,
    NOISE_KEYWORDS,
)

__all__ = [
    "POSITIVE_KEYWORDS",
    "NEGATIVE_KEYWORDS",
    "HARD_REJECT_KEYWORDS",
    "NOISE_KEYWORDS",
]
