# Loki Freelance Assistant --- Technical Documentation

> Authoritative technical reference for the current SQLite/Docker
> implementation.

## 1. System Overview

Loki is a single-process Python application using `asyncio` for
orchestration and dedicated serialized workers for durable SQLite and
JSON state operations.

The production dependency graph is assembled by `app/composition.py`.

The runtime contains:

-   source ingestion workers
-   parsing/normalization
-   durable identity and deduplication
-   deterministic category classification
-   one-call LLM arbitration for ambiguous jobs
-   optional Notification Guard
-   fixed-destination notification
-   user subscription routing
-   durable user notification delivery
-   recovery/retry workers
-   heartbeat/liveness and health checks

### Runtime flow

``` text
Sources
  │
  ├── Telegram / Telethon
  └── FreeHub
        │
        ▼
   Source adapters
        │
        ▼
 parse + normalize
        │
        ▼
 identity + dedup
        │
        ▼
 deterministic classification
        │
   ┌────┴────┐
   │         │
confident  ambiguous
   │         │
   │         ▼
   │    LLM arbitration
   │         │
   └────┬────┘
        ▼
   final category
        │
   ┌────┴──────────────┐
   ▼                   ▼
fixed destination   subscriber routing
                       │
                       ▼
                durable user queue
                       │
                       ▼
                 Telegram Bot API
```

## 2. Installation

### Prerequisites

-   Python 3.11+
-   Telegram API ID/hash
-   Telethon user account
-   Telegram Bot token
-   Gemini API key(s)
-   Groq API key
-   FreeHub configuration

### Install

``` bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows PowerShell:

``` powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create `.env` from `.env.example`.

### First Telethon login

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first login is interactive. The Telethon session is persisted and
reused.

## 3. Configuration

Configuration is split between `app/config.py`, `app/runtime_config.py`,
and Notification Guard configuration.

Typical environment variables include:

  ---------------------------------------------------------------------------------------
  Variable                                Required                Purpose
  --------------------------------------- ----------------------- -----------------------
  `API_ID`                                Yes                     Telegram application ID

  `API_HASH`                              Yes                     Telegram application
                                                                  hash

  `PHONE_NUMBER`                          Yes                     Telethon user-account
                                                                  phone number

  `BOT_TOKEN`                             Yes                     Telegram Bot API token

  `BOT_CHAT_ID`                           Yes                     Fixed private
                                                                  notification
                                                                  destination

  `BOT_CHANNEL_ID`                        No                      Optional public/channel
                                                                  destination

  `BOT_CHANNEL_CATEGORY_ID`               No                      Category used by the
                                                                  configured channel
                                                                  destination

  `GEMINI_API_KEYS`                       Yes                     Comma-separated Gemini
                                                                  credentials

  `GROQ_API_KEY`                          Yes                     Main Groq fallback
                                                                  credential

  `TARGET_CHANNEL_IDS`                    Yes                     Monitored Telegram
                                                                  source channels

  `FREEHUB_USER_ID`                       Yes                     FreeHub account/user
                                                                  identifier

  `FREEHUB_BASE_URL`                      No                      FreeHub API base URL

  `FREEHUB_POLL_INTERVAL`                 No                      FreeHub polling
                                                                  interval

  `FREEHUB_PAGE_SIZE`                     No                      FreeHub page size

  `NOTIFICATION_GUARD_ENABLED`            No                      Enables Notification
                                                                  Guard

  `GROQ_NOTIFICATION_GUARD_API_KEY`       Guard-only              Guard provider
                                                                  credential

  `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`   No                      Guard retry limit
  ---------------------------------------------------------------------------------------

The actual accepted configuration surface is defined by the
runtime/config modules; `.env.example` is the deployment-oriented
reference.

## 4. Architecture and Module Responsibilities

### 4.1 Composition root

`app/composition.py` owns production dependency construction.

Its responsibilities include:

-   constructing repositories
-   constructing state/dedup adapters
-   constructing HTTP transport
-   wiring source factories
-   wiring Telegram ingestion
-   wiring notification transports/sinks
-   wiring Notification Guard
-   wiring the user bot
-   assembling the runtime resource graph

Application services should receive semantic dependencies rather than
constructing SDK clients themselves.

### 4.2 Ports

