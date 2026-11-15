# Loki Freelance Assistant

> Technical architecture and operations guide for the current two-node
> implementation of Loki: the Python ingestion/classification/guard node
> in this repository, plus a separate Node.js notification/delivery
> service.

Loki is a resilient freelance-job aggregation, classification, and
notification-guard system. It collects opportunities from multiple
sources, converts them into a common job model, deduplicates them,
classifies them against registered categories, arbitrates ambiguous cases
when required, applies the Notification Guard, and publishes durable
events to a Redis stream for a separate service to fan out and deliver.

Within **this repository**, the system is intentionally designed as a
**single-process `asyncio` application** with Postgres persistence, JSON
state, bounded workers, and durable recovery mechanisms. Notification
delivery itself is deliberately **not** part of this process any more.

------------------------------------------------------------------------

## Table of Contents

1.  [System Overview](#1-system-overview)
2.  [Runtime Architecture](#2-runtime-architecture)
3.  [Data Flow](#3-data-flow)
4.  [Source Ingestion](#4-source-ingestion)
5.  [Telegram Ingestion and Recovery](#5-telegram-ingestion-and-recovery)
6.  [FreeHub Ingestion and Recovery](#6-freehub-ingestion-and-recovery)
7.  [LinkedIn and Wuzzuf Scraper](#7-linkedin-and-wuzzuf-scraper)
8.  [Parsing, Normalization, and Identity](#8-parsing-normalization-and-identity)
9.  [Classification Architecture](#9-classification-architecture)
10. [LLM Subsystem](#10-llm-subsystem)
11. [Notification Guard](#11-notification-guard)
12. [Job Processing Pipeline](#12-job-processing-pipeline)
13. [Users, Categories, and Subscriptions](#13-users-categories-and-subscriptions)
14. [Notification Routing and Delivery](#14-notification-routing-and-delivery)
15. [Persistence](#15-persistence)
16. [Concurrency, Leases, and Race Prevention](#16-concurrency-leases-and-race-prevention)
17. [Failure and Recovery Model](#17-failure-and-recovery-model)
18. [Health, Observability, and Deployment](#18-health-observability-and-deployment)
19. [Testing](#19-testing)
20. [Adding and Maintaining Categories](#20-adding-and-maintaining-categories)
21. [Design Decisions and Known Boundaries](#21-design-decisions-and-known-boundaries)

------------------------------------------------------------------------

# 1. System Overview

Loki is split across **two cooperating nodes**:

1.  **Node 1 — this repository (Python).** Ingests and evaluates
    freelance opportunities, then publishes guard-approved jobs to a
    Redis stream as a durable hand-off.
2.  **Node 2 — a separate Node.js service.** Owns the user-facing
    Telegram bot, subscriptions, fan-out, rendering, and delivery.

``` text
+----------------------------------------------+
| Node 1 - this repository (Python)            |
|                                              |
|  Sources -> pipeline -> classification       |
|                |          |                  |
|                |          v                  |
|                |     LLM arbitration         |
|                |          |                  |
|                |          v                  |
|                |     Notification Guard      |
|                |       /        \            |
|                |   notify    do_not_notify   |
|                |      |            |         |
|                |      v            v         |
|                |  Postgres +    Suppressed   |
|                |  XADD jobs:notify           |
+----------------|------------------------------+
                 | Redis Stream (at-least-once)
                 v
+----------------------------------------------+
| Node 2 - separate service (Node.js)          |
|  consume -> route -> render -> deliver       |
|  own user / subscription / notification DB   |
+----------------------------------------------+
```

The key architectural property is unchanged from the earlier single-node
design: **sources are isolated from downstream processing**. A source only
needs to produce a normalized job candidate; the same processing pipeline
is reused regardless of where the job originated. What changed is where
that pipeline *ends*: at a durable Postgres state plus a Redis publish,
rather than at a Telegram send.

### Core principle

``` text
Sources
   |
   v
Common job model
   |
   v
One processing pipeline
   |
   v
One durable decision
   |
   v
One durable stream event
   |
   v
(Many notification destinations, in Node 2)
```

The central application boundary remains:

``` text
app.services.job_processor.process_job()
```

and the point at which a job leaves this node is:

``` text
app.services.job_processor._publish_and_mark_complete()
```

------------------------------------------------------------------------

# 2. Runtime Architecture

Loki runs as one Python process with several long-lived asynchronous
activities.

``` text
Application startup (app.bot.run)
      |
      +--------------> Startup steps
      |                  initialize_database (migrations)
      |                  state.load()
      |
      +--------------> Telegram worker ---------+
      |                (TelegramChannelWorker)  |
      |                                          |
      +--------------> FreeHub source worker ----+
      |                                          |
      +--------------> LinkedIn source worker ---+
      |                                          |
      +--------------> Wuzzuf source worker -----+
      |                                          |
      +--------------> Scraper scheduler -------+  (only when a
      |                                          |   scraper_file source
      |                                          |   is enabled)
      +--------------> Classification retry ----+
      |                                          |
      +--------------> Notification retry ------+
      |                                          v
      |                                   Shared Job Pipeline
      |                                          |
      |                       +------------------+------------------+
      |                       v                                     v
      |                    Postgres                           JSON state
      |                       ^                                     ^
      |                       |                                     |
      |            retry workers / pipeline              source watermarks
      |
      +--------------> Heartbeat worker ------------> heartbeat file
                                                          |
                                              Healthcheck subprocess
```

All workers are registered in `app.wiring.workers.WorkerRegistry`, which
runs them under an `asyncio.TaskGroup` (structured concurrency). If any
worker raises, its siblings are cancelled and the whole process shuts
down in a controlled way rather than leaving orphaned tasks. Registered
shutdown hooks run exactly once.

## Main runtime responsibilities

  -----------------------------------------------------------------------
  Component                           Responsibility
  ----------------------------------- -----------------------------------
  Telegram worker                     Live Telegram ingestion, startup
                                      recovery, bounded reconnect

  Source workers                      Generic poll loop per configured
  (freehub/linkedin/wuzzuf)           `JobSource`; parse -> `process_job()`

  Scraper scheduler                   Runs the in-image LinkedIn/Wuzzuf
                                      scraper as a subprocess and keeps
                                      the JSON snapshot fresh

  Classification retry worker         Retries durably `Pending`
                                      classifications

  Notification retry worker           Re-drives jobs left before
                                      `Complete`/`Suppressed`

  Heartbeat worker                    Serializes per-worker liveness to
                                      the heartbeat file

  Postgres repository                 Durable jobs/guard/audit storage

  State manager                       Durable source watermarks and
                                      source-specific dedup state

  Healthcheck subprocess              Detects dead/stale workers and
                                      quarantine, plus Postgres/Redis
  -----------------------------------------------------------------------

> Note: `user_notifications` may still appear in the legacy
> `ENABLED_WORKERS` list, but no such worker is registered here any more -
> subscriber delivery moved to Node 2. The `heartbeat` worker is always
> registered.

------------------------------------------------------------------------

# 3. Data Flow

## 3.1 Complete job lifecycle (this node)

``` text
Source
  |
  v
Parser / normalizer
  |
  v
Job processor
  |
  v
Postgres identity / dedup check
  |
  +-------------- Duplicate --------------> Stop
  |
  v New job
Create durable job row (original URL)
  |
  v
Classification
  |
  +-------------- Confident --------------+
  |                                       |
  +-------------- Ambiguous --> LLM ------+
                                          v
                                  Notification Guard
                                     /          \
                                 notify     do_not_notify
                                    |             |
                                    v             v
                            URL shorten +     Suppressed
                            XADD jobs:notify
                                    |
                                    v
                            mark Notification Status
                                = "Complete"
                                    |
                                    v
                            (Node 2 fan-out/delivery)

Duplicate jobs return without entering the downstream pipeline.
```

## 3.2 State ownership

``` text
Job facts
(classification, guard decision, notification status)
          |
          v
     Postgres
          |
    +-----+---------------+
    v                     v
Job recovery      Guard/decision recovery
(classification   (durable notify /
 retry)            do_not_notify)

Telegram watermarks
FreeHub seen state
Cross-source identity claims
          |
          v
Persistent JSON state
          |
          v
Source recovery
```

Postgres is the source of truth for durable job and guard state. JSON
state is used for source-specific progress that does not belong in the
job database.

------------------------------------------------------------------------

# 4. Source Ingestion

Loki currently supports three ingestion families:

``` text
Telegram channels
      |
      v
Telegram adapter
      |
      +---------------------+
      |                     |
      v                     |
Common job model <----------+
      ^                     |
      |                     |
FreeHub adapter             |
      |                     |
      +---------------------+

LinkedIn / Wuzzuf
      |
      v
In-container scraper -> JSON snapshot
      |
      v
Scraper file adapter
      |
      +--------------> Common job model
                              |
                              v
                        process_job()
```

The source boundary isolates transport and source-specific quirks from
classification and notification logic.

### Source characteristics

-   **Telegram:** event-driven live ingestion plus durable startup
    recovery.
-   **FreeHub:** HTTP polling with persisted seen state and bounded
    recovery. Upstream platforms: Kafiil, Freelancer, Mostaql, Nafezly.
-   **LinkedIn/Wuzzuf:** an in-container scraper process producing a
    curated JSON snapshot that file-backed adapters poll.

### Configuration

Sources are configured in `config/project.json` under `job_sources` and
selected/overridden in the environment (`JOB_SOURCES`, `FREEHUB_SOURCES`,
`JOB_SOURCE_<ID>_POLL_INTERVAL`, `JOB_SOURCE_<ID>_ENABLED`). A job source
runs only when it is both `enabled` and present in `ENABLED_WORKERS`.

------------------------------------------------------------------------

# 5. Telegram Ingestion and Recovery

Telegram ingestion uses Telethon as a logged-in Telegram user. There is
exactly **one** canonical implementation
(`app.adapters.sources.telegram`); `app.handlers.telegram` is a thin
compatibility re-export. The Telegram Bot API is no longer used in this
node.

## 5.1 Startup flow

``` text
Application starts
      |
      v
Load Telegram watermarks (JSON state)
      |
      v
Warm entity cache + resolve canonical chat ids
      |
      v
Acquire per-channel locks - register live handler
      |
      v
Recover missed messages
oldest -----------------> newest
      |
      v
Release barrier on COMPLETE
      |
      v
Process new live events
      |
      v
Persist progress
```

## 5.2 Live message flow

``` text
Telegram
   |
   v
Live NewMessage handler
   |
   v
Per-channel lock (serialization)
   |
   v
Message processor
   |
   v
Job processor
   |
   +--------> Postgres identity / processing
   |
   +--------> Source state / watermark
                 |
                 v
           Durable progress
```

## 5.3 Recovery

Telegram recovery uses durable per-channel watermarks with
oldest->newest ordering and a contiguous-head guarantee:

``` text
persisted watermark
        |
        v
find messages after watermark
        |
        v
recover in chronological order
        |
        v
process through normal pipeline
        |
        v
advance watermark only for the
contiguous confirmed head
```

The recovery pass is intentionally bounded at **2,000 messages**.
Reaching that cap produces a `CAPPED` outcome - not proof of catch-up.
Only a `COMPLETE` pass releases the channel's live barrier, so a live
message can never advance the watermark past still-unrecovered messages.

``` text
Older messages
      |
      v
   Recovery
      |
      +------------------+
      |                  |
      v                  v
Recovery barrier <---- Live events
      |
      v
Shared job pipeline
      |
      v
Durable watermark
```

The goal is:

> **Never let a newly received event make an older unrecovered message
> permanently unreachable.**

A transient disconnect triggers a bounded exponential reconnect; fatal
auth/session/identity errors propagate and shut the process down.

------------------------------------------------------------------------

# 6. FreeHub Ingestion and Recovery

FreeHub is accessed over HTTP by design.

The worker polls the configured endpoint, converts projects into Loki's
common job model, and sends them through the same downstream pipeline as
Telegram jobs.

## 6.1 Polling flow

``` text
Timer
  |
  v
FreeHub HTTP request
  |
  v
Parse projects
  |
  v
Compare persistent seen state
  |
  v
New projects?
  |
 +-+---------------+
 No                Yes
 |                  |
 v                  v
Persist progress   Convert to common job
 / sleep              |
 |                    v
 +--------------> process_job()
                      |
                      v
               Persist seen state
                      |
                      v
                    Sleep
                      |
                      +--------> Timer
```

A project stays in FreeHub's durable pending queue until
`process_job()` returns successfully; a `ClassificationPendingError`
leaves it unseen so the retry sweeps own it rather than the poll loop.

## 6.2 Bounded recovery

FreeHub recovery is intentionally capped at **10 pages per pass**.

``` text
poll
 |
 v
continue from persisted pagination state
 |
 v
fetch page
 |
 v
process unseen projects
 |
 v
stop when caught up
 |
 v
reset continuation state
```

Because FreeHub uses page/offset-style pagination, newly inserted
projects can shift page boundaries. The 10-page bound therefore provides
bounded backfill, not a mathematical guarantee of historical
completeness. Durable project identity/deduplication is the real
protection.

------------------------------------------------------------------------

# 7. LinkedIn and Wuzzuf Scraper

LinkedIn and Wuzzuf are handled by a scraper that now ships **inside the
image** (`scraper/scraper.py`). There is no host-side cron or host script:
a registered worker (`app.wiring.scraper_scheduler`) runs the scraper as a
subprocess on the same cadence the file adapters poll its snapshot.

``` text
scraper_scheduler worker
      |
      v
Launch scraper subprocess (scraper/scraper.py)
      |
   +--+--------------+
   v                 v
LinkedIn           Wuzzuf
   |                 |
   +--------+--------+
            v
     Discover listings
            |
            v
      Open detail pages
            |
            v
   Extract / curate details
            |
            v
      Normalize records
            |
            v
   scraper/jobs_results.json
            |
            v
   LinkedInFileJobSource /
   WuzzufFileJobSource
            |
            v
    Shared Job Pipeline
```

Why a subprocess rather than an import:

-   The scraper drives `scrapling`/`curl_cffi` and runs its own event
    loop; isolating it keeps its network work out of the app's loop and
    out of the Postgres worker thread. A hung scrape is hard-killed after
    a timeout (600s) and only the child dies.
-   The application never imports `scrapling`, so app imports still work
    if only `requirements.txt` is installed.

The scraper is intentionally curated around detail extraction rather
than treating a search-result card as a complete job. LinkedIn guest
search is restricted to the newest postings (newest-first, last 24h) and
only head-slices detail fetches; a Wuzzuf description can legitimately
reach the scraper's 200K-char cap.

## Scheduler health

A scraper subprocess can fail while its parent loop remains alive. The
scheduler therefore tracks consecutive failed runs.

``` text
Scraper scheduler
      |
      v
Run
      |
      v
Successful?
  |         |
 Yes        No
  |         |
  v         v
Alive    Failed run
             |
             v
      3 consecutive failures?
          |           |
         No          Yes
          |           |
          v           v
      Keep retrying   Dead
                         |
                         v
                  Health becomes
                    unhealthy

A later successful run returns the scheduler to Alive.
```

This distinguishes:

``` text
scheduler process is alive
```

from:

``` text
scraper data is actually being refreshed
```

The distinction is surfaced through worker liveness and container
health.

------------------------------------------------------------------------

# 8. Parsing, Normalization, and Identity

## 8.1 Parsing

Source adapters extract a common set of fields:

``` text
title
description
budget / metadata where available
URL
source
source identifier
```

## 8.2 Normalization

Normalization prepares text for deterministic matching while preserving
the original job content for storage and LLM review.

Typical operations include:

-   lowercasing
-   punctuation/separator normalization
-   whitespace cleanup
-   Arabic character normalization
-   diacritic handling
-   consistent textual representation

``` text
Raw source text
      |
      v
Parse fields
      |
   +--+---------------+
   v                  v
Normalize        Original job content
   |                  |
   v                  +------> Storage
Keyword classification
   |
   +--------------> Positive / supporting evidence
   |
   +--------------> Negative / excluding evidence
```

## 8.3 Identity

Identity is the guarantee that the same opportunity is not processed
twice, even when it arrives through different sources or under changed
wording.

``` text
Candidate job
      |
      v
Build identity (URL first, source id fallback)
      |
      v
Postgres identity claim
      |
   +--+------------------+
   v                     v
Already claimed       New claim
   |                     |
   v                     v
Duplicate -> Stop     Continue pipeline
```

Identity prefers the canonical URL; the source-specific identifier is
the fallback. Cross-source project claims are stored in the durable JSON
state (`claim_cross_source_project`), so a restart does not lose the
association.

------------------------------------------------------------------------

# 9. Classification Architecture

Classification decides which registered category (if any) a job belongs
to. Loki uses a deliberate two-stage design:

``` text
Normalized job
      |
      v
+----------------------+
| Deterministic stage  |   keyword / weighted rules
| (7 categories)       |
+----------------------+
      |
   +--+---------------------+
   v                        v
Confident                Ambiguous
   |                        |
   v                        v
Decided              LLM arbitration
                            |
                            v
                     Full Stack arbitration
                            |
                            v
                      One final category
```

## 9.1 Category set

Categories live under `app/domain/categories/<id>/profile.py` and are
auto-discovered by `app/domain/categories/registry.py`.

| Category id      | Display name              | Role              |
| ---------------- | ------------------------- | ----------------- |
| `data_analysis`  | Data Analysis             | Deterministic     |
| `ai_ml`          | AI/ML Data Science        | Deterministic     |
| `backend`        | Backend Development       | Deterministic     |
| `frontend`       | Frontend Development      | Deterministic     |
| `mobile_app`     | Mobile App Development    | Deterministic     |
| `game_dev`       | Game Development          | Deterministic     |
| `graphic_design` | Graphic Design            | Deterministic     |
| `full_stack`     | Full Stack Development    | Arbitration-only  |

`full_stack` is **arbitration-only**: it is never selected by the
deterministic stage. It exists so the LLM can resolve a genuine mix of
frontend and backend work, and so the Notification Guard can reclassify a
job into it when the content clearly spans both.

The registry exposes three views:

-   `enabled()` - every registered category.
-   `deterministic()` - categories eligible for the deterministic stage.
-   `arbitration_only()` - categories only the LLM/guard may choose.

## 9.2 Deterministic stage

Each deterministic category profile supplies keyword and weighting rules.
The stage produces either a confident category or an explicit "ambiguous"
outcome that defers to the LLM.

``` text
text -> normalize -> keyword hits -> weighted score
                                       |
                              confident threshold?
                                 |            |
                                yes           no
                                 |            |
                                 v            v
                             category      ambiguous
```

## 9.3 Input bounds

Classification input is bounded before it reaches an LLM:

``` text
MAX_CLASSIFY_TEXT_CHARS  = 32,000
MAX_CLASSIFY_TITLE_CHARS = 2,000
```

Bounding is applied in `app/domain/classification.py` and protects the
provider request size. Wuzzuf descriptions in particular can be very
large.

------------------------------------------------------------------------

# 10. LLM Subsystem

LLM configuration is declarative in `config/project.json`, with
environment overrides. The subsystem supports multiple providers and
rotates across models/keys when a provider is rate-limited, cooling down,
or failing.

``` text
Classification / Guard request
        |
        v
Provider registry (app.llm.registry)
        |
        v
Rotation / deadline control (app.llm.rotation)
        |
   +----+-------------------------------+
   v                                    v
Gemini provider                     Groq provider
(gemini-3.5-flash)                  (gpt-oss-120b,
                                     gpt-oss-20b,
                                     qwen3.8-27b)
```

## 10.1 Providers

``` text
gemini
  adapter: app.llm.gemini:GeminiProvider
  models:  gemini-3.5-flash
  max_output_tokens:             1000
  arbitration_max_output_tokens: 2000

groq
  adapter: app.llm.groq:GroqProvider
  models:  openai/gpt-oss-120b
           openai/gpt-oss-20b
           qwen/qwen3.8-27b
  max_output_tokens:              800
  arbitration_max_output_tokens: 1000
```

`LLM_PROVIDERS` defines the deployment order. Each provider may hold
multiple API keys; the rotation layer selects an available key/model and
respects cooldowns.

## 10.2 Rotation and deadlines

``` text
attempt
  |
  v
pick provider/key/model
  |
  v
within deadline?
  |
 +--+----------------+
 yes                 no
 |                    |
 v                    v
call provider     abandon attempt
 |                    |
 v                    v
success?          next rotation slot
 |     |
 yes   no
 |     |
 v     v
done  cooldown slot -> next
```

Key properties:

-   **Deadline-aware.** Each call is attempted inside an overall
    deadline; the rotation layer does not start a call it cannot finish.
-   **Cooldown on failure.** A rate-limited or failing key/model is
    parked for a cooldown window instead of being retried immediately.
-   **Total-outage behavior.** If every slot is cooling down or the
    deadline is exceeded, the classification is left durably `Pending`
    and the retry sweep picks it up later.

## 10.3 Legacy seam

`app/llm/manager.py` is retained as a compatibility seam for older call
sites. The real orchestration lives in `app.llm.registry` and
`app.llm.rotation` (`run_with_rotation`).

------------------------------------------------------------------------

# 11. Notification Guard

The Notification Guard is Loki's final quality gate. After a job is
classified, the guard decides whether it is genuinely worth notifying
subscribers. This is where "correctly categorized" is separated from
"appropriate to send".

``` text
Classified job
      |
      v
Build bounded guard input
      |
      v
Durable guard decision lookup
      |
   +--+--------------------------+
   v                             v
Prior notify/                No usable decision
do_not_notify                     |
   |                              v
   |                    Guard LLM evaluation
   |                              |
   |                       +------+------+
   |                       v             v
   |                   notify     do_not_notify
   |                       |             |
   +-----------+-----------+-------------+
               v
        Persist durable decision
               |
               v
   (guard may reclassify to full_stack)
```

## 11.1 Package layout

``` text
app/notification_guard/
    config.py       guard tuning, bounds, enabled flag
    guard.py        evaluation engine
    integration.py  pipeline integration point
    registry.py     registered guard providers
    groq.py         Groq guard provider
    prompt.py       guard prompt construction
    provider.py     guard provider interface
    logger.py       guard audit logging
```

Groq is currently the only registered guard provider.

## 11.2 Durable decisions

The guard decision is durable per job:

-   A terminal `notify` or `do_not_notify` decision is **reused forever**;
    a rerun never flips a job from one to the other.
-   A transient `error` outcome is re-evaluated on a later pass.

This makes the guard idempotent and crash-safe: a restart or retry cannot
change an already-made decision.

## 11.3 Fail-closed and reclassification

-   **Fail-closed:** if the guard cannot obtain a trustworthy decision,
    the job is not sent.
-   **Reclassification:** the guard may move a job into `full_stack`
    when its content clearly combines frontend and backend work.
-   Guard input is bounded by `MAX_GUARD_TEXT_CHARS`,
    `MAX_GUARD_TITLE_CHARS`, and `MAX_GUARD_TOTAL_TOKENS` in
    `app/notification_guard/config.py`. This exists because Wuzzuf
    descriptions can reach ~200K characters and an oversized Groq request
    is rejected with HTTP 413.

## 11.4 Enabling the guard

`run_guarded.py` forces `NOTIFICATION_GUARD_ENABLED=true` **before**
importing the application. The ordering is load-bearing: configuration is
read at import time, so the environment must be set first.

``` text
python run.py          # guard controlled by environment
python run_guarded.py  # force-enables the guard before import
```

------------------------------------------------------------------------

# 12. Job Processing Pipeline

`app.services.job_processor.process_job()` is the single entry point used
by every source adapter. It owns the full lifecycle from raw candidate to
durable event.

``` text
process_job()
   |
   v
Validate + normalize
   |
   v
Identity / dedup claim (Postgres)
   |
   +-- Duplicate --> Stop
   |
   v
Create job row (original URL, Notification Status = Pending)
   |
   v
Classify
   |
   +-- Confident --> category
   |
   +-- Ambiguous --> LLM arbitration
   |
   v
Required category? (full_stack / guard reclassification)
   |
   v
Notification Guard decision
   |
   +-- do_not_notify --> Notification Status = Suppressed
   |
   +-- notify
          |
          v
    Shorten URL (idempotent)
          |
          v
    XADD jobs:notify (full job row, stringified)
          |
          v
    mark Notification Status = Complete
```

## 12.1 Publish-before-complete invariant

The single most important pipeline invariant:

> **A job is marked `Complete` only after its event has been written to
> the Redis stream.**

``` text
shorten URL
    |
    v
XADD jobs:notify
    |
 +--+------------------+
 ok                    failure
 |                       |
 v                       v
Notification        Notification Status
Status = Complete   stays "Pending"
                          |
                          v
                    notification_retry
                    sweep republishes
```

If the publish fails, the job is left recoverable and the notification
retry worker re-drives it. Because `XADD` can succeed after a crash
before the status write, the stream is **at-least-once**; Node 2 uses the
**Job UUID** as its idempotency key.

## 12.2 Bounds and leases

-   `CLASSIFICATION_CLAIM_LEASE_SECONDS = 120` - how long a worker may
    hold a classification claim before it is considered abandoned.
-   Job UUID namespace: `6f6e6465-7370-4a6f-6273-7570706f7274` (stable,
    so a replayed job keeps the same `Job UUID`).

------------------------------------------------------------------------

# 13. Users, Categories, and Subscriptions

**This concern has moved out of this repository.** Node 1 no longer owns
users, subscriptions, category preferences, or per-user routing, and it
no longer stores a user/notification schema.

``` text
Node 1 (here)                      Node 2 (separate service)
-------------                      -------------------------
knows: jobs, categories,           knows: users, subscriptions,
       guard decisions,                  category preferences,
       Notification Status               delivery state
                                   exposes: /start /categories /stop
```

The `user_notifications` entry that may still appear in the legacy
`ENABLED_WORKERS` default is vestigial: no worker is registered for it.

Consequences for this node:

-   No user table, no subscription table.
-   No per-user fan-out inside this process.
-   A single job produces a single stream event, regardless of how many
    subscribers Node 2 will eventually notify.

------------------------------------------------------------------------

# 14. Notification Routing and Delivery

This node's delivery responsibility ends at the Redis publish.

``` text
Node 1                                    Node 2
------                                    ------
publish job row  ---- Redis Stream ---->  consume (XREADGROUP)
to jobs:notify                            |
(stream: jobs:notify,                     v
 maxlen ~100_000)                   route by category
                                          |
                                          v
                                    render message
                                          |
                                          v
                                    deliver to subscribers
```

## 14.1 Stream contract

-   **Stream:** `jobs:notify`
-   **Payload:** the full job row, with every field stringified for Redis.
-   **Bounded:** `MAXLEN ~ 100_000` (approximate trimming).
-   **Semantics:** at-least-once. The `Job UUID` is the consumer's
    idempotency key.
-   **Suppressed jobs never enter the stream.** A `do_not_notify`
    decision sets `Notification Status = Suppressed`, which is terminal
    and never published.

## 14.2 Why a stream instead of direct sends

-   **Decoupling.** Delivery outages (Telegram limits, Node 2 restarts)
    can no longer stall ingestion or classification.
-   **Durability.** The event survives a Node 1 crash via the publish
    invariant plus the notification retry sweep.
-   **Clear ownership.** Delivery, users, and subscriptions live in the
    service that understands them.

------------------------------------------------------------------------

# 15. Persistence

Loki uses two persistence layers with different purposes.

``` text
Durable job / guard / audit state        Source progress state
             |                                    |
             v                                    v
          Postgres                      Persistent JSON state
   (SQLAlchemy Core + psycopg)      (Telegram watermarks, FreeHub seen,
   migrations/postgres/*.sql         identity breadcrumbs)
```

## 15.1 Postgres

-   Engine: `postgres:16-alpine` in the compose stack; SQLAlchemy Core
    with `psycopg` (`psycopg[binary]`).
-   Schema: `migrations/postgres/0001_init.sql`.
-   Migration runner: `scripts/migrate_postgres.py`, tracked by a
    `schema_migrations` table.
-   Startup: `initialize_database` applies migrations before workers
    start.
-   Logical tables in `0001_init.sql`:

``` text
categories           category id/name/description/enabled
jobs                 full job row + classification + Notification Status
notification_guard   per-job guard audit (REAL read path:
                     get_latest_guard_decision)
gemini               write-only LLM audit
notifications        write-only notification audit
errors               write-only error audit
```

-   `notification_guard` is an operational read path, not just a log;
    `gemini`, `notifications`, and `errors` are write-only audit tables.
-   Subscriber/notification-delivery tables (`users`,
    `subscription_events`, `user_notifications`) are deliberately **not**
    defined here - that flow belongs to Node 2. There is no
    user/subscription schema in this node.

``` text
process starts
      |
      v
initialize_database
      |
      v
apply pending migrations
      |
      v
record in schema_migrations
      |
      v
workers start
```

## 15.2 JSON state

JSON state holds source-specific progress that does not belong in the job
database:

-   Telegram per-channel watermarks.
-   FreeHub seen projects and pagination continuation.
-   Cross-source identity breadcrumbs.

It is loaded at startup (`state.load()`) and persisted as the sources make
progress, giving bounded, restart-safe source recovery.

------------------------------------------------------------------------

# 16. Concurrency, Leases, and Race Prevention

Loki is a single process with concurrent workers, so correctness depends
on explicit serialization and leases rather than thread safety.

## 16.1 Structured concurrency

``` text
WorkerRegistry
      |
      v
asyncio.TaskGroup
      |
   +--+--+--+--+--+--+--+
   |  |  |  |  |  |  |  |
 worker siblings...
      |
      v
any worker raises -> cancel siblings -> shutdown hooks run once
```

## 16.2 Per-channel serialization

Telegram messages for a channel are processed under a per-channel lock so
live events and recovery cannot interleave and corrupt the watermark.

## 16.3 Leases and claims

``` text
worker picks up job
      |
      v
claim (lease = 120s)
      |
      v
work...
      |
 +----+---------------------+
 done                       lease expires
 |                          |
 v                          v
release claim          another pass may reclaim
                            |
                            v
                     idempotent re-processing
```

-   Classification claims carry a `CLASSIFICATION_CLAIM_LEASE_SECONDS`
    lease (default 120s) so an abandoned claim is eventually retried.
-   Identity claims prevent two ingestion paths from processing the same
    opportunity concurrently.
-   Guard decisions and job status transitions are durable and
    idempotent, so a reclaimed job cannot double-notify subscribers.

------------------------------------------------------------------------

# 17. Failure and Recovery Model

Loki assumes any component can fail and is designed so that failure
leaves work recoverable rather than lost.

``` text
Failure class              Protection
--------------             ------------------------------------------
Source disconnect          Bounded reconnect (Telegram) / next poll
                           (FreeHub)
Missed Telegram messages    Durable watermark recovery, oldest->newest,
                           contiguous-head advance
Scraper subprocess hang     Subprocess timeout (600s), JSON snapshot
                           preserved
Scraper repeatedly fails    3 consecutive failures -> scheduler Dead ->
                           unhealthy
LLM outage / cooldown      Durable Pending classification + retry sweep
Guard transient error      Re-evaluated on a later pass
Redis publish failure      Notification Status stays Pending; retry
                           sweep republishes (publish-before-complete)
Process crash              Postgres state + JSON state + streams rebuild
                           progress; at-least-once via Job UUID
Dead/stale worker          Heartbeat + healthcheck marks container
                           unhealthy
```

## 17.1 The three durability guarantees

1.  **Publish-before-complete.** A job is `Complete` only once its event
    is in the stream.
2.  **Durable guard decisions.** `notify` / `do_not_notify` are terminal
    and never flipped by a rerun.
3.  **Watermark ordering.** Recovery cannot leapfrog unrecovered older
    messages.

## 17.2 Retry sweeps

Two workers close the gaps left by transient failure:

``` text
classification_retry worker
  -> re-drives jobs whose classification is durably "Pending"

notification_retry worker
  -> re-drives jobs not yet Complete/Suppressed
     (e.g. publish failed after classification)
```

Both sweeps are idempotent because the underlying state transitions and
guard decisions are durable.

------------------------------------------------------------------------

# 18. Health, Observability, and Deployment

## 18.1 Heartbeat

A dedicated `heartbeat` worker periodically snapshots the liveness of
every registered worker and writes it to the heartbeat file.

``` text
registered workers
      |
      v
heartbeat_loop (every ~15s)
      |
      v
compute liveness per worker:
   alive / reconnecting / dead
      |
      v
write heartbeat file
      |
      v
healthcheck subprocess reads it
```

-   Default heartbeat cadence: **15 seconds**.
-   A critical worker is considered **stale** after `3 x interval`
    (~45s).
-   Source workers emit their own liveness beats more frequently
    (`_LIVENESS_BEAT_CADENCE_SECONDS`, ~10s).

## 18.2 Healthcheck

`app/infra/healthcheck.py` runs as the container `HEALTHCHECK` (a
subprocess). It verifies:

-   Postgres reachable (`SELECT 1`).
-   Redis reachable (`PING`).
-   JSON state parses.
-   Process table is coherent.
-   Heartbeat snapshot shows no stale critical worker.

``` text
Docker HEALTHCHECK
      |
      +--> Postgres SELECT 1
      +--> Redis PING
      +--> parse JSON state
      +--> inspect process table
      +--> read heartbeat snapshot
      |
      v
healthy / unhealthy
```

A stale or `dead` critical worker (including the scraper scheduler after 3
consecutive failures) makes the container unhealthy.

## 18.3 Deployment

``` text
docker-compose.yml
      |
   +--+---------------------+
   v                        v
postgres:16-alpine        loki container
(internal)                image:
                          mohamedbasem2/loki-freelance-assistant:latest
```

-   **Redis is external**, reached via `REDIS_URL`.
-   `DATABASE_URL` is overridden inside compose to the internal
    `postgres:5432`.
-   `./sessions` (Telethon session) and `./database` (JSON state) are
    bind-mounted for persistence.
-   `scripts/migrate_postgres.py` is explicitly copied into the runtime
    image.
-   The image ships two runtime modes; the default `CMD` (and compose's
    `command`) is the guarded entry point:

``` text
python run_guarded.py  # default: force-enables the Notification Guard
python run.py          # standard: guard controlled by environment
```

## 18.4 Configuration surface

Primary configuration is `config/project.json`, overridable from the
environment (`.env`). Notable groups:

``` text
LLM_PROVIDERS                          provider order
GEMINI_API_KEY / GROQ_API_KEY ...      provider credentials
JOB_SOURCES / FREEHUB_SOURCES          enabled sources
JOB_SOURCE_<ID>_POLL_INTERVAL          per-source cadence
ENABLED_WORKERS                        which workers to run
TARGET_CHANNEL_IDS                     Telegram channels to ingest
NOTIFICATION_GUARD_ENABLED             guard on/off
URL_SHORTENER_FAILURE_MODE             fail_open / fail_closed
DATABASE_URL / REDIS_URL               backing services
```

Current Telegram channels (`TARGET_CHANNEL_IDS`):
`-1001335768304,-1002142292720,-1001994689105`.

------------------------------------------------------------------------

# 19. Testing

Tests live under `tests/` and run through `scripts/test.sh`.

``` text
scripts/test.sh
      |
      v
pytest tests/
```

``` text
tests/
    unit tests for parsing / normalization
    classification and category-registry tests
    LLM rotation / cooldown tests
    notification-guard decision tests
    job-processor pipeline and publish-invariant tests
    infrastructure / config tests
```

## 19.1 CI/CD

GitHub Actions workflows live under `.github/workflows/`:

``` text
ci.yml        push to main + pull_request
              postgres:16-alpine / redis:7-alpine services
              pip install -r requirements.txt
              pytest tests/ -q
              entrypoint probes (run / run_guarded, own process each)
              scripts/ci_smoke_test.py (real composition, no network)

docker.yml    push to main + workflow_dispatch
              test job (same as ci.yml)
              build-and-push -> Docker Hub image
              deploy -> Tailscale SSH -> docker compose up -d --pull always
              waits for container HEALTH = healthy, then prunes old images
```

`scripts/ci_smoke_test.py` builds the real dependency graph, initializes
the database, and checks a category's arbitration prompt without making
network calls.

------------------------------------------------------------------------

# 20. Adding and Maintaining Categories

Categories are added by creating a profile file; the registry discovers it
automatically.

``` text
app/domain/categories/
    registry.py          auto-discovery + enabled/deterministic/
                         arbitration_only views
    data_analysis/profile.py
    ai_ml/profile.py
    backend/profile.py
    frontend/profile.py
    mobile_app/profile.py
    game_dev/profile.py
    graphic_design/profile.py
    full_stack/profile.py    (arbitration-only)
```

Steps to add a deterministic category:

1.  Create `app/domain/categories/<id>/profile.py` following an existing
    profile (id, display name, keyword/weight rules).
2.  Add any downstream assumptions (category id used in the stream
    payload / Node 2 routing) in coordination with Node 2.
3.  Add or extend tests for the deterministic rules.
4.  If the category should be reachable only via LLM arbitration, model
    it like `full_stack` and keep it out of the deterministic set.

``` text
create profile.py
      |
      v
registry auto-discovers it
      |
      v
appears in enabled() / deterministic()
      |
      v
classification / guard can select it
```

------------------------------------------------------------------------

# 21. Design Decisions and Known Boundaries

## 21.1 Key design decisions

-   **Two nodes, one responsibility each.** Ingestion/evaluation and
    user-facing delivery are separate services with separate schemas.
-   **Postgres as the source of truth.** Jobs, guard decisions, and audit
    rows are durable and transactional.
-   **Redis stream as the hand-off.** At-least-once delivery, `Job UUID`
    as the idempotency key, suppressed jobs never published.
-   **Publish-before-complete.** A job is `Complete` only after its event
    is durably streamed.
-   **Durable guard decisions.** `notify` / `do_not_notify` are terminal.
-   **Sources isolated from processing.** Any source produces the common
    job model and reuses one pipeline.
-   **Structured concurrency.** `WorkerRegistry` + `TaskGroup` gives
    predictable startup/shutdown and exactly-once shutdown hooks.
-   **Scraper as a subprocess.** Isolation, timeout kill, and no
    `scrapling` import in the app.

## 21.2 Known boundaries and follow-ups

``` text
-  FreeHub's 10-page bound is bounded backfill, not a completeness
   guarantee under shifting pagination.
-  Telegram recovery caps at 2,000 messages per pass; a CAPPED pass
   is not proof of catch-up.
-  The Redis stream is at-least-once: Node 2 must dedupe on Job UUID.
-  user_notifications remains in legacy ENABLED_WORKERS defaults but is
   not a registered worker.
-  No user/subscription/delivery schema exists in this node by design.
-  LLM model names / caps live in config/project.json and must be kept
   in sync with provider availability.
```

------------------------------------------------------------------------

# Operational Mental Model

``` text
SOURCES
Telegram (event-driven)
FreeHub (HTTP polling)
LinkedIn/Wuzzuf (scraper snapshot)
        |
        v
SHARED PIPELINE (process_job)
        |
        v
DURABLE STATE (Postgres + JSON state)
        |
        v
DURABLE DECISION (classification + guard)
        |
        v
DURABLE EVENT (XADD jobs:notify)
        |
        v
NODE 2 (delivery)
```

# Architecture Summary

Loki is a **single-process, multi-worker `asyncio` application** for
ingestion, classification, and notification guarding, plus a **separate
Node.js service** for user-facing delivery.

-   **Runtime:** one structured-concurrency process (`TaskGroup`),
    independent source workers, retry sweeps, heartbeat, healthcheck.
-   **Sources:** Telegram (event-driven), FreeHub (HTTP, 4 platforms),
    LinkedIn/Wuzzuf (in-image scraper subprocess).
-   **Classification:** 7 deterministic categories plus `full_stack`
    arbitration-only, with bounded LLM arbitration.
-   **Guard:** fail-closed, durable terminal decisions, `full_stack`
    reclassification, bounded input.
-   **Persistence:** Postgres (jobs/guard/audit) + JSON state (source
    progress, identity claims).
-   **Hand-off:** at-least-once Redis stream `jobs:notify`, publish
    before complete, `Job UUID` idempotency.
-   **Recovery:** durable watermarks, retry sweeps, heartbeat/healthcheck,
    scraper 3-strike liveness.

