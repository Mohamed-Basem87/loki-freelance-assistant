"""Typed configuration loaded from config/project.json with environment overrides.

This file intentionally contains no provider/model/source/policy literals. The
JSON file is the replaceable deployment-independent configuration baseline;
environment variables can override it in containers and production.
"""
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

with (BASE_DIR / "config" / "project.json").open(encoding="utf-8") as _f:
    _CONFIG = json.load(_f)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    wait_seconds: float


@dataclass(frozen=True)
class LLMProviderConfig:
    provider_id: str
    adapter: str
    models: tuple[str, ...]
    retry: RetryPolicy
    max_output_tokens: int
    arbitration_max_output_tokens: int = 600


@dataclass(frozen=True)
class RecoveryPolicy:
    telegram_max_messages: int
    freehub_max_backfill_pages: int
    telegram_retry_base_seconds: int
    telegram_retry_cap_seconds: int


@dataclass(frozen=True)
class RuntimePolicy:
    freehub_poll_interval: float
    freehub_page_size: int
    notification_retry_interval: int
    state_timeout_seconds: int
    database_timeout_seconds: int
    delivery_concurrency: int
    user_bot_batch_size: int
    user_bot_max_attempts: int
    http_timeout_seconds: int
    external_call_timeout_seconds: int
    telegram_message_limit: int
    message_safety_margin: int
    max_description_length: int
    max_reason_length: int
    llm_compact_arbitration_chars: int
    state_file_path: str
    database_file_path: str
    heartbeat_file_path: str
    heartbeat_interval_seconds: float
    heartbeat_max_age_seconds: float
    rejection_reason_category_id: str
    enabled_workers: tuple[str, ...]
    notification_backoff_base_seconds: int
    notification_backoff_cap_seconds: int
    user_bot_poll_interval: float
    freehub_base_url: str


@dataclass(frozen=True)
class JobSourceConfig:
    id: str
    adapter: str
    poll_interval: float
    enabled: bool = True
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    settings: dict = None


@dataclass(frozen=True)
class SourceProfile:
    id: str
    display_name: str
    aliases: tuple[str, ...] = ()


_llm_config = _CONFIG["llm"]
_env_llm_ids = _env("LLM_PROVIDERS")
_default_llm_ids = tuple(item["id"].lower() for item in _llm_config["providers"])
_llm_ids = (
    tuple(x.strip().lower() for x in _env_llm_ids.split(",") if x.strip())
    if _env_llm_ids
    else _default_llm_ids
)
_known_llm_ids = {item["id"].lower() for item in _llm_config["providers"]}
_unknown_llm_ids = sorted(set(_llm_ids) - _known_llm_ids)
if _unknown_llm_ids:
    raise ValueError(f"LLM_PROVIDERS contains unknown provider(s): {', '.join(_unknown_llm_ids)}")
_env_gemini = _env("GEMINI_MODELS")
_env_groq = _env("GROQ_MODELS")
_llm_env_models = {"gemini": _env_gemini, "groq": _env_groq}

# LLM_PROVIDERS is deployment *order*, not just a membership filter: the
# provider chain is tried in configured order (see app.llm.manager), so
# LLM_PROVIDERS=groq,gemini must yield groq before gemini even when
# config/project.json lists gemini first. Without a selector, the JSON
# baseline order is the provider order. dict.fromkeys dedupes duplicate
# selectors while preserving first occurrence, matching the de-duplicated
# behavior of the previous JSON-order scan.
_llm_config_by_id = {item["id"].lower(): item for item in _llm_config["providers"]}
_llm_ordered_ids = tuple(dict.fromkeys(_llm_ids))

LLM_PROVIDERS = tuple(
    LLMProviderConfig(
        provider_id=item["id"],
        adapter=item.get("adapter", ""),
        models=tuple(
            x.strip()
            for x in (_llm_env_models.get(item["id"]) or ",".join(item["models"])).split(",")
            if x.strip()
        ),
        retry=RetryPolicy(int(item["retry_attempts"]), float(item["retry_wait_seconds"])),
        max_output_tokens=int(item["max_output_tokens"]),
        arbitration_max_output_tokens=int(item.get("arbitration_max_output_tokens", item["max_output_tokens"])),
    )
    for provider_id in _llm_ordered_ids
    for item in (_llm_config_by_id[provider_id],)
)

