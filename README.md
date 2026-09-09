# Loki Freelance Assistant

> A production-oriented freelance-job monitoring and Telegram
> notification system built around a durable, single-process Python
> runtime.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

## What Loki Does

Loki continuously ingests freelance-job opportunities from configured
sources, normalizes and deduplicates them, classifies them into reusable
category profiles, optionally reviews ambiguous jobs with LLMs, applies
the Notification Guard where required, persists the result, and routes
accepted jobs to Telegram destinations and subscribed users.

### Current capabilities

-   **Telegram ingestion** through a Telethon user account.
-   **FreeHub polling** through the FreeHub source adapter.
-   **English/Arabic normalization** before classification.
-   **Tiered deterministic classification** using reusable category
    profiles.
-   **Six registered category profiles**:
    -   Data Analysis
    -   AI/ML Data Science
    -   Backend Development
    -   Frontend Development
    -   Mobile App Development
    -   Game Development
-   **Full Stack Development** is registered as an **arbitration-only**
    category. It is not a deterministic classifier category.
-   **Gemini/Groq LLM arbitration** for ambiguous classification.
-   **Optional Notification Guard** for jobs accepted directly by
    deterministic classification.
-   **SQLite persistence** for jobs, decisions, users, subscriptions,
    and notification state.
-   **Telegram user subscriptions** through `/start`, `/categories`, and
    `/stop`.
-   **Durable user notification queue** with bounded concurrent
    delivery.
-   **Crash recovery and retry behavior** for ingestion, state,
    classification, and notifications.
-   **Docker deployment** with persistent session, database, and state
    storage.

## Core Architecture

The production dependency graph is assembled in `app/composition.py`.

``` text
                         ┌──────────────────────┐
                         │   Composition Root   │
                         │ app/composition.py   │
                         └──────────┬───────────┘
                                    │
                    configuration + dependency wiring
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          ▼                         ▼                         ▼
   Source adapters             Persistence              LLM/Guard
   ├─ Telegram                 ├─ SQLite                 ├─ Gemini
   └─ FreeHub                  └─ JSON state             ├─ Groq
          │                         │                    └─ Guard
          └─────────────────────────┼─────────────────────────┘
                                    ▼
                           Shared job pipeline
                                    │
                     parse → normalize → identity
                                    │
                                    ▼
                               deduplication
                                    │
                                    ▼
                         deterministic classifier
                              /            \
                         confident       ambiguous
                            │                │
                            │                ▼
                            │         one LLM arbitration
                            │                │
                            └───────┬────────┘
                                    ▼
                              final category
                                    │
                           ┌────────┴─────────┐
                           ▼                  ▼
                    fixed destination   subscriber routing
                           │                  │
                           ▼                  ▼
                    notification       durable user queue
                    service/Guard             │
                                              ▼
                                        Telegram Bot API
```

## Two Telegram roles

Loki uses two Telegram interfaces in the same process:

1.  **Telethon user account** --- reads monitored source channels and
    performs startup/recovery ingestion.
2.  **Telegram Bot API** --- handles the user-facing bot and sends
    notifications to users/destinations.

They have separate responsibilities and separate lifecycles.

## Category Architecture

Category-specific knowledge is isolated under `app/categories/`.

``` text
app/categories/
├── registry.py
├── profile.py
├── ai_ml/
├── backend/
├── data_analysis/
├── frontend/
├── full_stack/        # arbitration-only
├── game_dev/
└── mobile_app/
```

Each category package contains:

``` text
profile.py
keywords.py
llm_prompt.py
guard_prompt.py
```

The shared classifier does not hard-code domain-specific keyword lists.

### Deterministic vs arbitration-only categories

The registry exposes three views:

-   `enabled()` --- all enabled profiles.
-   `deterministic()` --- enabled profiles allowed to participate in
    deterministic classification.
-   `arbitration_only()` --- enabled profiles reserved for LLM
    arbitration.

This is why Full Stack can be available to the arbitration layer without
becoming a deterministic classifier.

## Classification Model

A job receives **one final category or no category**.

``` text
Job
 │
 ▼
deterministic classification
 │
 ├── confident ───────────────► final category
 │
 └── ambiguous
          │
          ▼
   ONE arbitration call
          │
          ▼
   one candidate ID or none
```

The LLM is not called once per category. Candidate categories are
evaluated together in one arbitration request.

## Notification Guard