`app/ports.py` defines semantic contracts such as:

-   `JobSource`
-   `JobRepository`
-   `StateStore`
-   `DedupStore`
-   `NotificationSink`
-   `NotificationTransport`
-   `LLMProvider`

Concrete SDKs and persistence details remain in adapters.

### 4.3 Registries

Registries provide configuration/discovery boundaries for:

-   categories
-   sources
-   parsers
-   repositories
-   state/dedup
-   notifications
-   transports
-   LLM providers
-   user-facing surfaces

Registries should remain wiring/discovery mechanisms, not hidden service
locators.

### 4.4 Compatibility seams

Legacy compatibility entry points remain where existing
imports/extensions depend on them.

Examples include:

-   `app.dependencies.DependencyProxy`
-   module-level LLM/Guard compatibility functions
-   `app.freehub.fetch_projects()`
-   `app.llm.*` compatibility entry points
-   transitional `SQLiteRepository.run()`

These are compatibility boundaries, not the preferred application
architecture.

## 5. Telegram Ingestion

Telegram ingestion uses a Telethon user account.

The production source is `TelegramChannelJobSource`.

### Responsibilities

-   connect/login
-   resolve monitored channels
-   register live handlers
-   perform startup recovery
-   process new messages
-   persist per-channel progress
-   reconnect/recover when required

### Recovery ordering

Registration-before-recovery is intentional:

``` text
register live handler
        │
        ▼
recover historical messages
        │
        ▼
release recovery barrier
        │
        ▼
normal live processing
```

This prevents a gap between historical recovery and live-event
registration.

### Recovery barrier

Per-channel durable watermarks prevent live processing from advancing
durable progress beyond unrecovered history.

### Hard recovery limit

Telegram recovery is capped at:

``` text
2,000 messages per recovery pass
```

This is intentional.

A bounded pass must not falsely declare an incompletely recovered
channel permanently caught up. The recovery state remains retryable.

## 6. FreeHub Ingestion

FreeHub is represented through `FreeHubJobSource` and its HTTP client.

The HTTP client is composed behind `HttpTransport`, with
`AioHttpTransport` providing the current implementation.

### Intentional HTTP transport

FreeHub currently uses HTTP because that is the configured upstream
endpoint. Do not treat "replace the client with HTTPS" as an
application-only fix.

The external HTTP boundary should remain clearly isolated and should be
upgraded when the upstream supports secure transport.

### Bounded pagination

FreeHub backfill is capped at:

``` text
10 pages
```

This bound is intentional.

### Pagination limitation

FreeHub uses newest-first offset/page pagination. New upstream
insertions can shift existing projects between page numbers.

Therefore:

``` text
saved page number
        +
new upstream insertions
        =
possible page-boundary movement
```

Durable project identity/deduplication protects against duplicates, but
a pure page-number continuation mechanism cannot provide the same
guarantee as a stable upstream cursor.

Future strict-recovery improvements should prefer:

1.  stable upstream cursor, if available;
2.  otherwise bounded overlap around the continuation point plus
    identity deduplication.

Do not remove the 10-page bound to solve this.

## 6a. URL Shortening

Every project URL is run through an internal shortener service --
`UrlShortenerService` (`app/url_shortener.py`) -- before its job row is
ever created, regardless of source. This unifies the URL representation
across Telegram, FreeHub, and the scraper-file sources into one owned
short-link format.

### Where it runs

Inside `app.job_processor.process_job()`, immediately before
`logger.create_job_if_absent(...)`, and only for genuinely new jobs
(`existing_incomplete` is False). A resumed/retried row never
re-shortens -- the URL was already resolved on the first pass.

Cross-source project-id extraction (`_extract_project_id()`, used for
FreeHub/Telegram duplicate detection) runs earlier, against the
*original* URL, so shortening never affects that dedup path.

### Contract with the shortener service

``` text
POST {URL_SHORTENER_DOMAIN}{URL_SHORTENER_ENDPOINT}
body:      {"url": "<original project url>"}

success:   200 {"url": "<shortened url>"}
duplicate: {URL_SHORTENER_DUPLICATE_STATUS} (default 409) -- this exact
           original URL was already shortened before
other:     any other non-2xx, timeout, or malformed body -- a genuine
           failure
```

### Duplicate handling