_source_items = _CONFIG["sources"]
_JOB_SOURCE_ITEMS = _CONFIG.get("job_sources", [{"id":"freehub","adapter":"freehub","poll_interval":_CONFIG["runtime"]["freehub_poll_interval"],"enabled":True}])
def _job_source_config(item):
    source_id = str(item["id"])
    env_key = re.sub(r"[^A-Za-z0-9]", "_", source_id).upper()
    interval = float(_env(f"JOB_SOURCE_{env_key}_POLL_INTERVAL", str(item.get("poll_interval", _CONFIG["runtime"]["freehub_poll_interval"]))))
    enabled = _env(f"JOB_SOURCE_{env_key}_ENABLED", str(bool(item.get("enabled", True)))).lower() not in {"0", "false", "no", "off"}
    return JobSourceConfig(
        source_id,
        item.get("adapter", source_id),
        interval,
        enabled,
        item.get("display_name", source_id),
        tuple(item.get("aliases", ())),
        dict(item.get("settings", {})),
    )

_all_job_sources = tuple(_job_source_config(x) for x in _JOB_SOURCE_ITEMS)
_job_source_selector = _env("JOB_SOURCES")
if _job_source_selector:
    _selected_job_source_ids = tuple(x.strip().lower() for x in _job_source_selector.split(",") if x.strip())
    _known_job_source_ids = {x.id.lower() for x in _all_job_sources}
    _unknown_job_sources = sorted(set(_selected_job_source_ids) - _known_job_source_ids)
    if _unknown_job_sources:
        raise ValueError(f"JOB_SOURCES contains unknown source(s): {', '.join(_unknown_job_sources)}")
    JOB_SOURCES = tuple(x for x in _all_job_sources if x.id.lower() in _selected_job_source_ids)
else:
    JOB_SOURCES = _all_job_sources

_env_source_ids = _env("FREEHUB_SOURCES")
_source_ids = tuple(x.strip().lower() for x in (_env_source_ids.split(",") if _env_source_ids else [x["id"] for x in _source_items]) if x.strip())
_known_upstream_source_ids = {x["id"].lower() for x in _source_items}
_unknown_upstream_sources = sorted(set(_source_ids) - _known_upstream_source_ids)
if _unknown_upstream_sources:
    raise ValueError(f"FREEHUB_SOURCES contains unknown upstream source(s): {', '.join(_unknown_upstream_sources)}")
_source_map = {x["id"]: x for x in _source_items}
_profiles = [
    SourceProfile(x, _source_map[x]["display_name"], tuple(_source_map[x]["aliases"]))
    for x in _source_ids
    if x in _source_map
]
# JOB_SOURCES are adapter identities, not upstream platform identities.
# Never synthesize an upstream SourceProfile from a JobSource adapter.
SOURCES = tuple(_profiles)
SOURCE_IDS = tuple(x.id for x in SOURCES)

def _resolve_path(value: str) -> str:
    """Resolve a possibly-relative configured path against BASE_DIR.

    Regression fix (audit finding P2-6): STATE_FILE_PATH and
    DATABASE_FILE_PATH's *defaults* were already absolute (derived
    from BASE_DIR), but .env.example itself documents relative
    example values ("loki_freelance_bot.db",
    "database/state.json") -- exactly the values an operator gets by
    copying .env.example to .env, which is the normal setup step.
    A relative path is resolved against the current working
    directory at runtime, not BASE_DIR, so running the bot from any
    directory other than the repo root (this is transparent inside
    the Docker image, where the working directory happens to be
    /app, but not for local/manual execution) silently created or
    read a *different* database/state file than intended -- with no
    error, just quietly wrong data. Resolving every configured path
    against BASE_DIR here, once, at the single point both
    RuntimePolicy fields are constructed, makes the two paths behave
    consistently regardless of the process's working directory.
    """
    path = Path(value)
    return str(path if path.is_absolute() else (BASE_DIR / path))


