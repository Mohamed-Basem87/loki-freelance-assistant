"""
CI smoke test (audit finding P2-9).

The previous CI smoke step was `python -c "import run, run_guarded"`,
which only proves the two entrypoint modules are importable -- it
never actually builds the application's runtime, so a broken
composition root, a provider that can't instantiate, an unregistered
category, or a database that can't initialize would only be caught by
a human running the bot for the first time in production.

This script drives the real, non-network parts of startup:

  1. app.composition.compose() -- builds the full dependency graph
     (repository, state, dedup, notification service + guard,
     parser registry, user-messaging surface) exactly as run.py does.
  2. Runtime.initialize_database() -- actually opens/creates the
     SQLite database and runs schema migration, against a throwaway
     temp file (never the real configured database).
  3. Category registry sanity: at least one category is enabled, and
     every configured LLM provider's arbitration prompt inputs are
     structurally well-formed (build_category_arbitration_system_prompt
     succeeds) -- this is "does category arbitration work" in the
     sense the audit means: prompt/candidate wiring, not an actual
     network call to Gemini/Groq (this script intentionally makes no
     external network calls, so it stays safe and fast to run on
     every CI push).

Any exception here is a real startup-composition failure and should
fail CI, exactly like an import error would have -- this is strictly
additive coverage on top of the plain import check, not a replacement
for it (the import check still catches syntax/import errors in the
entrypoints themselves, which this script does not re-verify).

The last check (_verify_guarded_entrypoints_in_fresh_processes) pins
the guarded-entrypoint semantics the previous CI step
`python -c "import run, run_guarded"` silently missed: importing both
entrypoints in ONE interpreter means `import run` caches the
guard-disabled configuration first, so the later `import run_guarded`
set NOTIFICATION_GUARD_ENABLED too late and proved nothing. Each
entrypoint is therefore re-verified in its own fresh subprocess where
the module-level env ordering actually holds.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# CI runs `python scripts/ci_smoke_test.py` from the repo root with no
# PYTHONPATH, so ``scripts/`` (this file's directory) would otherwise be
# the only path entry and ``import app`` would fail. Bootstrap the repo
# root explicitly and idempotently so the script is self-sufficient.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _verify_guarded_entrypoints_in_fresh_processes() -> None:
    """Re-verify the run / run_guarded semantics in isolated fresh
    interpreters (see module docstring for why one-interpreter imports
    could not). Makes no network calls."""
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["NOTIFICATION_GUARD_ENABLED"] = "false"

    def run_py(code, extra_env=None):
        run_env = dict(env)
        if extra_env:
            run_env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(root),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    # 1. run.py stays configuration-driven: false in, false out.
    result = run_py(
        "import run; import app.notification_guard.config as gc; print(gc.NOTIFICATION_GUARD_ENABLED)"
    )
    if result.returncode != 0 or "False" not in result.stdout:
        raise RuntimeError(
            "run.py must respect NOTIFICATION_GUARD_ENABLED=false in a "
            "fresh process: " + result.stdout + result.stderr
        )

    # 2. run_guarded.py forces the guard on even when env says false.
    guard_env = {
        "GROQ_NOTIFICATION_GUARD_API_KEY": "smoke-placeholder",
        "GROQ_NOTIFICATION_GUARD_MODELS": "llama-3.3-70b-versatile",
    }
    result = run_py(
        "import run_guarded; import app.notification_guard.config as gc;"
        "print(gc.NOTIFICATION_GUARD_ENABLED)",
        extra_env=guard_env,
    )
    if result.returncode != 0 or "True" not in result.stdout:
        raise RuntimeError(
            "run_guarded.py must force the guard on in a fresh process: "
            + result.stdout + result.stderr
        )

    # 3. ... and must fail loudly without its required config, never
    #    silently degrade to unguarded delivery. The key is explicitly
    #    blanked even if the ambient environment (e.g. pytest wrapping
    #    this script, or a developer shell exporting it) carries one.
    result = run_py(
        "import run_guarded",
        extra_env={"GROQ_NOTIFICATION_GUARD_API_KEY": ""},
    )
    if result.returncode == 0 or "GROQ_NOTIFICATION_GUARD_API_KEY" not in result.stderr:
        raise RuntimeError(
            "run_guarded.py without GROQ_NOTIFICATION_GUARD_API_KEY must "
            "abort at import: " + result.stdout + result.stderr
        )

    print("[SMOKE] Guarded entrypoint semantics verified in fresh "
          "subprocesses (run respects config; run_guarded forces true "
          "and fails loudly without its required config).")


def _run_smoke_checks() -> None:
    """Run every non-network startup check. DATABASE_FILE_PATH and
    STATE_FILE_PATH are already pointed at the throwaway temp dir by the
    caller. Raises RuntimeError on any failure so main() can fail CI."""
    from app.composition import compose

    runtime = compose()
    print("[SMOKE] compose() succeeded: repository, state, dedup, "
          "notification service, guard, and user-messaging surface "
          "all constructed without error.")

    asyncio.run(runtime.initialize_database())
    print("[SMOKE] Database initialized/migrated successfully at "
          f"{os.environ['DATABASE_FILE_PATH']}.")

    from app.categories.registry import enabled_categories
    categories = enabled_categories()
    if not categories:
        raise RuntimeError("No categories are enabled; nothing could ever be classified.")
    print(f"[SMOKE] {len(categories)} categor(y/ies) enabled: "
          f"{', '.join(c.id for c in categories)}")

    from app.llm.manager import build_category_arbitration_system_prompt
    candidates = [
        {
            "id": profile.id,
            "name": profile.name,
            "description": profile.description,
            "arbitration_context": profile.arbitration_context,
            "result": {"reason": "smoke test", "categories": [], "negative_categories": []},
        }
        for profile in categories
    ]
    prompt = build_category_arbitration_system_prompt(candidates)
    if not prompt or not isinstance(prompt, str):
        raise RuntimeError(
            "build_category_arbitration_system_prompt produced an "
            "empty/invalid prompt"
        )
    print("[SMOKE] Category arbitration prompt wiring is structurally "
          "sound (every enabled category's llm_prompt.py loads and "
          "has a non-empty SYSTEM_PROMPT; no network calls made).")

    _verify_guarded_entrypoints_in_fresh_processes()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # Never touch the real configured DB/state files during a CI
        # smoke run -- point everything at a throwaway temp directory.
        os.environ["DATABASE_FILE_PATH"] = os.path.join(tmp, "smoke.db")
        os.environ["STATE_FILE_PATH"] = os.path.join(tmp, "state.json")

        try:
            _run_smoke_checks()
            print("[SMOKE] All checks passed.")
            return 0
        finally:
            # Close the SQLite connection BEFORE the TemporaryDirectory
            # context manager's __exit__ runs its cleanup -- on success
            # AND on failure. On Windows, a still-open file handle
            # prevents the temp dir deletion (PermissionError: WinError
            # 32), which would otherwise mask the original failure with
            # a confusing cleanup error.
            from app.logger import logger as _db
            _db.close()


if __name__ == "__main__":
    sys.exit(main())