A duplicate-status response (`UrlAlreadyExistsError`) is an
authoritative "already seen" signal, not a failure. It is always
treated as a stop, independent of `URL_SHORTENER_FAILURE_MODE`: no job
row is created and no notification is sent -- the same shape as the
existing legacy-identity-match and cross-source project-claim skip
paths already in `process_job()`. The exception is caught locally, so
the source still marks the job seen and does not retry it.

### Failure handling

Any other shortener failure is governed by `URL_SHORTENER_FAILURE_MODE`,
toggled purely by that env var (no code change required):

``` text
fail_open    (default) -- log and fall back to the original URL; the
                           pipeline continues normally with the long URL.
fail_closed             -- raise UrlShorteningError, uncaught, out of
                           process_job(). The calling source worker logs
                           it and does NOT mark the job seen / advance
                           its watermark (see app/source_worker.py), so
                           the job is retried from scratch on that
                           source's next poll. No new durable "pending"
                           state is needed for this -- it reuses the
                           same seen/watermark mechanism FreeHub and
                           Telegram already rely on for every other
                           process_job() failure.
```

### Scope

New jobs only. Existing rows created before this feature keep their
original long URL; there is no backfill/migration step.

## 7. Parsing and Normalization

Parsing is source-specific.

Current parser adapters include generic, Mostaql, and Nafezly handling.

The parser layer converts source-specific content into the shared job
representation.

Normalization:

-   handles Arabic/English text
-   normalizes whitespace and textual representation
-   prepares consistent input for deterministic classification and LLM
    review

External job content remains untrusted data.

## 8. Category Profiles and Classification

Category profiles are discovered from `app/categories`.

Current registered profiles:

  Category                   Deterministic   Arbitration
  ------------------------ --------------- -------------
  Data Analysis                        Yes           Yes
  AI/ML Data Science                   Yes           Yes
  Backend Development                  Yes           Yes
  Frontend Development                 Yes           Yes
  Mobile App Development               Yes           Yes
  Game Development                     Yes           Yes
  Full Stack Development            **No**       **Yes**

Full Stack is explicitly:

``` text
arbitration_only = true
```

This is a design decision.

It must not be inserted into deterministic keyword classification.

### Category package contract

Each category normally supplies:

``` text
profile.py
keywords.py
llm_prompt.py
guard_prompt.py
```

The shared classifier remains category-agnostic.

### One final category

A job has:

-   one final category, or
-   no category.

Users may subscribe to many categories, but a single job is not
broadcast as multiple categories.

## 9. LLM Subsystem

The LLM subsystem contains:

-   provider adapters
-   provider registry
-   manager
-   candidate rotation
-   rate-limit/cooldown tracking
-   response validation

### Arbitration

Ambiguous jobs are reviewed in one arbitration request containing the
relevant candidate category definitions.

The provider must return one candidate category ID or `none`.

### Provider rotation

Gemini rotates across configured key/model candidates.

Groq uses configured model rotation.

### Cooldown policy

Local cooldown is advisory.

If all candidates are locally cooling down, Loki still attempts them.

Reason:

``` text
local cooldown = estimate
provider response = authority
```

Waiting for the local timer could cause a valid provider to be skipped
and a job to be missed.

### Deadline behavior

The review path is bounded by a global deadline so provider rotation
cannot extend indefinitely.

### Failure policy

If required LLM review cannot complete successfully, classification
fails closed.

## 10. Notification Guard

The Guard is an optional safety filter for direct deterministic
acceptance.

``` text
deterministic acceptance
        │
        ▼
Notification Guard
        │
   ┌────┴────┐
 notify   do_not_notify
```

LLM-reviewed jobs do not receive a redundant Guard pass because they
have already undergone LLM review.

Guard provider construction is isolated from core classification.

## 11. User Bot and Subscriptions

The user bot uses the Telegram Bot API.

### `/start`

-   registers/activates the user
-   exposes enabled categories
-   allows multiple category selections
-   persists subscription preferences

### `/categories`

Allows subscription changes after initial setup.

### `/stop`

Deactivates the user and cancels undelivered queued notifications.

Previously delivered notifications are not removed.

A later `/start` does not replay the cancelled backlog.

### Source preferences

Users may select source preferences in addition to categories.

Empty source preference means all sources.