_r = _CONFIG["runtime"]
RUNTIME = RuntimePolicy(
    freehub_poll_interval=_float("FREEHUB_POLL_INTERVAL", _r["freehub_poll_interval"]),
    freehub_page_size=_int("FREEHUB_PAGE_SIZE", _r["freehub_page_size"]),
    notification_retry_interval=_int("NOTIFICATION_RETRY_INTERVAL", _r["notification_retry_interval"]),
    state_timeout_seconds=_int("STATE_TIMEOUT_SECONDS", _r["state_timeout_seconds"]),
    database_timeout_seconds=_int("DATABASE_TIMEOUT_SECONDS", _r["database_timeout_seconds"]),
    delivery_concurrency=_int("DELIVERY_CONCURRENCY", _r["delivery_concurrency"]),
    user_bot_batch_size=_int("USER_BOT_BATCH_SIZE", _r["user_bot_batch_size"]),
    user_bot_max_attempts=_int("USER_BOT_MAX_ATTEMPTS", _r["user_bot_max_attempts"]),
    http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", _r["http_timeout_seconds"]),
    # Bounds external calls that otherwise have no timeout of their own
    # and are awaited directly on a worker's event-loop task -- Telethon
    # RPC calls (get_dialogs/get_entity/get_messages/iter_messages/
    # start/disconnect) and the LLM provider calls run via
    # asyncio.to_thread (app.job_processor, app.notification_guard.guard).
    # Unlike a hung *synchronous* call, a hung genuinely-async await or a
    # stuck background thread never blocks the event loop, so
    # app.heartbeat.heartbeat_loop keeps ticking on schedule and the
    # container's healthcheck reports healthy even though that one
    # worker task is permanently stuck (audit finding: hung-worker
    # detection gap). This gives every such call a real ceiling so it
    # fails loudly (and is retried/backed off by its existing caller)
    # instead of hanging forever.
    external_call_timeout_seconds=_int("EXTERNAL_CALL_TIMEOUT_SECONDS", _r.get("external_call_timeout_seconds", 60)),
    telegram_message_limit=_int("TELEGRAM_MESSAGE_LIMIT", _r["telegram_message_limit"]),
    message_safety_margin=_int("MESSAGE_SAFETY_MARGIN", _r["message_safety_margin"]),
    max_description_length=_int("MAX_DESCRIPTION_LENGTH", _r["max_description_length"]),
    max_reason_length=_int("MAX_REASON_LENGTH", _r["max_reason_length"]),
    llm_compact_arbitration_chars=_int("COMPACT_ARBITRATION_MAX_TEXT_CHARS", _r["llm_compact_arbitration_chars"]),
    state_file_path=_resolve_path(_env("STATE_FILE_PATH", str(BASE_DIR / "database" / "state.json"))),
    database_file_path=_resolve_path(_env("DATABASE_FILE_PATH", str(BASE_DIR / "loki_freelance_bot.db"))),
    # Resolved the same way as state_file_path/database_file_path
    # above and for the same reason (audit finding P2-6 /
    # configuration consistency): app/healthcheck.py runs as a
    # separate subprocess and previously read HEARTBEAT_FILE_PATH
    # (and DATABASE_FILE_PATH/STATE_FILE_PATH) directly from the
    # environment with its own hardcoded absolute defaults, bypassing
    # this resolution entirely -- so a relative override that the
    # running application resolved against BASE_DIR could resolve
    # differently (against the healthcheck subprocess's working
    # directory) or not match healthcheck's own separate default at
    # all. healthcheck.py now imports RUNTIME instead, so there is
    # exactly one interpretation of these paths in the whole process
    # tree, not one in the runtime and a second, subtly different one
    # in the healthcheck.
    heartbeat_file_path=_resolve_path(_env("HEARTBEAT_FILE_PATH", str(BASE_DIR / "database" / "heartbeat.txt"))),
    heartbeat_interval_seconds=_float("HEARTBEAT_INTERVAL_SECONDS", 15),
    heartbeat_max_age_seconds=_float("HEARTBEAT_MAX_AGE_SECONDS", 90),
    rejection_reason_category_id=_env("REJECTION_REASON_CATEGORY_ID", _r["rejection_reason_category_id"]),
    enabled_workers=tuple(x.strip().lower() for x in _env("ENABLED_WORKERS", ",".join(_r.get("enabled_workers", ("telegram", "freehub", "classification_retry", "notification_retry", "user_notifications")))).split(",") if x.strip()),
    notification_backoff_base_seconds=_int("NOTIFICATION_BACKOFF_BASE_SECONDS", _r["notification_backoff_base_seconds"]),
    notification_backoff_cap_seconds=_int("NOTIFICATION_BACKOFF_CAP_SECONDS", _r["notification_backoff_cap_seconds"]),
    user_bot_poll_interval=_float("USER_BOT_POLL_INTERVAL", _r["user_bot_poll_interval"]),
    freehub_base_url=_env("FREEHUB_BASE_URL", _CONFIG["freehub"]["base_url"]).rstrip("/"),
)

