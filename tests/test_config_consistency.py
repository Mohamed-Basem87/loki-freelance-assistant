"""Regression test for audit finding: configuration consistency.

app/core/healthcheck.py previously read DATABASE_FILE_PATH, STATE_FILE_PATH,
and HEARTBEAT_FILE_PATH directly from the environment with its own
hardcoded absolute defaults, bypassing runtime_config._resolve_path().
A relative override (e.g. the literal values documented in
.env.example) could then resolve differently for the running
application (against BASE_DIR) than for the healthcheck subprocess
(against its own working directory, with no BASE_DIR applied) --
"one interpretation in the runtime and another in the healthcheck".

Persistence is Postgres+Redis now (DATABASE_URL/REDIS_URL), not a
SQLite file path -- those two are connection strings, not filesystem
paths, so there is no BASE_DIR-vs-cwd ambiguity for them to inherit,
and app.wiring.composition.compose() reads them the same direct
`os.getenv(...)` way. The invariant this test guards is narrower now:
the two genuinely filesystem-path configs (state/heartbeat) must still
go through RUNTIME's resolved paths, not a bare os.getenv() re-read.
"""
from pathlib import Path

from app.infra.runtime_config import RUNTIME


def test_healthcheck_reads_paths_from_the_same_resolved_runtime_config():
    import app.infra.healthcheck as healthcheck
    import inspect

    source = inspect.getsource(healthcheck.main)
    code_lines = [
        line for line in source.splitlines() if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert "RUNTIME.state_file_path" in code_only
    assert "RUNTIME.heartbeat_file_path" in code_only
    assert 'os.getenv("DATABASE_URL")' in code_only
    assert 'os.getenv("REDIS_URL")' in code_only
    for var in ("STATE_FILE_PATH", "HEARTBEAT_FILE_PATH", "DATABASE_FILE_PATH"):
        assert f'os.getenv("{var}")' not in code_only, (
            f"healthcheck.main() must not re-read {var} from the "
            "environment directly -- that reintroduces the two-"
            "interpretations bug even if the defaults happen to match "
            "today"
        )


def test_heartbeat_writes_to_the_same_path_healthcheck_reads():
    import app.infra.heartbeat as heartbeat

    assert heartbeat.HEARTBEAT_FILE_PATH == Path(RUNTIME.heartbeat_file_path)


def test_a_relative_override_resolves_against_base_dir_not_cwd():
    """Exercises _resolve_path directly with the exact example value
    .env.example documents, independent of whatever the real process
    environment happens to have set."""
    from app.infra.runtime_config import _resolve_path, BASE_DIR

    resolved = _resolve_path("loki_freelance_bot.db")
    assert resolved == str(BASE_DIR / "loki_freelance_bot.db")
    assert Path(resolved).is_absolute()
