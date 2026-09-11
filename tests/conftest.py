"""
Test-collection-time environment setup.

app/config.py validates a full set of required production credentials
(Telegram API/session, Gemini, Groq, bot token, target channels,
FreeHub user id) eagerly at import time, and raises RuntimeError if
any of them is missing. That's the right behavior for the *running
bot* -- fail fast rather than start up half-configured -- but most of
this project's own module graph transitively imports app.config just
by importing app.job_processor, app.notifier, app.telegram_bot, etc.,
which meant a large chunk of the test suite (anything touching the
pipeline/LLM/notification layers, not just the classifier itself)
could not even be *collected* by pytest without a full, real .env
file present.

This file does NOT weaken app/config.py's validation, and does NOT
make any required production variable optional at runtime -- the
real app still refuses to start without real credentials. It only
ensures that, for the *test process*, every required variable has
*some* syntactically valid value before any test module gets a chance
to import app.config:

  - If a real .env is present (e.g. a developer's local checkout),
    its values are loaded first and take priority, so tests can still
    exercise real credentials when explicitly asked to (see
    tests/test_llm_gemini.py, tests/test_llm_groq.py,
    tests/test_llm_manager.py for the opt-in "live" tests).
  - Anything still missing after that gets a clearly-fake placeholder
    (never a value that could be mistaken for a real secret), purely
    so importing app.config succeeds. These placeholders are never
    used to make real API/Telegram calls anywhere in the default
    (offline) test suite -- provider calls are mocked (see
    tests/test_llm_gemini.py etc.), and nothing in this test suite
    starts the actual Telegram client or sends a real Telegram
    message.

The classifier's own tests (tests/test_keyword_filter.py) do not
depend on this at all -- app.filters/app.keywords/app.normalize have
no app.config dependency, so they run fully isolated regardless of
anything in this file. This fixture only unblocks the *other* test
files that genuinely need the rest of the app's object graph to
construct (e.g. app.notifier needs BOT_CHAT_ID to exist, even just to
import).
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.llm import rate_limit_tracker

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Load a real .env first, if the developer has one -- load_dotenv()
# does not override variables already present in the environment, and
# nothing below has been set yet, so real values always win when they
# exist.
load_dotenv(_REPO_ROOT / ".env")

# Every variable app/config.py treats as required. Values below are
# deliberately obvious placeholders, not plausible-looking fakes, so
# nobody mistakes one for a leaked credential in a log/diff.
_TEST_ENV_DEFAULTS = {
    "API_ID": "10000000",
    "API_HASH": "test-api-hash-not-a-real-secret",
    "PHONE_NUMBER": "+10000000000",
    "GEMINI_API_KEYS": "test-gemini-key-not-a-real-secret",
    "GROQ_API_KEY": "test-groq-key-not-a-real-secret",
    "BOT_TOKEN": "0000000000:test-bot-token-not-a-real-secret",
    "BOT_CHAT_ID": "10000000",
    "TARGET_CHANNEL_IDS": "-1000000000000",
    "FREEHUB_USER_ID": "test-freehub-user-not-a-real-id",
}

for _name, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True, scope="session")
def _bind_dependency_slots():
    """Bind the legacy DependencyProxy slots before any test runs.

    app.dependencies.DependencyProxy is deliberately strict: application
    code may never lazily construct adapter instances (that was the old
    "lazy fallback factory" design the composition root removed -- see
    app.dependencies). Production binds every slot through
    app.composition.compose() before any worker starts. The test session
    mirrors that contract by binding the same canonical module facades
    up front: app.persistence.db (SQLiteRepository over the shared
    DBLogger singleton), app.state_store.store (JsonStateStore over the
    shared StateManager singleton), the default notification service,
    routing, parser registry, and the standard no-op notification
    resolver. Tests that monkeypatch a proxy attribute (e.g.
    tests/test_telegram_recovery.py) still work because an instance
    attribute shadows the bound value, exactly as before.
    """
    from app.dependencies import configure
    from app.notifier import get_notification_service
    from app.parser import get_parser_registry
    from app.persistence import db
    from app.routing import queue_for_category
    from app.state_store import store

    async def _noop_resolver(job_uuid, row, category_id):
        return category_id

    configure(
        persistence=db,
        state_store=store,
        dedup_store=store,
        notification_service=get_notification_service(),
        routing=queue_for_category,
        parser_registry=get_parser_registry(),
        notification_resolver=_noop_resolver,
    )
    yield


@pytest.fixture(autouse=True)
def _reset_llm_rate_limit_cooldowns():
    """app.llm.rate_limit_tracker (see tests/test_llm_gemini.py,
    tests/test_notification_guard.py) is process-lifetime, in-memory,
    global state by design -- see its own module docstring for why.
    That's the right choice for the running bot, but across a test
    session it means one test's simulated 429/model-not-found failure
    could leave a candidate id "in cooldown" for a later, unrelated
    test that happens to reuse the same positional id (e.g. both
    construct a 2-client fake rotation and both call it "key #1").
    Clearing before *and* after each test keeps that state from ever
    leaking across test boundaries in either direction.
    """
    rate_limit_tracker.clear()
    yield


@pytest.fixture(autouse=True)
def _reset_worker_liveness():
    """app.heartbeat.liveness is process-lifetime, in-memory, global state
    by design (see its own module docstring) -- the running bot's
    healthcheck depends on exactly one shared registry, snapshotted into
    the heartbeat file. Across a test session that means any test which
    exercises real code beating a worker id (e.g. app.logger.DBLogger.run
    / app.state.StateManager.run reporting "db_worker"/"state_worker" dead
    after a real timed-out-operation test) leaves that state sitting in
    the registry for every later test that calls write_heartbeat() or
    otherwise reads the shared registry -- including tests with no
    relationship to DB/state timeouts at all. Clearing before *and* after
    each test keeps that state from ever leaking across test boundaries.
    """
    from app.heartbeat import liveness as _liveness
    with _liveness._lock:
        _liveness._states.clear()
    yield
    with _liveness._lock:
        _liveness._states.clear()
    rate_limit_tracker.clear()
