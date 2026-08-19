from .data_analysis.profile import PROFILE as DATA_ANALYSIS_PROFILE

# The registry is the only place the active category set is declared.
# Adding a category later means adding its profile here; the shared
# classifier does not need category-specific branches.
CATEGORY_PROFILES = {
    DATA_ANALYSIS_PROFILE.id: DATA_ANALYSIS_PROFILE,
}

def get_category(category_id):
    return CATEGORY_PROFILES.get(category_id)

def enabled_categories():
    return tuple(CATEGORY_PROFILES.values())