The optional Notification Guard is applied to jobs accepted directly by
deterministic classification.

``` text
direct deterministic acceptance
             │
             ▼
       Notification Guard
          /          \
      notify      do_not_notify
         │
         ▼
     delivery
```

Jobs that already went through LLM arbitration do not require a second
Guard review.

Guard prompts are category-owned and the Guard engine remains shared.

Enable with:

``` text
NOTIFICATION_GUARD_ENABLED=true
```

## User Subscriptions

The Telegram user bot supports:

-   `/start` --- register/activate the user and configure subscriptions.
-   `/categories` --- change category subscriptions.
-   `/stop` --- deactivate the user and cancel undelivered queued
    notifications.

Users may subscribe to multiple categories.

Source preferences are also supported. An empty source preference means
all sources.

Configured public channel destinations use the same category/source
filtering semantics as user destinations.

## Notification Delivery

Accepted jobs are routed into durable notification state.

``` text
final category
      │
      ▼
routing / subscription matching
      │
      ▼
user_notifications
      │
      ▼
bounded delivery workers
      │
      ▼
Telegram Bot API
```

Delivery is persisted and idempotency is tracked per destination/sink so
retries do not blindly resend already-completed deliveries.

Current delivery defaults include:

``` text
DELIVERY_CONCURRENCY = 10
BATCH_SIZE            = 20
MAX_ATTEMPTS          = 5
```

## Persistence Model

SQLite is the primary durable store.

Logical areas include:

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

Telegram and FreeHub source state is additionally persisted in:

``` text
database/state.json
```

The state writer uses atomic replacement and backup/recovery semantics.

### Single-process invariant

Loki intentionally uses:

``` text
one application process
one SQLite database
one serialized DB worker
one serialized JSON state worker
```

Do **not** run multiple Loki replicas against the same SQLite/state
volume.

The application is designed to scale its configured sources/categories
and bounded in-process workers, not by attaching multiple processes to
one database file.

## Recovery Guarantees and Bounds

### Telegram recovery

Telegram startup/recovery has a hard maximum of **2,000 messages per
recovery pass**.

This limit is intentional. If recovery cannot complete within the bound,
the durable watermark/recovery barrier remains positioned so that a
later retry can continue recovery rather than silently declaring the
channel caught up.

The 2,000-message bound must not be removed merely to make recovery
appear more complete in a single pass.

### FreeHub recovery

FreeHub backfill is intentionally capped at **10 pages** per bounded
recovery operation.

The cap is a resource/safety bound, not an assertion that the upstream
feed has no older data.

Because FreeHub uses newest-first offset/page pagination, page numbers
can shift when new projects are inserted. The project therefore treats
pagination continuation as a known upstream limitation and relies on
durable project identity/deduplication. If strict no-gap historical
recovery becomes a requirement, a stable upstream cursor or
overlap-based continuation should be introduced.

### LLM cooldown behavior

Local LLM cooldown tracking is advisory.

If all configured candidates are locally marked as cooling down, Loki
still attempts the candidates. This is intentional: provider-side state
is authoritative and the system must avoid missing jobs because a local
cooldown estimate is stale.

### Total LLM outage

If a required LLM review cannot be completed by the configured
providers, classification fails closed rather than retrying
indefinitely.

## Security Boundaries

-   External job content is treated as untrusted input.
-   Telegram HTML is escaped and bounded before delivery.
-   Notification URLs are validated before being used as buttons.
-   LLM prompts distinguish system instructions from untrusted job
    content.
-   Secrets are supplied through configuration/environment rather than
    job data.
-   FreeHub currently uses HTTP because that upstream transport is
    intentional. This remains a transport security boundary and should
    be revisited if the upstream offers HTTPS.

## Installation

### Requirements

-   Python 3.11+
-   Telegram API ID/hash
-   Telethon user account
-   Telegram Bot token
-   Gemini API key(s)
-   Groq API key
-   FreeHub user ID/configuration

### Local setup

``` bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Windows PowerShell:

``` powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

Copy `.env.example` to `.env` and provide the required
credentials/configuration.

### First Telegram login

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first Telethon login is interactive. The resulting session is
persisted for subsequent runs.

## Testing

Preferred:

``` bash
./scripts/test.sh
```

Manual:

``` bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -q
```

The suite covers major areas including:

