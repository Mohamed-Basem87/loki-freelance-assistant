from .data_analysis.profile import PROFILE as DATA_ANALYSIS_PROFILE
from .game_dev.profile import PROFILE as GAME_DEV_PROFILE
from .mobile_app.profile import PROFILE as MOBILE_APP_PROFILE
from .frontend.profile import PROFILE as FRONTEND_PROFILE
from .backend.profile import PROFILE as BACKEND_PROFILE
from .ai_ml.profile import PROFILE as AI_ML_PROFILE
from .full_stack.profile import PROFILE as FULL_STACK_PROFILE

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
    FULL_STACK_PROFILE.id: FULL_STACK_PROFILE,
}

def get_category(category_id):
    return CATEGORY_PROFILES.get(category_id)


def enabled_categories():
    """Return categories exposed to routing, subscriptions, and the UI."""
    return tuple(
        profile for profile in CATEGORY_PROFILES.values()
        if getattr(profile, "enabled", True)
    )


def deterministic_categories():
    """Return categories that participate in deterministic keyword scoring."""
    return tuple(
        profile
        for profile in enabled_categories()
        if not getattr(profile, "arbitration_only", False)
    )


def arbitration_only_categories():
    """Return registered categories that can only be selected by arbitration.

    This deliberately ignores the subscriber-facing enabled flag: an
    arbitration-only category can be evaluated during shadow validation while
    remaining hidden from the subscription UI.
    """
    return tuple(
        profile
        for profile in CATEGORY_PROFILES.values()
        if getattr(profile, "arbitration_only", False)
    )