## 12. User Notification Routing and Delivery

Routing evaluates both:

-   category
-   source

for subscriber destinations.

Destination type does not bypass subscription criteria.

### Durable queue

Subscriber notifications are persisted before delivery.

Each `(job, user)` relationship is protected against duplicate queue
insertion.

### Delivery

Delivery workers use bounded concurrency.

Current defaults:

``` text
concurrency = 10
batch size  = 20
max attempts = 5
```

Delivery state is persisted so retries can resume after restart.

Telegram rate limits are delayed/retried. Unavailable bot chats can
deactivate the affected user.

## 13. SQLite Database

SQLite is the primary durable store.

Important logical tables include:

``` text
jobs
gemini
notifications
errors
notification_guard
users
categories
user_categories
user_notifications
```

The repository exposes semantic methods to application code.

The SQLite adapter owns the underlying serialized DB execution.

### Single-process rule

The database is intentionally single-process:

``` text
one Loki process
one SQLite file
one DB worker
```

Multiple containers must not share the same database volume.

### Transactional claims

Claim/state transitions that must be atomic are implemented using SQLite
transactional semantics rather than application-level "check then
update" sequences.

## 14. Persistent JSON State

Source watermarks and source-specific state are stored under:

``` text
database/state.json
```

The state implementation provides:

-   serialized execution
-   atomic file replacement
-   backup/recovery behavior
-   semantic `StateStore` methods

The underlying state executor is an infrastructure mechanism and should
not leak into application code.

## 15. Recovery and Failure Semantics

The system is designed around the principle:

> Never mark durable progress past work that has not been safely
> accounted for.

Important recovery cases include:

-   Telegram startup recovery
-   Telegram reconnect/recovery
-   FreeHub bounded backfill
-   notification retries
-   classification retries
-   state corruption recovery
-   persistence worker failures
-   interrupted delivery

### Persistence worker quarantine

A timed-out persistence worker cannot safely be killed in Python.

Therefore a stuck DB/state worker is quarantined rather than replaced
in-process against the same underlying resource.

The defined safe boundary is process restart.

This prevents two workers from concurrently operating on one SQLite
connection or one serialized state backend.

## 16. Concurrency and Race Prevention

The runtime uses bounded asynchronous concurrency and explicit
per-resource synchronization.

Important invariants include:

### Telegram

``` text
register handler
      before
recovery
```

and:

``` text
live processing cannot advance a channel
past unrecovered durable history
```

### Notifications

Live delivery and retry sweeps coordinate around durable notification
state and per-job/destination synchronization.

### SQLite

All application database mutations go through the serialized repository
infrastructure.

### Shutdown

The runtime owns resource shutdown and attempts deterministic cleanup
for:

-   HTTP transport
-   notification transport
-   user bot
-   registered shutdown hooks

Shutdown errors are surfaced without preventing the remaining cleanup
sequence.

## 17. Docker Deployment

Docker deployment must preserve:

-   Telethon session
-   SQLite database
-   JSON state

Do not mount one database/state directory into multiple Loki replicas.

The process model is intentionally:

``` text
1 container/process
1 SQLite DB
1 JSON state
```

## 18. Testing

Run:

``` bash
./scripts/test.sh
```

or:

``` bash
pip install -r requirements.txt
pytest tests/ -q
```

The repository includes tests for:

-   parser behavior
-   normalization
-   classification
-   category discovery
-   LLM behavior
-   provider rotation
-   SQLite persistence
-   migrations
-   identity/deduplication
-   notification state
-   Guard
-   routing
-   user bot behavior
-   Telegram lifecycle
-   Telegram recovery and recovery caps
-   reconnect behavior
-   FreeHub polling/backfill
-   state recovery
-   message/HTML safety
-   timeouts
-   worker liveness
-   source registry behavior
-   pluggable architecture

Live provider tests require credentials.

## 19. Troubleshooting

### Loki exits because Telegram credentials are missing

Verify:

``` text
API_ID
API_HASH
PHONE_NUMBER
```

and ensure the Telethon session can be created.

### Bot commands work but users receive nothing

Check:

-   `BOT_TOKEN`
-   user activation
-   category subscriptions
-   source preferences
-   `user_notifications`
-   Telegram Bot API permissions/rate limits

### FreeHub appears to stop recovering

Remember:

``` text
FREEHUB_MAX_BACKFILL_PAGES = 10
```

is an intentional bound.

Inspect durable continuation and dedup state before changing the bound.

### Telegram recovery appears incomplete

A single recovery pass is capped at 2,000 messages by design. Check the
durable watermark/recovery state and allow subsequent recovery to
continue.

### All LLM candidates show cooldown

This does not mean Loki will stop.

The system intentionally attempts all candidates anyway because local
cooldown state is advisory.

### SQLite worker becomes stuck

Do not start a replacement worker against the same connection.

The safe recovery boundary is process restart.

## 20. Known Limitations and Engineering Follow-ups

### FreeHub offset pagination

Newest-first page-number pagination is inherently weaker than stable
cursor pagination. New upstream inserts can move projects across page
boundaries.

Preferred future solution:

``` text
stable cursor
```

or, if unavailable:

``` text
bounded overlap + durable identity dedup
```

The 10-page bound remains intentional.

### Compatibility seams

Some legacy module-level entry points remain for compatibility.

They should not become new application dependencies.

### Timestamp representation

New durable time fields should prefer UTC-aware timestamps rather than
naive local `datetime.now()` values.

Existing behavior should only be changed with a migration/compatibility
plan.

### CI workflow duplication

If multiple CI workflows duplicate dependency bootstrap and smoke-test
logic, keep them synchronized or consolidate common steps into reusable
workflow components.

### Dependency-injection hygiene

Avoid eager default expressions such as:

``` python
overrides.pop("api_id", get_api_id())
```

because `get_api_id()` executes even when the override exists.

Prefer explicit lazy resolution.

## 21. Maintenance Guidelines

When adding a source:

1.  define the semantic `JobSource`
2.  isolate upstream SDK/HTTP behavior in an adapter
3.  expose a stable identity/checkpoint contract
4.  wire it through the composition root/registry
5.  add recovery and failure tests

When adding a category:

1.  add a category package
2.  implement `profile.py`
3.  implement `keywords.py`
4.  implement `llm_prompt.py`
5.  implement `guard_prompt.py`
6.  decide whether it is deterministic, arbitration-only, or both
7.  register/discover it
8.  add classification and routing tests

When adding a notification destination:

1.  keep rendering separate from transport
2.  preserve durable per-destination state
3.  make retries idempotent
4.  route through subscription filters
5.  test rate-limit and unavailable-destination behavior

When changing persistence:

-   preserve transaction boundaries
-   preserve single-process invariants
-   add migration tests
-   test crash/interruption scenarios
-   do not expose raw DB execution primitives to application services

## 22. Design Philosophy

Loki prioritizes:

-   correctness over cleverness
-   bounded recovery over unbounded resource usage
-   durable state over in-memory assumptions
-   deterministic classification before probabilistic review
-   one category per job
-   idempotent delivery
-   explicit dependency boundaries
-   recoverability after crashes
-   fail-closed decisions where classification cannot be trusted
-   compatibility without allowing legacy seams to become the new
    architecture

## 23. License

MIT

---

# 24. Abstraction Architecture

`app/composition.py` is the authoritative production composition root.

```text
validated/runtime configuration
            │
            ▼
      composition root
            │
   ┌────────┼────────┐
   ▼        ▼        ▼
sources  persistence LLM/Guard
   │        │        │
   └────────┼────────┘
            ▼
     application services
            │
            ▼
     workers + recovery
            │
            ▼
          runtime
```

The application depends on semantic ports. Concrete SDKs, HTTP clients, SQLite execution, and JSON file I/O stay behind adapters.

## Semantic ports

### `JobSource`

Represents a source ingestion adapter such as Telegram channels or FreeHub.

`identity_source` provides the adapter's fallback identity namespace. A multi-platform adapter may normalize a job to an upstream identity namespace; that upstream identity is not itself a `JobSource` adapter.

### `JobRepository`

Exposes semantic persistence operations used by application services:

- job creation/deduplication
- job reads and updates
- audit logging
- Guard decisions
- notification recovery
- subscriber queue operations
- user/subscription persistence

Application code should use semantic repository methods rather than passing raw repository execution methods around.

### `StateStore`

Owns semantic source state such as:

- Telegram watermarks
- recovery state
- FreeHub continuation state