-   parsing and normalization
-   deterministic classification
-   category registry behavior
-   LLM response validation and provider rotation
-   SQLite persistence and migrations
-   job identity/deduplication
-   Telegram lifecycle and recovery
-   FreeHub polling/backfill
-   notification state and retries
-   Notification Guard
-   user routing and subscriptions
-   HTML/message safety
-   JSON state recovery
-   worker liveness and concurrency behavior

Live provider tests require valid credentials.

## Docker

The deployment includes Docker/Compose configuration.

Persistent storage must cover at least:

-   Telegram session
-   SQLite database
-   JSON state

Do not share one SQLite/state volume between multiple Loki containers.

## Project Structure

``` text
app/
├── adapters/
│   ├── http/
│   ├── notifications/
│   ├── parsers/
│   ├── repositories/
│   ├── sources/
│   ├── state/
│   ├── transports/
│   └── user/
├── categories/
│   ├── ai_ml/
│   ├── backend/
│   ├── data_analysis/
│   ├── frontend/
│   ├── full_stack/
│   ├── game_dev/
│   ├── mobile_app/
│   ├── profile.py
│   └── registry.py
├── llm/
├── notification_guard/
├── recovery/
├── repositories/
├── bot.py
├── classification.py
├── composition.py
├── config.py
├── job_processor.py
├── message_processor.py
├── notifier.py
├── normalize.py
├── ports.py
├── routing.py
├── runtime_config.py
├── source_worker.py
├── startup.py
├── state.py
├── telegram_bot.py
├── user_bot.py
└── workers.py

database/
tests/
scripts/
Dockerfile
docker-compose.yml
.env.example
requirements.txt
requirements-dev.txt
run.py
run_guarded.py
README.md
DOCUMENTATION.md
ABSTRACTION_ARCHITECTURE.md
PORTS.md
```

## Architecture Principles

Loki favors:

-   deterministic rules before LLM calls
-   explicit category profiles
-   one final category per job
-   durable state over in-memory assumptions
-   bounded concurrency
-   idempotent notification delivery
-   fail-closed classification when required review cannot be completed
-   semantic application ports
-   concrete SDK/persistence implementations behind adapters
-   a single production composition root
-   explicit compatibility seams rather than accidental infrastructure
    imports

## Known Engineering Follow-ups

These are documented engineering considerations, not reasons to remove
the intentional design bounds above:

1.  **FreeHub offset pagination:** page-number continuation can be
    affected by newly inserted upstream projects. Stable cursors would
    be preferable if the upstream provides them; otherwise bounded
    overlap plus existing identity deduplication is the safer future
    improvement.
2.  **Dependency-injection defaults:** avoid eager expressions such as
    `overrides.pop("x", get_x())` when a getter should not execute if an
    override is supplied.
3.  **Architecture documentation:** keep diagrams and examples
    synchronized with the current adapter/registry structure.
4.  **Time representation:** prefer explicit UTC-aware timestamps for
    new persistence/retry fields.
5.  **CI maintenance:** keep duplicated workflow bootstrap/smoke logic
    synchronized or consolidate it into reusable workflow components.

## License

MIT

## Architecture & Abstraction Boundaries

The production dependency graph is assembled in `app/composition.py`.

Application code depends on semantic boundaries such as:

- `JobSource`
- `JobRepository`
- `StateStore`
- `DedupStore`
- `NotificationSink`
- `NotificationTransport`
- `LLMProvider`

Concrete SDKs, HTTP clients, SQLite execution, and JSON file I/O remain behind adapters.

### Current category policy

| Category | Deterministic | LLM Arbitration |
|---|---:|---:|
| Data Analysis | Yes | Yes |
| AI/ML Data Science | Yes | Yes |
| Backend Development | Yes | Yes |
| Frontend Development | Yes | Yes |
| Mobile App Development | Yes | Yes |
| Game Development | Yes | Yes |
| Full Stack Development | **No** | **Yes** |

Full Stack is intentionally arbitration-only.

### Runtime invariants

- One application process.
- One SQLite database.
- One serialized DB worker.
- One serialized JSON state worker.
- Telegram recovery capped at 2,000 messages per pass.
- FreeHub recovery capped at 10 pages per bounded operation.
- Local LLM cooldown is advisory; locally cooled candidates may still be attempted.
- FreeHub HTTP is an intentional upstream constraint.

For the complete module/port architecture and compatibility-boundary details, see the **Abstraction Architecture** section of `DOCUMENTATION.md`.
