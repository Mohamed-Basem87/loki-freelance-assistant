"""
Test-collection-time environment setup.

app/core/config.py validates a full set of required production credentials
(Telegram API/session, Gemini, Groq, bot token, target channels,
FreeHub user id) eagerly at import time, and raises RuntimeError if
any of them is missing. That's the right behavior for the *running
bot* -- fail fast rather than start up half-configured -- but most of
this project's own module graph transitively imports app.infra.config just
by importing app.services.job_processor, app.services.notifier, app.services.user_bot, etc.,
which meant a large chunk of the test suite (anything touching the
pipeline/LLM/notification layers, not just the classifier itself)
could not even be *collected* by pytest without a full, real .env
file present.

This file does NOT weaken app/core/config.py's validation, and does NOT
make any required production variable optional at runtime -- the
real app still refuses to start without real credentials. It only
ensures that, for the *test process*, every required variable has
*some* syntactically valid value before any test module gets a chance
to import app.infra.config:

  - If a real .env is present (e.g. a developer's local checkout),
    its values are loaded first and take priority, so tests can still
    exercise real credentials when explicitly asked to (see
    tests/test_llm_gemini.py, tests/test_llm_groq.py,
    tests/test_llm_manager.py for the opt-in "live" tests).
  - Anything still missing after that gets a clearly-fake placeholder
    (never a value that could be mistaken for a real secret), purely
    so importing app.infra.config succeeds. These placeholders are never
    used to make real API/Telegram calls anywhere in the default
    (offline) test suite -- provider calls are mocked (see
    tests/test_llm_gemini.py etc.), and nothing in this test suite
    starts the actual Telegram client or sends a real Telegram
    message.

The classifier's own tests (tests/test_keyword_filter.py) do not
depend on this at all -- app.domain.filters/app.domain.normalize have
no app.infra.config dependency, so they run fully isolated regardless of
anything in this file. This fixture only unblocks the *other* test
files that genuinely need the rest of the app's object graph to
construct (e.g. app.services.notifier needs BOT_CHAT_ID to exist, even just to
import).
"""

import asyncio
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

# Every variable app/core/config.py treats as required. Values below are
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
    "URL_SHORTENER_DOMAIN": "http://test-shortener.invalid",
    "URL_SHORTENER_ENDPOINT": "/shorten",
}

for _name, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)


class _StubJobRepository:
    """Hermetic stand-in for PostgresRepository when no DATABASE_URL is
    configured (plain `pytest tests/` with no Postgres reachable).

    Implements the JobRepository surface with permissive no-op semantics
    so tests that only need the dependency graph to construct -- or that
    monkeypatch a repository method via the DependencyProxy -- can run
    without infrastructure. When DATABASE_URL IS set (e.g. CI service
    containers), a real, initialized PostgresRepository is used instead,
    so repository-behavior tests keep exercising the real adapter.
    """

    async def initialize(self, *a, **kw):
        return None

    async def save(self, *a, **kw):
        return None

    async def get_job(self, *a, **kw):
        return None

    async def create_job_if_absent(self, *a, **kw):
        return True

    async def update_job(self, *a, **kw):
        return True

    async def has_job(self, *a, **kw):
        return False

    async def log_error(self, *a, **kw):
        return None

    async def log_gemini(self, *a, **kw):
        return None

    async def log_notification_guard(self, *a, **kw):
        return None

    async def get_latest_guard_decision(self, *a, **kw):
        return None

    async def get_latest_guard_decision_with_category(self, *a, **kw):
        return (None, None)

    async def get_incomplete_notification_jobs(self, *a, **kw):
        return []

    async def get_incomplete_classification_jobs(self, *a, **kw):
        return []

    async def claim_pending_classification(self, *a, **kw):
        return True


@pytest.fixture(autouse=True, scope="session")
def _bind_dependency_slots():
    """Bind the legacy DependencyProxy slots before any test runs.

    app.wiring.dependencies.DependencyProxy is deliberately strict: application
    code may never lazily construct adapter instances (that was the old
    "lazy fallback factory" design the composition root removed -- see
    app.wiring.dependencies). Production binds every slot through
    app.wiring.composition.compose() before any worker starts. The test
    session mirrors that contract by binding the same canonical module
    facades up front: app.adapters.repositories.postgres's
    PostgresRepository (requires DATABASE_URL), app.adapters.state.json's
    JsonStateStore over the shared StateManager singleton, a no-op guard
    allow/publisher pair, parser registry, and the standard no-op
    notification-category resolver. Tests that monkeypatch a proxy
    attribute (e.g. tests/test_telegram_recovery.py) still work because
    an instance attribute shadows the bound value, exactly as before.

    NOTE: this binds a real PostgresRepository when DATABASE_URL is set
    (e.g. CI service containers), bringing up the schema via the same
    versioned migrations / category seed the production startup runs.
    When DATABASE_URL is NOT set, a permissive no-op stub is bound
    instead so plain `pytest tests/` works hermetically without a
    Postgres server. Tests that only need repository *behavior* faked
    out should monkeypatch app.wiring.dependencies.logger directly
    rather than relying on either backend.
    """
    import os

    from app.adapters.repositories.postgres import PostgresRepository
    from app.adapters.state.json import JsonStateStore
    from app.services.state import state as _state_manager
    from app.wiring.dependencies import configure
    from app.services.parser import get_parser_registry

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        db = PostgresRepository(database_url)
        # Bring up the real schema (versioned migrations + category seed)
        # exactly as production startup does, so repository-behavior
        # tests run against a genuine Postgres backing.
        asyncio.run(db.initialize())
    else:
        db = _StubJobRepository()
    store = JsonStateStore(_state_manager)

    async def _noop_resolver(job_uuid, row, category_id):
        return category_id

    async def _noop_guard_allow(payload):
        return True

    class _NoopStreamPublisher:
        async def publish(self, job_row):
            return None

    class _NoopUrlShortener:
        """Pass-through stand-in for the real UrlShortenerService (see
        app.infra.url_shortener). Tests that specifically exercise shortening
        behavior bind their own fake via app.wiring.dependencies.configure()
        or by monkeypatching app.wiring.dependencies.url_shortener directly
        (an instance attribute shadows this bound value, same pattern
        used for the other proxies)."""

        async def shorten(self, job_id, url):
            return url

    configure(
        persistence=db,
        state_store=store,
        dedup_store=store,
        parser_registry=get_parser_registry(),
        notification_resolver=_noop_resolver,
        guard_allow_fn=_noop_guard_allow,
        stream_publisher_service=_NoopStreamPublisher(),
        url_shortener_service=_NoopUrlShortener(),
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
    """app.infra.heartbeat.liveness is process-lifetime, in-memory, global state
    by design (see its own module docstring) -- the running bot's
    healthcheck depends on exactly one shared registry, snapshotted into
    the heartbeat file. Across a test session that means any test which
    exercises real code beating a worker id (e.g. app.services.logger.DBLogger.run
    / app.services.state.StateManager.run reporting "db_worker"/"state_worker" dead
    after a real timed-out-operation test) leaves that state sitting in
    the registry for every later test that calls write_heartbeat() or
    otherwise reads the shared registry -- including tests with no
    relationship to DB/state timeouts at all. Clearing before *and* after
    each test keeps that state from ever leaking across test boundaries.
    """
    from app.infra.heartbeat import liveness as _liveness
    with _liveness._lock:
        _liveness._states.clear()
    yield
    with _liveness._lock:
        _liveness._states.clear()
    rate_limit_tracker.clear()
