# Loki Freelance Assistant

> A production-oriented freelance-job **ingestion, classification, and
> notification-guard** node. It ingests from FreeHub, Telegram channels, and
> a LinkedIn/Wuzzuf scraper, classifies jobs into reusable category
> profiles, arbitrates ambiguous cases with Gemini/Groq, applies the
> Notification Guard where required, persists the result to Postgres, and
> publishes guard-approved jobs to a Redis stream for a separate
> notification/delivery service.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

## What Loki Does

Loki continuously ingests freelance-job opportunities from configured
sources, normalizes and deduplicates them, classifies them into reusable
category profiles, optionally reviews ambiguous jobs with LLMs, applies the
Notification Guard where required, persists the result, and — for jobs that
pass the guard — publishes a durable event to a Redis stream. A separate
Node.js service consumes that stream and owns everything user-facing:
subscriptions, fan-out, rendering, and Telegram delivery.

## Two-Node Architecture

Loki runs as two cooperating services on two machines:

``` text
┌───────────────────────────────────────────┐      ┌───────────────────────────────┐
│ Node 1 — THIS REPOSITORY (Python)         │      │ Node 2 — separate service     │
│                                           │      │ (Node.js, not this repo)      │
│  ingest → parse → dedup → classify        │      │                               │
│      → LLM arbitration → Guard            │      │  user-facing Telegram bot     │
│      → Postgres persistence               │      │  /start · /categories · /stop │
│      → XADD jobs:notify                   │      │  subscription management      │
│                                           │      │  render + fan-out + delivery  │
└───────────────────┬───────────────────────┘      └───────────────▲───────────────┘
                    │                                              │
                    └──────────── Redis Stream: jobs:notify ───────┘
                                     (at-least-once)

Shared infrastructure: Postgres (persistence) · Redis (stream hand-off) · Tailscale
```

- **Node 1 (this repository)** — Python `asyncio` application. It has **no
  `python-telegram-bot` dependency**. Its responsibility ends at a durable
  `Complete`/`Suppressed` write in Postgres plus a successful Redis
  publish.
- **Node 2** — a separate Node.js service that consumes the stream. It
  owns the user bot, category/source subscriptions, recipient fan-out,
  message rendering, and delivery retries. Its user/subscription/notification
  tables are deliberately **not** defined in this repo's migrations.

### Current capabilities

-   **Telegram channel ingestion** through a Telethon user account
    (oldest→newest watermark semantics with a contiguous-head barrier).
-   **FreeHub polling** through the FreeHub source adapter (Kafiil,
    Freelancer, Mostaql, Nafezly).
-   **LinkedIn / Wuzzuf ingestion** from an in-container scraper that
    writes a curated JSON snapshot on a fixed cadence.
-   **English/Arabic normalization** before classification.
-   **Tiered deterministic classification** using reusable category
    profiles.
-   **Seven registered deterministic category profiles**:
    -   Data Analysis
    -   AI/ML Data Science
    -   Backend Development
    -   Frontend Development
    -   Mobile App Development
    -   Game Development
    -   Graphic Design
-   **Full Stack Development** is registered as an **arbitration-only**
    category. It is not a deterministic classifier category; the guard can
    also reclassify an otherwise-clean single-category match to Full Stack.
-   **Gemini/Groq LLM arbitration** for ambiguous classification (one call
    for the whole candidate set).
-   **Optional Notification Guard** for jobs accepted directly by
    deterministic classification, with a durable per-job decision.
-   **Postgres persistence** for jobs, the category catalog, guard
    decisions, and audit logs, with versioned SQL migrations that run at
    startup.
-   **Redis Streams hand-off** — guard-approved jobs are published to
    `jobs:notify` as a self-contained DTO; the job is only marked
    `Complete` after the publish succeeds.
-   **Internal URL shortener** applied to every accepted project URL.
-   **Crash recovery and retry behavior** for ingestion, state,
    classification, guard, and stream publishing.
-   **Docker deployment** (`postgres` + `loki` compose services) with
    persistent session and state storage.

