from app.categories.data_analysis.profile import PROFILE as DATA_ANALYSIS_PROFILE


# Every enabled category is evaluated independently for every incoming job.
# Adding a category means registering its CategoryProfile here; the shared
# classification/LLM/notification machinery remains unchanged.
CATEGORIES = {
    DATA_ANALYSIS_PROFILE.id: DATA_ANALYSIS_PROFILE,
}


def get_categories():
    """Return all enabled category profiles in deterministic order."""
    return tuple(CATEGORIES.values())


def get_category(category_id: str = "data_analysis"):
    try:
        return CATEGORIES[category_id]
    except KeyError as exc:
        raise ValueError(f"Unknown category: {category_id!r}") from exc