RECOVERY = RecoveryPolicy(
    telegram_max_messages=_int("TELEGRAM_MAX_RECOVERY_MESSAGES", _r["telegram_max_recovery_messages"]),
    freehub_max_backfill_pages=_int("FREEHUB_MAX_BACKFILL_PAGES", _r["freehub_max_backfill_pages"]),
    telegram_retry_base_seconds=_int("TELEGRAM_RECOVERY_RETRY_BASE_SECONDS", _r.get("telegram_recovery_retry_base_seconds", 5)),
    telegram_retry_cap_seconds=_int("TELEGRAM_RECOVERY_RETRY_CAP_SECONDS", _r.get("telegram_recovery_retry_cap_seconds", 300)),
)


def _validate_positive(name, value, *, minimum=0):
    if value <= minimum:
        comparator = "> 0" if minimum == 0 else f"> {minimum}"
        raise ValueError(f"{name} must be {comparator}; got {value!r}")


for _name in (
    "freehub_poll_interval", "freehub_page_size", "notification_retry_interval",
    "state_timeout_seconds", "database_timeout_seconds",
    "delivery_concurrency", "user_bot_batch_size", "http_timeout_seconds",
    "external_call_timeout_seconds",
    "telegram_message_limit", "message_safety_margin",
    "max_description_length", "max_reason_length",
    "llm_compact_arbitration_chars", "notification_backoff_base_seconds",
    "notification_backoff_cap_seconds", "user_bot_poll_interval",
    "freehub_page_size",
    "heartbeat_interval_seconds", "heartbeat_max_age_seconds",
):
    _validate_positive(_name, getattr(RUNTIME, _name))

_validate_positive("user_bot_max_attempts", RUNTIME.user_bot_max_attempts, minimum=0)
_validate_positive("telegram_max_recovery_messages", RECOVERY.telegram_max_messages)
_validate_positive("freehub_max_backfill_pages", RECOVERY.freehub_max_backfill_pages)
_validate_positive("telegram_retry_base_seconds", RECOVERY.telegram_retry_base_seconds)
_validate_positive("telegram_retry_cap_seconds", RECOVERY.telegram_retry_cap_seconds)

if RECOVERY.telegram_retry_cap_seconds < RECOVERY.telegram_retry_base_seconds:
    raise ValueError("telegram_retry_cap_seconds must be >= telegram_retry_base_seconds")

# A heartbeat that must be *older* than the interval before it is
# considered stale is an invalid configuration: the healthcheck would
# permanently report the runtime as stuck even though it is writing
# beats on schedule.
if RUNTIME.heartbeat_max_age_seconds < RUNTIME.heartbeat_interval_seconds:
    raise ValueError(
        "heartbeat_max_age_seconds must be >= heartbeat_interval_seconds"
    )

