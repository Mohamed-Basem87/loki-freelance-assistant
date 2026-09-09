

def test_category_discovery_propagates_broken_dependency(monkeypatch):
    import importlib
    import pkgutil
    from app.categories import registry
    registry._discover_profiles.cache_clear()
    monkeypatch.setattr(pkgutil, "iter_modules", lambda path: [type("M", (), {"name": "broken"})()])

    real_import = importlib.import_module
    def fake_import(name):
        if name == "app.categories.broken.profile":
            raise ModuleNotFoundError("missing dependency", name="third_party_dependency")
        return real_import(name)
    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    with __import__('pytest').raises(ModuleNotFoundError, match="missing dependency"):
        registry._discover_profiles()
    registry._discover_profiles.cache_clear()


def test_category_discovery_fails_on_duplicate_category_id(monkeypatch):
    """If two category modules declare the same category ID, discovery must fail fast."""
    import importlib
    import pkgutil
    from app.categories import registry

    registry._discover_profiles.cache_clear()

    # Create two fake modules with the same category ID
    class FakeModule1:
        class PROFILE:
            id = "duplicate_id"
            name = "Duplicate 1"

    class FakeModule2:
        class PROFILE:
            id = "duplicate_id"
            name = "Duplicate 2"

    def fake_iter_modules(path):
        return [
            type("M", (), {"name": "dup1"})(),
            type("M", (), {"name": "dup2"})(),
        ]

    real_import = importlib.import_module

    def fake_import(name):
        if name == "app.categories.dup1.profile":
            return FakeModule1()
        if name == "app.categories.dup2.profile":
            return FakeModule2()
        if name == "app.categories":
            # Return a fake package for the categories package
            class FakePackage:
                __path__ = []
            return FakePackage()
        # Don't call importlib.import_module for other names to avoid recursion
        raise ModuleNotFoundError(f"fake import for {name}")

    monkeypatch.setattr(pkgutil, "iter_modules", fake_iter_modules)
    monkeypatch.setattr(registry.importlib, "import_module", fake_import)

    with __import__('pytest').raises(RuntimeError, match="Duplicate category ID 'duplicate_id'"):
        registry._discover_profiles()

    registry._discover_profiles.cache_clear()
