"""
Regression tests for audit finding P2-3 (run_guarded.py must actually
force the Notification Guard on) and P2-9 (the CI smoke script must
prove startup composition, not just importability).

These must run in a real subprocess (not just monkeypatched imports)
because app.notification_guard.config reads NOTIFICATION_GUARD_ENABLED
once at *import time* -- the only way to genuinely verify run_guarded.py
sets it before that import happens is a fresh interpreter, and the only
way to genuinely verify run.py stays configuration-driven is the same.

The previous CI step `python -c "import run, run_guarded"` flaws are
specifically pinned here:
  * run_guarded.py is imported in its OWN fresh process (the real file,
    not a byte-mirror of its top-of-file side effect), where the
    module's env-ordering actually holds.
  * import run_guarded without GROQ_NOTIFICATION_GUARD_API_KEY must
    fail loudly at import (the guard's own validate()), never silently
    degrade to unguarded delivery.
  * run.py with NOTIFICATION_GUARD_ENABLED=false must stay disabled in
    a fresh process (no hidden force).
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _subprocess_script(source: str, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run `source` in a fresh interpreter from the repo root with the
    given env, and return the completed process (caller asserts)."""
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=str(REPO_ROOT),
        env=env_overrides,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _base_env():
    import os

    env = dict(os.environ)
    env.pop("NOTIFICATION_GUARD_ENABLED", None)
    # These tests only care about NOTIFICATION_GUARD_ENABLED, so satisfy
    # the guard's other required configuration in every guarded case
    # (both entrypoints must be able to import cleanly).
    env["GROQ_NOTIFICATION_GUARD_API_KEY"] = "test-key"
    env["GROQ_NOTIFICATION_GUARD_MODELS"] = "llama-3.3-70b-versatile"
    return env


def _guard_enabled_after_import(source: str, env) -> str:
    """Import source's entrypoint then report the guard flag the way
    app.notification_guard.config sees it at import time."""
    script = (
        source
        + "\nimport app.notification_guard.config as guard_config\n"
        "print('RESULT=' + str(guard_config.NOTIFICATION_GUARD_ENABLED))\n"
    )
    result = _subprocess_script(script, env)
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("RESULT="):
            return line.split("=", 1)[1]
    raise AssertionError(f"no RESULT line in output: {result.stdout!r} / {result.stderr!r}")


def test_run_guarded_forces_the_guard_on_even_if_env_says_false():
    """The real run_guarded.py, imported in a fresh process with
    NOTIFICATION_GUARD_ENABLED=false, must end up with the guard on --
    this is the actual report of what importing the file does, not a
    hand-mirrored copy of its side effect."""
    env = _base_env()
    env["NOTIFICATION_GUARD_ENABLED"] = "false"
    assert _guard_enabled_after_import("import run_guarded", env) == "True"


def test_plain_run_stays_configuration_driven():
    """run.py has no override: in a fresh process with the env set to
    false, the guard must be off exactly as configured."""
    env = _base_env()
    env["NOTIFICATION_GUARD_ENABLED"] = "false"
    assert _guard_enabled_after_import("import run", env) == "False"


def test_run_guarded_import_fails_loudly_without_guard_api_key():
    """run_guarded.py forces the guard on before importing app.bot; the
    guard's own validate() must then abort at import when its required
    Groq API key is missing -- never a silent, unguarded startup."""
    import os

    env = dict(os.environ)
    env.pop("NOTIFICATION_GUARD_ENABLED", None)
    env["NOTIFICATION_GUARD_ENABLED"] = "false"
    env.pop("GROQ_NOTIFICATION_GUARD_API_KEY", None)

    result = _subprocess_script("import run_guarded\n", env)

    assert result.returncode != 0, (
        "import run_guarded without GROQ_NOTIFICATION_GUARD_API_KEY must "
        f"fail at import; got success with stdout={result.stdout!r}"
    )
    assert "GROQ_NOTIFICATION_GUARD_API_KEY" in result.stderr, result.stderr


def test_ci_smoke_script_runs_clean(tmp_path):
    """Regression test for audit finding P2-9: scripts/ci_smoke_test.py
    (which CI now runs) must actually succeed -- this exercises the
    same real composition/database/category-arbitration-wiring and
    fresh-process entrypoint-semantics checks from within the test
    suite itself, not only from CI's own YAML, so `pytest` alone
    catches a startup-composition regression.
    """
    env = _base_env()
    env["NOTIFICATION_GUARD_ENABLED"] = "false"
    env["BOT_TOKEN"] = "123456789:test-placeholder"
    env["FREEHUB_USER_ID"] = "test-placeholder"

    # No PYTHONPATH is set -- CI runs `python scripts/ci_smoke_test.py`
    # from the repo root with no PYTHONPATH, so this pins the script's
    # own repo-root sys.path bootstrap rather than masking a missing
    # one behind the test harness.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci_smoke_test.py")],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[SMOKE] All checks passed." in result.stdout