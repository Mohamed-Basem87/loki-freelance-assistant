from .data_analysis.profile import PROFILE as DATA_ANALYSIS_PROFILE
from .game_dev.profile import PROFILE as GAME_DEV_PROFILE
from .mobile_app.profile import PROFILE as MOBILE_APP_PROFILE
from .frontend.profile import PROFILE as FRONTEND_PROFILE
from .backend.profile import PROFILE as BACKEND_PROFILE
from .ai_ml.profile import PROFILE as AI_ML_PROFILE

# The registry is the only place the active category set is declared.
# Adding a category later means adding its profile here; the shared
# classifier does not need category-specific branches.
CATEGORY_PROFILES = {
    DATA_ANALYSIS_PROFILE.id: DATA_ANALYSIS_PROFILE,
    GAME_DEV_PROFILE.id: GAME_DEV_PROFILE,
    MOBILE_APP_PROFILE.id: MOBILE_APP_PROFILE,
    FRONTEND_PROFILE.id: FRONTEND_PROFILE,
    BACKEND_PROFILE.id: BACKEND_PROFILE,
    AI_ML_PROFILE.id: AI_ML_PROFILE,
}

def get_category(category_id):
    return CATEGORY_PROFILES.get(category_id)

def enabled_categories():
    return tuple(CATEGORY_PROFILES.values())