# Every outbound call the app frames with no HTTP layer of its own
# (Telethon RPCs, the FreeHub/LLM sync SDK calls that run via
# asyncio.to_thread) is bounded by EXTERNAL_CALL_TIMEOUT_SECONDS (see
# app.timeouts). HTTP_TIMEOUT_SECONDS is the SDK/HTTP-layer deadline
# those same calls observe while doing real network I/O. An
# external-call deadline BELOW the HTTP/SDK deadline would be the
# weaker of the two bounds on exactly the calls that need the longer
# one -- a configuration that can only surprise -- so it is rejected at
# startup rather than accepted silently.
if RUNTIME.external_call_timeout_seconds < RUNTIME.http_timeout_seconds:
    raise ValueError(
        "external_call_timeout_seconds must be >= http_timeout_seconds "
        f"(got external_call_timeout_seconds={RUNTIME.external_call_timeout_seconds}, "
        f"http_timeout_seconds={RUNTIME.http_timeout_seconds})"
    )


def _warn_if_freehub_endpoint_is_plaintext(base_url: str) -> bool:
    """Audit finding P1-5: FreeHub's configured endpoint is plain HTTP,
    not HTTPS, so traffic to/from it is unauthenticated and
    unencrypted -- vulnerable to tampering, content injection,
    response manipulation, and passive observation at the network
    level. The audit's own accepted mitigation, when moving the
    upstream itself to HTTPS is not immediately possible, is to make
    this trust boundary explicit rather than silent. Returns whether a
    warning was emitted, so this is unit-testable without capturing
    stdout.
    """
    if base_url.lower().startswith("http://"):
        print(
            "[CONFIG WARNING] FREEHUB_BASE_URL uses plaintext HTTP "
            f"({base_url!r}). Traffic to this endpoint is not "
            "protected by TLS and can be tampered with, injected "
            "into, or observed by anything on the network path. "
            "Move this endpoint to HTTPS as soon as the upstream "
            "supports it; until then, treat any data it returns as "
            "coming from an untrusted network boundary."
        )
        return True
    return False


_warn_if_freehub_endpoint_is_plaintext(RUNTIME.freehub_base_url)

if RUNTIME.notification_backoff_cap_seconds < RUNTIME.notification_backoff_base_seconds:
    raise ValueError(
        "notification_backoff_cap_seconds must be >= notification_backoff_base_seconds"
    )

_known_workers = {"telegram", "freehub", "classification_retry", "notification_retry", "user_notifications"}
_unknown_workers = sorted(set(RUNTIME.enabled_workers) - _known_workers)
if _unknown_workers:
    raise ValueError(f"ENABLED_WORKERS contains unknown worker(s): {', '.join(_unknown_workers)}")

for _provider in LLM_PROVIDERS:
    _validate_positive(f"{_provider.provider_id}.retry_attempts", _provider.retry.max_attempts, minimum=0)
    _validate_positive(f"{_provider.provider_id}.retry_wait_seconds", _provider.retry.wait_seconds)
    _validate_positive(f"{_provider.provider_id}.max_output_tokens", _provider.max_output_tokens)
    _validate_positive(
        f"{_provider.provider_id}.arbitration_max_output_tokens",
        _provider.arbitration_max_output_tokens,
    )
    if not _provider.models:
        raise ValueError(f"{_provider.provider_id}.models must contain at least one model")

for _source in JOB_SOURCES:
    if _source.enabled:
        _validate_positive(f"job_source:{_source.id}.poll_interval", _source.poll_interval)


def _exact_alias_match(value: str, alias: str) -> bool:
    import re as _re
    return bool(_re.search(rf"(?<!\w){_re.escape(alias.casefold())}(?!\w)", value.casefold()))

def source_profile(source: str) -> SourceProfile | None:
    value = (source or "").strip()
    canonical = value.casefold()
    for profile in SOURCES:
        if canonical == profile.id.casefold():
            return profile
    for profile in SOURCES:
        if any(_exact_alias_match(value, alias) for alias in profile.aliases):
            return profile
    return None
