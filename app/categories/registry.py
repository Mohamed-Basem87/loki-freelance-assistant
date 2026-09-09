"""Category registry and discovery.

Category packages expose `profile.PROFILE`. The registry discovers them rather
than embedding a list of concrete categories in core code.
"""
import importlib
import pkgutil
from functools import lru_cache


@lru_cache(maxsize=1)
def _discover_profiles():
    package = importlib.import_module("app.categories")
    profiles = {}
    seen_ids = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        name = module_info.name
        if name.startswith("_") or name in {"profile", "registry"}:
            continue
        profile_module = f"app.categories.{name}.profile"
        try:
            module = importlib.import_module(profile_module)
            profile = getattr(module, "PROFILE", None)
        except ModuleNotFoundError as exc:
            # A genuinely absent optional profile module is ignorable; a
            # dependency imported by an existing profile is an actionable
            # deployment error and must not masquerade as a missing category.
            if exc.name == profile_module:
                continue
            raise
        except ImportError:
            raise
        if profile is not None:
            if profile.id in seen_ids:
                first_module = seen_ids[profile.id]
                raise RuntimeError(
                    f"Duplicate category ID '{profile.id}' declared by "
                    f"modules '{first_module}' and '{name}'. "
                    f"Category IDs must be unique."
                )
            seen_ids[profile.id] = name
            profiles[profile.id] = profile
    return profiles


class CategoryRegistry:
    def __init__(self, profiles=None):
        self._profiles = dict(profiles) if profiles is not None else dict(_discover_profiles())

    def get(self, category_id):
        return self._profiles.get(category_id)

    def all(self):
        return tuple(self._profiles.values())

    def enabled(self):
        return tuple(p for p in self.all() if getattr(p, "enabled", True))

    def deterministic(self):
        return tuple(p for p in self.enabled() if not getattr(p, "arbitration_only", False))

    def arbitration_only(self):
        return tuple(p for p in self.enabled() if getattr(p, "arbitration_only", False))


_registry = CategoryRegistry()
CATEGORY_PROFILES = _registry._profiles


def get_category(category_id):
    return _registry.get(category_id)


def enabled_categories():
    return _registry.enabled()


def deterministic_categories():
    return _registry.deterministic()


def arbitration_only_categories():
    return _registry.arbitration_only()