The current JSON implementation uses a serialized blocking executor internally.

### `DedupStore`

Owns durable identity claims and FreeHub-specific seen/pending/continuation state.

Its purpose is to keep ingestion idempotent across:

- multiple source paths
- retries
- reconnects
- process restarts

### `NotificationSink`

Represents a destination-level notification contract.

The notification service treats sinks independently so one failed destination does not erase successful destination state.

### `NotificationTransport`

Represents the concrete outbound transport, such as the Telegram Bot API.

Rendering and transport are intentionally separate.

### `LLMProvider`

Represents an LLM provider implementation behind the LLM manager.

Provider adapters own:

- SDK clients
- provider exceptions
- response parsing
- provider-specific retry semantics
- key/model rotation

The manager owns cross-provider orchestration and deadlines.

## Source abstraction

### Telegram

Telegram preserves:

- registration-before-recovery
- per-channel recovery barriers
- durable watermarks
- bounded 2,000-message recovery passes
- bounded recovery retry

### FreeHub

FreeHub preserves:

- HTTP adapter isolation
- 10-page bounded backfill
- durable seen/continuation state

FreeHub page-number continuation is an upstream pagination limitation. A stable cursor or bounded overlap would be preferable if strict no-gap historical recovery becomes a requirement.

## Category abstraction

`app/categories/registry.py` discovers category packages and distinguishes:

```text
enabled
deterministic
arbitration-only
```

Current policy:

```text
Data Analysis          deterministic + arbitration
AI/ML Data Science     deterministic + arbitration
Backend Development    deterministic + arbitration
Frontend Development   deterministic + arbitration
Mobile App Development deterministic + arbitration
Game Development       deterministic + arbitration
Full Stack Development arbitration-only
```

Full Stack must not be inserted into deterministic keyword classification.

## LLM and Guard abstraction

Provider SDKs, exceptions, prompt details, and provider-specific mechanics stay inside adapters.

LLM candidate rotation intentionally attempts locally cooled candidates when necessary because local cooldown state is advisory.

The Notification Guard is independently composed and category-aware.

## Notification abstraction

Notification architecture separates:

```text
routing
   ↓
rendering
   ↓
sink semantics
   ↓
transport
```

Notification state is persisted per destination/sink where required.

User routing applies both category and source filters.

## Startup and worker abstraction

There is one production startup sequence in `app/startup.py` and one production worker registry in `app/workers.py`.

The runtime owns resource shutdown.

## Compatibility boundaries

Legacy compatibility mechanisms remain only where existing callers/extensions depend on them, including:

- `app.dependencies.DependencyProxy`
- module-level LLM/Guard functions
- `app.freehub.fetch_projects()`
- `app.llm.*` entry points
- transitional `SQLiteRepository.run()`

These are compatibility boundaries, not the preferred application API.

## Architectural rules

1. New application behavior should depend on semantic ports.
2. Concrete SDK clients belong in adapters/composition.
3. Infrastructure state machines must remain durable where correctness depends on them.
4. Do not introduce unbounded recovery.
5. Do not introduce multi-process SQLite access.
6. Do not turn Full Stack into deterministic classification.
7. Do not change the intentional Telegram 2,000-message bound.
8. Do not change the intentional FreeHub 10-page bound.
9. Do not make local LLM cooldown state authoritative.
10. Preserve idempotency and crash recovery before optimizing throughput.

---

# 25. Current Engineering Follow-ups

These are known considerations for future maintenance; they are not reasons to rewrite the current architecture.

### FreeHub offset pagination

Newest-first page-number pagination is inherently weaker than stable cursor pagination. New upstream inserts can move projects across page boundaries.

Preferred future solution:

```text
stable cursor
```

or, if unavailable:

```text
bounded overlap + durable identity dedup
```

The 10-page bound remains intentional.

### Dependency-injection hygiene

Avoid eager expressions such as:

```python
overrides.pop("api_id", get_api_id())
```

because `get_api_id()` executes even when the override exists.

Prefer explicit lazy resolution.

### Time representation

New durable time fields should prefer UTC-aware timestamps rather than naive local `datetime.now()` values.

### CI maintenance

If multiple CI workflows duplicate dependency bootstrap and smoke-test logic, keep them synchronized or consolidate common steps into reusable workflow components.
