"""Regression test for audit finding: configuration consistency.

app/healthcheck.py previously read DATABASE_FILE_PATH, STATE_FILE_PATH,
and HEARTBEAT_FILE_PATH directly from the environment with its own
hardcoded absolute defaults, bypassing runtime_config._resolve_path().
A relative override (e.g. the literal values documented in
.env.example) could then resolve differently for the running
application (against BASE_DIR) than for the healthcheck subprocess
(against its own working directory, with no BASE_DIR applied) --
"one interpretation in the runtime and another in the healthcheck".
"""
from pathlib import Path

from app.runtime_config import RUNTIME


def test_healthcheck_reads_paths_from_the_same_resolved_runtime_config():
    import app.healthcheck as healthcheck
    import inspect

    source = inspect.getsource(healthcheck.main)
    code_lines = [
        line for line in source.splitlines() if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert "RUNTIME.database_file_path" in code_only
    assert "RUNTIME.state_file_path" in code_only
    assert "RUNTIME.heartbeat_file_path" in code_only
    assert "os.getenv(" not in code_only, (
        "healthcheck.main() must not re-read these paths from the "
        "environment directly -- that reintroduces the two-"
        "interpretations bug even if the defaults happen to match "
        "today"
    )


def test_heartbeat_writes_to_the_same_path_healthcheck_reads():
    import app.heartbeat as heartbeat

    assert heartbeat.HEARTBEAT_FILE_PATH == Path(RUNTIME.heartbeat_file_path)


def test_a_relative_override_resolves_against_base_dir_not_cwd():
    """Exercises _resolve_path directly with the exact example value
    .env.example documents, independent of whatever the real process
    environment happens to have set."""
    from app.runtime_config import _resolve_path, BASE_DIR

    resolved = _resolve_path("loki_freelance_bot.db")
    assert resolved == str(BASE_DIR / "loki_freelance_bot.db")
    assert Path(resolved).is_absolute()
