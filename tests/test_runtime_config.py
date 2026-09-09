"""
Regression tests for app.runtime_config's startup validation/warnings.
"""

from app.runtime_config import _warn_if_freehub_endpoint_is_plaintext


def test_warns_on_plaintext_freehub_endpoint(capsys):
    """Audit finding P1-5: a plain-HTTP FreeHub endpoint must produce
    an explicit, visible startup warning documenting the trust
    boundary, since the upstream cannot unilaterally be forced onto
    HTTPS from this codebase alone.
    """
    warned = _warn_if_freehub_endpoint_is_plaintext(
        "http://ec2-51-21-119-160.eu-north-1.compute.amazonaws.com/v1/users"
    )
    captured = capsys.readouterr()

    assert warned is True
    assert "CONFIG WARNING" in captured.out
    assert "HTTP" in captured.out


def test_no_warning_for_https_freehub_endpoint(capsys):
    warned = _warn_if_freehub_endpoint_is_plaintext("https://freehub.example.com/v1/users")
    captured = capsys.readouterr()

    assert warned is False
    assert captured.out == ""


def test_llm_providers_follow_llm_providers_selector_order():
    """LLM_PROVIDERS is deployment *order*, not a membership filter: a
    `groq,gemini` selector must yield groq before gemini even though
    config/project.json lists gemini first -- the provider chain is
    tried in this order (app.llm.manager), so ignoring it silently
    reverses the intended fallback priority."""
    result = _import_runtime_config_with_env(
        {"LLM_PROVIDERS": "groq,gemini"},
        script=(
            "import app.runtime_config as rc\n"
            "print('IDS=' + ','.join(p.provider_id for p in rc.LLM_PROVIDERS))\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "IDS=groq,gemini" in result.stdout


def test_llm_providers_default_to_project_json_order():
    """Without a selector, the JSON baseline order is the provider order."""
    result = _import_runtime_config_with_env(
        {},
        script=(
            "import app.runtime_config as rc\n"
            "print('IDS=' + ','.join(p.provider_id for p in rc.LLM_PROVIDERS))\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "IDS=gemini,groq" in result.stdout


def test_llm_providers_selector_deduplicates_while_preserving_order():
    """A duplicate selector entry must not yield a duplicate provider; the
    first occurrence's position wins (same behavior as the old
    JSON-scan dedup, but now order-aware)."""
    result = _import_runtime_config_with_env(
        {"LLM_PROVIDERS": "gemini,groq,gemini"},
        script=(
            "import app.runtime_config as rc\n"
            "print('IDS=' + ','.join(p.provider_id for p in rc.LLM_PROVIDERS))\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "IDS=gemini,groq" in result.stdout


def test_llm_providers_selector_rejects_unknown_provider():
    result = _import_runtime_config_with_env({"LLM_PROVIDERS": "gemini,unknown_provider"})
    assert result.returncode != 0, "import must reject an unknown LLM provider"
    assert "LLM_PROVIDERS contains unknown provider" in result.stderr


def test_relative_configured_paths_are_resolved_against_base_dir(monkeypatch):
    """Regression test for audit finding P2-6: a relative
    DATABASE_FILE_PATH/STATE_FILE_PATH (exactly the style of value
    .env.example itself documents) must resolve against BASE_DIR, not
    the process's current working directory, so the effective path is
    the same no matter where the process is launched from.
    """
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    env = dict(__import__("os").environ)
    env["DATABASE_FILE_PATH"] = "loki_freelance_bot.db"
    env["STATE_FILE_PATH"] = "database/state.json"
    env["PYTHONPATH"] = str(repo_root)

    script = (
        "from app.runtime_config import RUNTIME, BASE_DIR\n"
        "print('DB=' + RUNTIME.database_file_path)\n"
        "print('STATE=' + RUNTIME.state_file_path)\n"
        "print('BASE=' + str(BASE_DIR))\n"
    )
    # Run from a directory other than the repo root to prove the
    # resolved path does not depend on cwd. tempfile.gettempdir() keeps
    # the test portable (a bare "/tmp" is invalid on Windows).
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith(("DB=", "STATE=", "BASE="))
    )

    assert lines["DB"] == str(Path(lines["BASE"]) / "loki_freelance_bot.db")
    assert lines["STATE"] == str(Path(lines["BASE"]) / "database" / "state.json")
    assert Path(lines["DB"]).is_absolute(), "resolved database path must be absolute"
    assert Path(lines["STATE"]).is_absolute(), "resolved state path must be absolute"


def _import_runtime_config_with_env(env_overrides, script=""):
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.update(env_overrides)
    env["PYTHONPATH"] = str(repo_root)

    code = (
        "import app.runtime_config\n"
        + script
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_non_positive_heartbeat_interval_is_rejected_at_import():
    """Regression test for the heartbeat-validation gap: heartbeat
    timings were the only numeric runtime knobs skipped by the
    positive-validation loop. A zero/negative interval must fail
    startup validation just like every other numeric knob."""
    result = _import_runtime_config_with_env({"HEARTBEAT_INTERVAL_SECONDS": "0"})
    assert result.returncode != 0, "import must fail on a non-positive interval"
    assert "heartbeat_interval_seconds must be > 0" in result.stderr


def test_non_positive_heartbeat_max_age_is_rejected_at_import():
    result = _import_runtime_config_with_env({"HEARTBEAT_MAX_AGE_SECONDS": "-5"})
    assert result.returncode != 0, "import must fail on a non-positive max age"
    assert "heartbeat_max_age_seconds must be > 0" in result.stderr


def test_heartbeat_max_age_below_interval_is_rejected_at_import():
    """The healthcheck flags the runtime as stale once the newest beat
    is older than max_age. With max_age < interval, a perfectly healthy
    runtime (beating exactly on schedule) is inevitably reported stale
    at the tail of every interval -- the configuration cannot
    distinguish a live process from a dead one, so it must be rejected
    at startup."""
    result = _import_runtime_config_with_env(
        {
            "HEARTBEAT_MAX_AGE_SECONDS": "10",
            "HEARTBEAT_INTERVAL_SECONDS": "15",
        }
    )
    assert result.returncode != 0, "import must reject max_age < interval"
    assert "heartbeat_max_age_seconds must be >= heartbeat_interval_seconds" in result.stderr


def test_valid_heartbeat_timings_import_cleanly():
    result = _import_runtime_config_with_env(
        {
            "HEARTBEAT_INTERVAL_SECONDS": "5",
            "HEARTBEAT_MAX_AGE_SECONDS": "30",
        },
        script="print('OK=' + str(app.runtime_config.RUNTIME.heartbeat_interval_seconds))\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK=5.0" in result.stdout


def test_external_call_timeout_below_http_timeout_is_rejected_at_import():
    """P3-A: EXTERNAL_CALL_TIMEOUT_SECONDS is the hard deadline on
    calls that have no HTTP layer of their own (Telethon RPCs, thread-
    hosted LLM SDK calls). A value below HTTP_TIMEOUT_SECONDS would be
    the weaker bound on exactly the calls that need the longer one, so
    startup must reject it instead of silently accepting nonsense."""
    result = _import_runtime_config_with_env(
        {
            "HTTP_TIMEOUT_SECONDS": "30",
            "EXTERNAL_CALL_TIMEOUT_SECONDS": "20",
        }
    )
    assert result.returncode != 0, "import must reject external_timeout < http_timeout"
    assert (
        "external_call_timeout_seconds must be >= http_timeout_seconds"
        in result.stderr
    )


def test_external_call_timeout_equal_to_http_timeout_imports_cleanly():
    """An external-call deadline equal to the HTTP deadline is the valid
    edge of the invariant (not strictly greater)."""
    result = _import_runtime_config_with_env(
        {
            "HTTP_TIMEOUT_SECONDS": "30",
            "EXTERNAL_CALL_TIMEOUT_SECONDS": "30",
        },
        script="print('OK=' + str(app.runtime_config.RUNTIME.external_call_timeout_seconds))\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK=30" in result.stdout


def test_external_call_timeout_above_http_timeout_imports_cleanly():
    """The default relationship (external 60s >= http 30s) remains valid."""
    result = _import_runtime_config_with_env(
        {
            "HTTP_TIMEOUT_SECONDS": "30",
            "EXTERNAL_CALL_TIMEOUT_SECONDS": "100",
        },
        script="print('OK=' + str(app.runtime_config.RUNTIME.external_call_timeout_seconds))\n",
    )
    assert result.returncode == 0, result.stderr
    assert "OK=100" in result.stdout


def test_default_heartbeat_timings_satisfy_the_key_invariants():
    """Defaults must keep the validation invariants so a future
    project.json change can't silently violate them (the same checks
    that now run at import)."""
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root)

    code = (
        "from app.runtime_config import RUNTIME\n"
        "interval = RUNTIME.heartbeat_interval_seconds\n"
        "max_age = RUNTIME.heartbeat_max_age_seconds\n"
        "print('I=', interval, 'M=', max_age)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parts = result.stdout.split()
    interval = float(parts[parts.index("I=") + 1])
    max_age = float(parts[parts.index("M=") + 1])
    assert interval > 0
    assert max_age > 0
    assert max_age >= interval