## Core Architecture

The production dependency graph is assembled in `app/wiring/composition.py`.

``` text
                         ┌──────────────────────────────┐
                         │      Composition Root        │
                         │  app/wiring/composition.py   │
                         └──────────────┬───────────────┘
                                    │
                    configuration + dependency wiring
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          ▼                         ▼                         ▼
   Source adapters             Persistence              LLM/Guard
   ├─ Telegram channels        ├─ Postgres               ├─ Gemini
   ├─ FreeHub                  ├─ JSON state             ├─ Groq
   └─ LinkedIn/Wuzzuf          └─ Redis publish          └─ Guard
          │                         │                         │
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
                                    ▼
                             Notification Guard
                              /            \
                         notify        do_not_notify
                            │                │
                            ▼                ▼
                    Postgres + XADD      Suppressed
                    (jobs:notify)
```

## Two Telegram roles → one

Historically Loki used two Telegram interfaces. That is no longer true in
this node:

1.  **Telethon user account** — reads monitored source channels and
    performs startup/recovery ingestion. This lives in
    `app/adapters/sources/telegram.py`.
2.  **Telegram Bot API** — no longer used here. The user-facing bot and
    notification delivery moved to a separate Node.js service.

## Source Ingestion

Loki supports three ingestion families, all converging on the same
pipeline:

-   **Telegram channels** — Telethon, live events plus durable startup
    recovery, bounded at **2,000 messages** per recovery pass.
-   **FreeHub** — HTTP polling with persisted seen state and bounded
    backfill at **10 pages** per recovery pass.
-   **LinkedIn / Wuzzuf** — a curator process (`scraper/scraper.py`) that
    writes `scraper/jobs_results.json`; the `LinkedInFileJobSource` /
    `WuzzufFileJobSource` adapters poll that snapshot. The scraper runs
    **inside the container** on the same cadence as the pollers (300s).

## Category Architecture

Category-specific knowledge is isolated under `app/domain/categories/`.

``` text
app/domain/categories/
├── registry.py
├── profile.py
├── ai_ml/
├── backend/
├── data_analysis/
├── frontend/
├── full_stack/        # arbitration-only
├── game_dev/
├── graphic_design/
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

-   `enabled()` — all enabled profiles.
-   `deterministic()` — enabled profiles allowed to participate in
    deterministic classification.
-   `arbitration_only()` — enabled profiles reserved for LLM arbitration.

This is why Full Stack can be available to the arbitration (and guard)
layer without becoming a deterministic classifier.

### Current category policy

| Category | Deterministic | LLM Arbitration |
|---|---:|---:|
| Data Analysis | Yes | Yes |
| AI/ML Data Science | Yes | Yes |
| Backend Development | Yes | Yes |
| Frontend Development | Yes | Yes |
| Mobile App Development | Yes | Yes |
| Game Development | Yes | Yes |
| Graphic Design | Yes | Yes |
| Full Stack Development | **No** | **Yes** |

Full Stack is intentionally arbitration-only. Adding a category means
adding a package under `app/domain/categories/`; the registry discovers it
automatically.

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
   (optionally reclassify to full_stack)
         │
         ▼
      publish
```

Jobs that already went through LLM arbitration do not require a second
Guard review.

The guard's decision (`notify` / `do_not_notify`) is **durable per job**:
once persisted it is reused forever, including across restarts and retry
sweeps, and the provider is never asked again for that job. A transient
`error` is not treated as a decision and is re-evaluated. The guard is
fail-closed.

Guard prompts are category-owned and the Guard engine remains shared.
Enable with:

``` text
NOTIFICATION_GUARD_ENABLED=true
```

The guarded entry point (`python run_guarded.py`) forces the guard on.

## Persistence and the Redis Hand-off

Postgres is the durable store for this node:

``` text
jobs
categories              (category catalog)
notification_guard
gemini                  (append-only LLM audit log)
errors                  (append-only error log)
schema_migrations
```

Telegram watermarks, FreeHub seen state, and source identity/dedup state
are additionally persisted in JSON:

``` text
database/state.json
```

The state writer uses atomic replacement and backup/recovery semantics.

### Publishing to the downstream service

When a job is approved, the node builds a canonical DTO
(`build_notification_event`) from a **fresh read of the durable row** and
publishes it to the Redis stream `jobs:notify` (default; override with
`JOB_STREAM_NAME`). The DTO fields are fixed:

``` text
Job UUID, Title, Description, Source, URL, Short URL,
Decision Reason, Categories, Category ID, Category Selection Method
```

Ordering is the crash-safety invariant: **publish happens before the job
is marked `Complete`**. If the publish fails, `Notification Status` stays
`Pending` and the retry sweep republishes it. `Job UUID` is the consumer's
idempotency key (delivery is at-least-once).

### URL shortener

Every accepted project URL is shortened before the job row is created, via
an internal service (`URL_SHORTENER_DOMAIN` + `URL_SHORTENER_ENDPOINT`).
The shortener is an idempotent upsert keyed by `jobId`. `URL_SHORTENER_FAILURE_MODE`
is `fail_open` (default — keep the original URL) or `fail_closed` (retry
the job on the source's next poll).

## Recovery Guarantees and Bounds

### Telegram recovery

Telegram startup/recovery has a hard maximum of **2,000 messages per
recovery pass**. Reaching the cap is not proof of catch-up: a `CAPPED`
pass keeps the channel's live barrier closed until a later pass completes.

### FreeHub recovery

FreeHub backfill is intentionally capped at **10 pages** per bounded
recovery operation. Because FreeHub uses page/offset pagination, this is a
resource bound rather than a historical-completeness guarantee; durable
project identity/dedup provides the real protection.

### Scraper scheduler

The in-container scraper scheduler runs a single scrape immediately, then
on the source poll cadence. A single transient failure leaves it `alive`
(it keeps retrying); **3 consecutive failed runs** report it `dead` so the
container healthcheck stops reporting healthy while LinkedIn/Wuzzuf data
goes stale. It self-heals on the next successful run.

### LLM cooldown behavior

Local LLM cooldown tracking is advisory. If all configured candidates are
locally marked as cooling down, Loki still attempts the candidates.
Provider-side state is authoritative.

### Total LLM outage

If a required LLM review cannot be completed by the configured providers,
classification fails closed (job left durably `Pending`) rather than
retrying indefinitely.

## Security Boundaries

-   External job content is treated as untrusted input.
-   LLM prompts distinguish system instructions from untrusted job content.
-   Notification URLs are validated before being used as buttons.
-   Secrets are supplied through configuration/environment rather than
    job data.
-   FreeHub currently uses HTTP because that upstream transport is
    intentional. This remains a transport security boundary and should be
    revisited if the upstream offers HTTPS.

## Installation

### Requirements

-   Python 3.11+
-   Postgres (or the compose `postgres` service)
-   Redis (reachable; hosted on the other node in production)
-   Telegram API ID/hash and a Telethon user account
-   Gemini API key(s) and/or Groq API key
-   FreeHub user ID/configuration
-   URL shortener domain/endpoint

### Local setup

``` bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

``` powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and provide the required
credentials/configuration. At minimum set `DATABASE_URL`, `REDIS_URL`, and
the `URL_SHORTENER_*` values; the composition root refuses to start
without them.

### First Telegram login

``` bash
python run_guarded.py    # pipeline + Notification Guard
```

or:

``` bash
python run.py            # standard pipeline
```

The first Telethon login is interactive. The resulting session is
persisted for subsequent runs. Database migrations run automatically at
startup; you can also run them manually:

``` bash
DATABASE_URL=... python scripts/migrate_postgres.py
```

## Testing

Preferred:

``` bash
./scripts/test.sh
```

Manual:

``` bash
pip install -r requirements.txt
pytest tests/ -q
```

The suite covers major areas including:

-   parsing and normalization
-   deterministic classification and category registry
-   LLM response validation, provider rotation, and guard providers
-   Postgres persistence, migrations, and normalization
-   job identity/deduplication and cross-source identity
-   Telegram lifecycle, recovery, and reconnection
-   FreeHub polling/backfill
-   LinkedIn/Wuzzuf scraper file source and scheduler
-   Redis publish/replay and notification DTO shape
-   URL shortening
-   JSON state recovery
-   worker liveness, heartbeat, and healthcheck behavior

Live provider tests require valid credentials.

## Docker

The deployment includes Docker/Compose configuration. Compose starts:

-   `postgres` — `postgres:16-alpine`, with a healthcheck and a host
    port published for host-side tooling.
-   `loki` — the application image
    (`mohamedbasem2/loki-freelance-assistant:latest`), guarded entry point
    by default, a Docker healthcheck, and bind mounts for `./sessions`
    and `./database`.

Redis lives **outside** the compose file (set `REDIS_URL` in `.env`).
Persistent storage must cover at least:

-   Telegram session (`./sessions`)
-   JSON state (`./database`)

Postgres supports concurrent writers, so job/audit persistence imposes no
single-writer constraint. The `sessions` and `database` bind mounts are
still plain files — do not scale the `loki` service to multiple replicas
sharing them.

## Project Structure

``` text
app/
├── adapters/            # concrete SDK/IO implementations behind each port
│   ├── http/
│   ├── parsers/
│   ├── repositories/    # Postgres repository + migration runner
│   ├── sources/         # freehub, telegram, scraper_file
│   ├── state/           # JSON state + dedup adapters
│   └── streams/         # Redis stream publisher
├── domain/              # pure domain logic
│   ├── categories/      # category-owned keywords/prompts/profiles
│   ├── classification.py
│   ├── filters.py
│   └── normalize.py
├── infra/               # config, healthcheck, heartbeat, timeouts, URL shortener
├── llm/                 # provider registry, Gemini/Groq, rotation, rate limits
├── notification_guard/  # guard engine, provider registry, Groq guard, integration
├── services/            # stateful domain services (job_processor, state, parser, ...)
├── wiring/              # composition root and process/worker orchestration
│   ├── composition.py
│   ├── dependencies.py
│   ├── scraper_scheduler.py
│   ├── source_worker.py
│   ├── startup.py
│   └── workers.py
├── bot.py
├── ports.py
└── handlers/            # compatibility seams (e.g. telegram)

config/project.json      # deployment-independent configuration baseline
migrations/postgres/     # versioned schema migrations
scraper/                 # in-container LinkedIn/Wuzzuf scraper
database/                # JSON state (runtime)
tests/
scripts/                 # migrate_postgres.py, ci_smoke_test.py, test.sh
Dockerfile
docker-compose.yml
.env.example
requirements.txt
run.py
run_guarded.py
README.md
DOCUMENTATION.md
```

## Architecture Principles

Loki favors:

-   deterministic rules before LLM calls
-   explicit category profiles
-   one final category per job
-   durable state over in-memory assumptions
-   bounded concurrency
-   publish-before-complete for downstream hand-off
-   fail-closed classification when required review cannot be completed
-   semantic application ports with concrete adapters behind them
-   a single production composition root
-   explicit compatibility seams rather than accidental infrastructure
    imports

## Known Engineering Follow-ups

These are documented engineering considerations, not reasons to remove the
intentional design bounds above:

1.  **FreeHub offset pagination:** page-number continuation can be affected
    by newly inserted upstream projects. Stable cursors would be
    preferable if the upstream provides them; otherwise bounded overlap
    plus existing identity deduplication is the safer future improvement.
2.  **Redis delivery is at-least-once:** the downstream service must
    deduplicate on `Job UUID`. Any change to the published DTO fields
    (`NOTIFICATION_DTO_FIELDS`) is a contract change for that service.
3.  **Architecture documentation:** keep diagrams and examples
    synchronized with the current adapter/registry structure.
4.  **Time representation:** prefer explicit UTC-aware timestamps for new
    persistence/retry fields.

## License

MIT
