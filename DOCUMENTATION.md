# Loki Freelance Assistant

> Technical architecture and operations guide for the current
> SQLite/Docker implementation of Loki.

Loki is a resilient freelance-job aggregation and notification system.
It collects opportunities from multiple sources, converts them into a
common job model, deduplicates them, classifies them against registered
categories, arbitrates ambiguous cases when required, and delivers
durable Telegram notifications to subscribed users.

The system is intentionally designed as a **single-process application**
with `asyncio`, SQLite, JSON state, bounded workers, and durable
recovery mechanisms.

------------------------------------------------------------------------

## Table of Contents

1.  [System Overview](#1-system-overview)
2.  [Runtime Architecture](#2-runtime-architecture)
3.  [Data Flow](#3-data-flow)
4.  [Source Ingestion](#4-source-ingestion)
5.  [Telegram Ingestion and
    Recovery](#5-telegram-ingestion-and-recovery)
6.  [FreeHub Ingestion and Recovery](#6-freehub-ingestion-and-recovery)
7.  [LinkedIn and Wuzzuf Scraper](#7-linkedin-and-wuzzuf-scraper)
8.  [Parsing, Normalization, and
    Identity](#8-parsing-normalization-and-identity)
9.  [Classification Architecture](#9-classification-architecture)
10. [LLM Subsystem](#10-llm-subsystem)
11. [Notification Guard](#11-notification-guard)
12. [Job Processing Pipeline](#12-job-processing-pipeline)
13. [Users, Categories, and
    Subscriptions](#13-users-categories-and-subscriptions)
14. [Notification Routing and
    Delivery](#14-notification-routing-and-delivery)
15. [Persistence](#15-persistence)
16. [Concurrency, Leases, and Race
    Prevention](#16-concurrency-leases-and-race-prevention)
17. [Failure and Recovery Model](#17-failure-and-recovery-model)
18. [Health, Observability, and
    Deployment](#18-health-observability-and-deployment)
19. [Testing](#19-testing)
20. [Adding and Maintaining
    Categories](#20-adding-and-maintaining-categories)
21. [Design Decisions and Known
    Boundaries](#21-design-decisions-and-known-boundaries)

------------------------------------------------------------------------

# 1. System Overview

Loki has two major responsibilities:

1.  **Ingest and evaluate freelance opportunities.**
2.  **Route accepted opportunities to the correct Telegram
    destinations.**

Sources are deliberately isolated from downstream processing. A source
only needs to produce a normalized job candidate; the same processing
pipeline can then be reused regardless of where the job originated.

``` text
Telegram source
      │
      ▼
Source ingestion
      │
      ▼
Parse + normalize
      │
      ▼
Stable identity
      │
      ▼
Deduplication / persistence
      │
      ▼
Category classification
      │
   ┌──┴──────────┐
   ▼             ▼
Confident     Ambiguous
   │             │
   ▼             ▼
Notification   LLM arbitration
Guard             │
   │              │
   └──────┬───────┘
          ▼
Notification routing
      │
      ├──────────────────────┐
      ▼                      ▼
Fixed private           Durable user queue
notification                 │
      │                      ▼
      ▼                Telegram Bot API
BOT_CHAT_ID                 │
                             ▼
                        Subscribers

FreeHub
  │
  ▼
Source ingestion
  │
  └──────────────────────────┘

LinkedIn / Wuzzuf
  │
  ▼
Scraper
  │
  └──────────────────────────┘
```

### Core principle

``` text
Sources
   ↓
Common job model
   ↓
One processing pipeline
   ↓
One durable decision
   ↓
Many notification destinations
```

The central application boundary is:

``` text
app.job_processor.process_job()
```

------------------------------------------------------------------------

# 2. Runtime Architecture

Loki runs as one Python process with several long-lived asynchronous
activities.

``` text
Application startup
      │
      ├──────────────► Telegram User Bot
      │
      ├──────────────► Telegram Source Worker ───┐
      │                                          │
      ├──────────────► FreeHub Worker ───────────┤
      │                                          │
      ├──────────────► Scraper Scheduler ────────┤
      │                                          │
      ├──────────────► Notification Retry Worker │
      │                                          │
      └──────────────► User Notification Worker ─┤
                                                 ▼
                                          Shared Job Pipeline
                                                 │
                                      ┌──────────┴──────────┐
                                      ▼                     ▼
                                   SQLite            Persistent State
                                      ▲                     ▲
                                      │                     │
                              Bot / Retry / User      Bot / Sources

Heartbeat / Healthcheck
      │
      ├────────► Telegram worker
      ├────────► FreeHub worker
      ├────────► Scraper scheduler
      ├────────► User notification worker
      ├────────► DB worker
      └────────► State worker
```

## Main runtime responsibilities

  -----------------------------------------------------------------------
  Component                           Responsibility
  ----------------------------------- -----------------------------------
  Telegram source worker              Live Telegram ingestion and
                                      recovery

  FreeHub worker                      Polling and bounded backfill

  Scraper scheduler                   Runs LinkedIn/Wuzzuf scraper and
                                      maintains snapshot freshness

  Notification retry worker           Recovers durable fixed-destination
                                      notifications

  User notification worker            Claims and delivers subscriber
                                      notifications

  Telegram user bot                   Commands, category selection,
                                      subscriptions

  SQLite worker                       Serialized durable database access

  State manager                       Durable source watermarks and
                                      source-specific state

  Healthcheck                         Detects stale/dead workers and
                                      persistence quarantine
  -----------------------------------------------------------------------

The architecture intentionally keeps these responsibilities separate
while allowing them to share the same persistence and processing layers.

------------------------------------------------------------------------

# 3. Data Flow

## 3.1 Complete job lifecycle

``` text
Source
  │
  ▼
Parser / normalizer
  │
  ▼
Job processor
  │
  ▼
SQLite identity / dedup check
  │
  ├────────────── Duplicate ──────────────► Stop
  │
  ▼ New job
Create durable job
  │
  ▼
Classification
  │
  ├────────────── Confident ──────────────┐
  │                                       │
  └────────────── Ambiguous ──► LLM ──────┤
                                          ▼
                                   Notification Guard
                                          │
                                          ▼
                                  Notification routing
                                          │
                                          ▼
                                Durable user queue
                                          │
                                          ▼
                                   Telegram Bot API
                                          │
                              ┌───────────┴───────────┐
                              ▼                       ▼
                           Sent / retry          Deactivate

Duplicate jobs return without entering the downstream pipeline.
```

## 3.2 State ownership

``` text
Job facts
(classification, notification state)
          │
          ▼
        SQLite
          │
    ┌─────┴───────────────┐
    ▼                     ▼
Job recovery       Notification recovery

Telegram watermarks
FreeHub seen state
Source identity state
          │
          ▼
Persistent JSON state
          │
          ▼
Source recovery
```

SQLite is the source of truth for durable job and notification state.
JSON state is used for source-specific progress that does not belong in
the job database.

------------------------------------------------------------------------

# 4. Source Ingestion

Loki currently supports three ingestion families:

``` text
Telegram channels
      │
      ▼
Telegram adapter
      │
      ├─────────────────────┐
      │                     │
      ▼                     │
Common job model ◄──────────┤
      ▲                     │
      │                     │
FreeHub adapter             │
      │                     │
      └─────────────────────┘

LinkedIn / Wuzzuf
      │
      ▼
Scraper + file adapter
      │
      └──────────────► Common job model
                              │
                              ▼
                        process_job()
```

The source boundary isolates transport and source-specific quirks from
classification and notification logic.

### Source characteristics

-   **Telegram:** event-driven live ingestion plus durable startup
    recovery.
-   **FreeHub:** HTTP polling with persisted seen state and bounded
    recovery.
-   **LinkedIn/Wuzzuf:** external scraper process producing a curated
    JSON snapshot.

------------------------------------------------------------------------

# 5. Telegram Ingestion and Recovery

Telegram ingestion uses Telethon as a logged-in Telegram user. The Bot
API is a separate interface used for user interaction and notification
delivery.

## 5.1 Startup flow

``` text
Application starts
      │
      ▼
Load Telegram watermarks
      │
      ▼
Recover missed messages
      │
      ▼
Process recovered messages
oldest ─────────────► newest
      │
      ▼
Establish live listener
      │
      ▼
Process new events
      │
      ▼
Persist progress
```

## 5.2 Live message flow

``` text
Telegram
   │
   ▼
Telegram handler
   │
   ▼
Message processor
   │
   ▼
Job processor
   │
   ├────────► SQLite identity / processing
   │
   └────────► Source state / watermark
                │
                ▼
          Durable progress
```

## 5.3 Recovery

Telegram recovery uses durable per-channel watermarks.

``` text
persisted watermark
       ↓
find messages after watermark
       ↓
recover in chronological order
       ↓
process through normal pipeline
       ↓
advance durable progress
```

The recovery pass is intentionally bounded at **2,000 messages**. This
is a design constraint rather than an accidental pagination limit.

A recovery barrier prevents live processing from advancing source
progress in a way that could bypass older messages still waiting for
recovery.

``` text
Older messages
      │
      ▼
  Recovery
      │
      ├──────────────────┐
      │                  │
      ▼                  ▼
Recovery barrier ◄──── Live events
      │
      ▼
Shared job pipeline
      │
      ▼
Durable watermark
```

The goal is:

> **Never let a newly received event make an older unrecovered message
> permanently unreachable.**

------------------------------------------------------------------------

# 6. FreeHub Ingestion and Recovery

FreeHub is accessed over HTTP by design.

The worker polls the configured endpoint, converts projects into Loki's
common job model, and sends them through the same downstream pipeline as
Telegram jobs.

## 6.1 Polling flow

``` text
Timer
  │
  ▼
FreeHub HTTP request
  │
  ▼
Parse projects
  │
  ▼
Compare persistent seen state
  │
  ▼
New projects?
  │
 ┌┴───────────────┐
No                Yes
 │                  │
 ▼                  ▼
Persist progress   Convert to common job
 / sleep              │
 │                    ▼
 └──────────────► process_job()
                      │
                      ▼
               Persist seen state
                      │
                      ▼
                    Sleep
                      │
                      └────────► Timer
```

## 6.2 Bounded recovery

FreeHub recovery is intentionally capped at **10 pages per pass**.

``` text
poll
 ↓
continue from persisted pagination state
 ↓
fetch page
 ↓
process unseen projects
 ↓
stop when caught up
 ↓
reset continuation state
```

The cap protects the application from unbounded historical crawling.

Because FreeHub uses page/offset-style pagination, newly inserted
projects can shift page boundaries. The 10-page bound therefore provides
bounded backfill, not a mathematical guarantee of historical
completeness.

That is an upstream pagination constraint and an intentional operating
boundary.

------------------------------------------------------------------------

# 7. LinkedIn and Wuzzuf Scraper

LinkedIn and Wuzzuf are handled by a separate scraper process.

``` text
scraper_scheduler
      │
      ▼
Launch scraper subprocess
      │
   ┌──┴──────────────┐
   ▼                 ▼
LinkedIn           Wuzzuf
   │                 │
   └────────┬────────┘
            ▼
     Discover listings
            │
            ▼
      Open detail pages
            │
            ▼
   Extract / curate details
            │
            ▼
      Normalize records
            │
            ▼
      jobs_results.json
            │
            ▼
       FileJobSource
            │
            ▼
   Shared Job Pipeline
```

The scraper is intentionally curated around detail extraction rather
than treating a search-result card as a complete job.

## Scheduler health

A scraper subprocess can fail while its parent loop remains alive. The
scheduler therefore tracks consecutive failed runs.

``` text
Scraper scheduler
      │
      ▼
Run
      │
      ▼
Successful?
  │         │
 Yes        No
  │         │
  ▼         ▼
Alive    Failed run
             │
             ▼
      3 consecutive failures?
          │           │
         No          Yes
          │           │
          ▼           ▼
      Keep retrying   Dead
                         │
                         ▼
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
      │
      ▼
Parse fields
      │
   ┌──┴───────────────┐
   ▼                  ▼
Normalize        Original job content
   │                  │
   ▼                  └──────► Storage
Keyword classification
   │
   ├──────────────► Positive / supporting evidence
   │
   └──────────────► LLM context when required
```

## 8.3 Stable identity

Jobs from different sources must not be confused with one another, while
the same opportunity must not be processed repeatedly.

``` text
Source record
      │
      ▼
Canonical identity inputs
      │
      ▼
Stable job UUID / identity
      │
      ▼
Already known?
   │           │
  Yes          No
   │            │
   ▼            ▼
Reuse durable   Create new job
record
```

Identity and notification deduplication are separate concerns:

``` text
job identity
    ≠
user notification identity
```

A single job can therefore be routed to many users without creating
multiple job records.

------------------------------------------------------------------------

# 9. Classification Architecture

Classification is split into:

1.  **Category definitions**
2.  **Shared deterministic engine**
3.  **LLM arbitration for ambiguity**

``` text
Category registry
      │
      ▼
Category profiles
      │
   ┌──┼───────────────┐
   ▼  ▼               ▼
Keywords  LLM context  Guard context
   │          │             │
   └──────────┼─────────────┘
              ▼
      Shared classification engine
              │
              ▼
          Confident?
          │       │
         Yes      No
          │       │
          ▼       ▼
     Final category   LLM arbitration
                           │
                           ▼
                     Final category
```

## Category abstraction

A category typically owns:

``` text
profile.py
keywords.py
llm_prompt.py
guard_prompt.py
```

The shared infrastructure owns:

``` text
filters
LLM providers
LLM rotation
Guard execution
persistence
routing
delivery
```

This means adding a category does not require cloning core
infrastructure.

## Deterministic classification

The keyword engine uses layered evidence rather than a simple substring
check.

Conceptually:

``` text
Job title + description
          │
          ▼
     Normalization
          │
   ┌──────┼──────────────┐
   ▼      ▼              ▼
Positive  Supporting   Negative /
evidence  evidence     reject evidence
   │      │              │
   └──────┼──────────────┘
          ▼
   Tiered evaluation
          │
          ▼
     Strong enough?
       │       │
      Yes      No
       │       │
       ▼       ▼
Category      Reject /
candidate     ambiguous
```

The exact thresholds and signals are owned by the category profile.

## One final category

A job receives one final category rather than one category per
subscription.

``` text
Job
 ↓
candidate evaluation
 ↓
one final category
 ↓
all users subscribed to that category may receive it
```

------------------------------------------------------------------------

# 10. LLM Subsystem

LLM infrastructure is shared across classification and other LLM-backed
decisions.

``` text
LLM request
    │
    ▼
LLM manager
    │
 ┌──┴───────────┐
 ▼              ▼
Gemini         Groq
 │              │
 ▼              ▼
Key rotation   Model / key rotation
 │              │
 └──────┬───────┘
        ▼
Validated result
        │
        ▼
Application decision
```

## Candidate rotation

The manager tracks provider candidates and cooldowns.

``` text
Need LLM decision
      │
      ▼
Build candidate list
      │
      ▼
Candidate available?
   │            │
  Yes           No
   │             │
   ▼             ▼
Call provider   Try cooled candidate
   │             │
   └──────┬──────┘
          ▼
       Success?
       │      │
      Yes     No / transient
       │          │
       ▼          ▼
Validate      Record cooldown
result            │
       │           └──────► next candidate
       ▼
Return decision
```

The system intentionally tries all configured candidates when necessary.
If every candidate is locally marked as cooling down, Loki can still
attempt them rather than silently dropping a potentially recoverable
job.

That behavior is deliberate.

## Timeouts and retries

Provider SDK/network requests are bounded. The higher-level
classification/Guard operation also has an orchestration deadline.

The important distinction is:

``` text
SDK timeout
    ↓
bounds the actual external request

outer deadline
    ↓
prevents starting additional work after the decision window
```

Because provider calls may run in worker threads, the outer timeout is
an orchestration boundary rather than a mechanism for forcibly killing a
running Python thread.

------------------------------------------------------------------------

# 11. Notification Guard

The Notification Guard is a second decision layer for eligible
deterministic classifications.

``` text
Accepted by deterministic classifier
          │
          ▼
     Guard enabled?
       │       │
      No      Yes
       │       │
       ▼       ▼
   Continue  Guard LLM
       │       │
       │       ▼
       │     Decision
       │      │    │
       │    notify  do_not_notify
       │      │        │
       └──────┘        ▼
          │       Stop notification
          ▼
   Persist / route
```

The Guard is deliberately separate from category classification.

### Guard principles

-   Fail closed when a required Guard decision cannot be safely
    obtained.
-   Bound input size.
-   Validate structured output.
-   Rotate configured provider candidates.
-   Persist the decision.
-   Avoid re-evaluating durable `do_not_notify` decisions unnecessarily.

The Guard does not replace category classification.

------------------------------------------------------------------------

# 12. Job Processing Pipeline

`process_job()` is the central orchestration boundary.

``` text
Job candidate
      │
      ▼
Normalize / canonicalize
      │
      ▼
Identity
      │
      ▼
Known job?
   │          │
Duplicate   New / reprocessable
   │          │
   ▼          ▼
Stop      Persist job
             │
             ▼
Deterministic classification
             │
             ▼
         Ambiguous?
          │       │
         Yes      No
          │        │
          ▼        ▼
     LLM arbitration
          │        │
          └───┬────┘
              ▼
        Selected category
              │
              ▼
      Category assigned?
         │          │
        No         Yes
         │           │
         ▼           ▼
 Persist unresolved  Notification Guard
 / stop                  │
                         ▼
                      Notify?
                    │         │
                   No        Yes
                    │         │
                    ▼         ├────────► Fixed private notification
               Persist        │
               decision       └────────► User routing
                                           │
                                           ▼
                                    user_notifications
```

The key architectural property is that all source-specific jobs converge
here.

------------------------------------------------------------------------

# 13. Users, Categories, and Subscriptions

The user-facing Telegram bot uses the Telegram Bot API.

Users can subscribe to multiple categories.

``` text
Telegram user
      │
      ▼
   User Bot
      │
      ▼
Category selector
      │
      ▼
Persist subscriptions
      │
      ▼
SQLite

Category registry
      │
      └──────────────► Category selector
```

## `/start`

``` text
User
 │
 ▼
/start
 │
 ▼
User Bot
 │
 ├────────► Create / update user
 │
 └────────► Read enabled categories
                │
                ▼
        Inline category selector
                │
                ▼
        User selects categories
                │
                ▼
       Persist subscriptions
                │
                ▼
              SQLite
```

## `/categories`

Reopens the same selector using the currently registered categories.

The user does not need to provide a phone number. Telegram user/chat IDs
are sufficient for subscription identity.

## Subscription model

``` text
users
  │
  ├── user_categories ──> categories
  │
  └── user_notifications
```

A user's category subscriptions determine which jobs are routed to them.

------------------------------------------------------------------------

# 14. Notification Routing and Delivery

Once a job has a final category and is eligible for notification,
routing converts that decision into durable recipient-specific work.

``` text
Final category
      │
      ▼
Find active subscribers
      │
      ▼
Create user_notifications
      │
      ▼
Claim pending rows
      │
      ▼
Bounded concurrent delivery
      │
      ▼
Telegram Bot API
      │
   ┌──┼───────────────────┐
   ▼  ▼                   ▼
Success  RetryAfter     Forbidden
   │       │               │
   ▼       ▼               ▼
 Sent   Rate-limited   Deactivate user
          retry
   │
   └──────────────► Retryable failures
                         │
                         ▼
                    Next attempt
```

## Durable notification state

Conceptually:

``` text
Pending
  │
  ▼
Sending
  │
 ├──────────────► Sent
 │
 ├──────────────► Failed
 │                   │
 │                   ▼
 │             Eligible retry
 │                   │
 │                   └────► Sending
 │
 └──────────────► Rate Limited
                         │
                         ▼
                   Retry time reached
                         │
                         └────► Sending
```

The `(job, user)` relationship is unique so repeated routing cannot
create duplicate user notifications for the same job.

## Fixed private destination

The existing fixed private notification path remains separate from
subscriber routing.

``` text
accepted job
   ├──> BOT_CHAT_ID
   └──> subscribed users
```

This preserves the original private notification behavior while allowing
the user-subscription system to scale delivery across categories.

------------------------------------------------------------------------

# 15. Persistence

## 15.1 SQLite

SQLite stores durable application data.

Major logical areas include:

``` text
USERS
  │
  ├──────────────► USER_CATEGORIES ◄────────────── CATEGORIES
  │
  └──────────────► USER_NOTIFICATIONS ◄────────── JOBS
                                      ▲              │
                                      │              └────► CATEGORIES
                                      │
                              NOTIFICATION_GUARD
                                      │
                                      ▼
                                   JOBS

JOBS
  │
  └──────────────► NOTIFICATIONS
                    (fixed destination)
```

Important logical tables include:

-   `jobs`
-   `gemini`
-   `notifications`
-   `notification_guard`
-   `errors`
-   `users`
-   `categories`
-   `user_categories`
-   `user_notifications`

## 15.2 Serialized database access

The database layer uses a dedicated executor to serialize database
operations.

``` text
Async workers
      │
      ▼
    DB API
      │
      ▼
Single DB executor
      │
      ▼
   SQLite
```

This keeps database concurrency predictable while retaining asynchronous
application behavior.

## 15.3 Persistence quarantine

If a database operation exceeds the configured execution timeout, the
persistence backend is quarantined rather than allowing an unknown stuck
worker to continue operating.

``` text
Healthy
   │
   ▼
DB operation exceeds deadline
   │
   ▼
Timeout
   │
   ▼
Quarantined
   │
   ├────────► Future operations fail closed
   │
   └────────► Process restart
                    │
                    ▼
                 Healthy
```

The same concept applies to the JSON state worker.

------------------------------------------------------------------------

# 16. Concurrency, Leases, and Race Prevention

Loki combines asynchronous workers with durable state.

The important rule is:

> **In-memory concurrency controls improve throughput; durable state
> controls correctness.**

## Job-level correctness

``` text
stable identity
    +
atomic deduplication
    ↓
one durable job
```

## Notification correctness

``` text
unique (job, user)
    +
claim/lease state
    ↓
one durable delivery decision
```

## Bounded delivery

``` text
Pending queue
      │
      ▼
Claim batch
      │
      ▼
Concurrency semaphore
      │
   ┌──┼───────┬─────────┐
   ▼  ▼       ▼         ▼
Send Send    Send      Send
  1    2       3        N
   │  │       │         │
   └──┴───────┴─────────┘
            │
            ▼
      Telegram Bot API
```

Delivery concurrency is bounded to avoid turning a large queue into an
uncontrolled Telegram request burst.

------------------------------------------------------------------------

# 17. Failure and Recovery Model

Loki generally follows four rules:

1.  **Persist progress before relying on it for recovery.**
2.  **Do not mark work complete until the relevant durable operation
    succeeds.**
3.  **Retry transient external failures.**
4.  **Fail closed when correctness cannot be established.**

## Recovery map

``` text
Failure
   │
   ▼
Failure type
   │
   ├── Telegram interruption ─────► Watermark recovery
   ├── FreeHub interruption ──────► Seen-state / bounded backfill
   ├── LLM transient failure ─────► Provider rotation
   ├── Notification failure ──────► Durable retry
   ├── Process crash ─────────────► Restart + durable state recovery
   ├── DB/state timeout ──────────► Quarantine + unhealthy
   └── Scraper repeated failure ──► Dead worker state + unhealthy
```

## Restart recovery

``` text
Process stops / crashes
      │
      ▼
Container restart
      │
   ┌──┼───────────────────────┐
   ▼  ▼                       ▼
Load SQLite   Load JSON state   Reset recoverable
                              notification leases
   │          │                       │
   ▼          ▼                       ▼
Resume jobs  Resume source       Resume notification
/ notifications progress            work
      │          │                       │
      └──────────┴───────────┬───────────┘
                             ▼
                       Workers restart
```

The system is designed so a process restart is a recovery mechanism, not
a reset of application history.

------------------------------------------------------------------------

# 18. Health, Observability, and Deployment

## 18.1 Worker health

Workers publish liveness state through the shared heartbeat mechanism.

``` text
Telegram worker ─────────┐
FreeHub worker ───────────┤
Scraper Scheduler ────────┤
User Notifications ───────┤
DB Worker ────────────────┤
State Worker ─────────────┘
             │
             ▼
         Heartbeat
             │
             ▼
      External healthcheck
             │
             ▼
   Healthy / Degraded / Unhealthy
```

Persistence workers are treated specially: they do not need to have run
before they can be considered healthy, but an explicit quarantined/dead
state fails health.

Scraper-backed sources also require the scraper scheduler to be healthy
because a live file poller alone does not prove that fresh scraper data
exists.

## 18.2 Docker model

``` text
Host
  │
  ▼
Loki container
  │
  ▼
Python application
  │
  ├────────► Telegram
  ├────────► FreeHub
  ├────────► Gemini / Groq
  └────────► Telegram Bot API
  │
  ├────────► Persistent SQLite volume
  └────────► Persistent state / session volume

Docker healthcheck
  │
  └──────────────► Loki container
```

Persistent storage is required for:

-   SQLite
-   Telegram session data
-   JSON state

The deployment intentionally remains **single-instance** because SQLite
and local state are not designed for multiple application replicas
sharing the same files.

------------------------------------------------------------------------

# 19. Testing

The repository contains tests across the major system boundaries.

``` text
Test suite
   │
   ├── Parsing / normalization
   ├── Classification
   ├── LLM / Guard
   ├── Telegram recovery
   ├── FreeHub
   ├── SQLite / migrations
   ├── Notifications
   ├── Subscriptions / routing
   ├── State recovery
   ├── Health / worker liveness
   └── Scraper scheduler
```

Run:

``` bash
pytest tests/ -q
```

The suite emphasizes state transitions and failure behavior rather than
only happy-path unit tests.

Provider-dependent tests may require credentials or controlled mocks.

------------------------------------------------------------------------

# 20. Adding and Maintaining Categories

Categories are implemented as profiles rather than separate pipelines.

A category normally provides:

``` text
app/categories/<category>/
├── profile.py
├── keywords.py
├── llm_prompt.py
└── guard_prompt.py
```

Then it is registered centrally.

``` text
Create category profile
      │
      ▼
Define keywords
      │
      ▼
Define LLM context
      │
      ▼
Define Guard context
      │
      ▼
Register category
      │
      ▼
Add tests
      │
      ▼
Bot selector exposes category
      │
      ▼
Classifier can select category
      │
      ▼
Subscribers can receive category
```

Do not duplicate shared infrastructure for a new category.

Shared infrastructure includes:

-   deterministic classification machinery
-   LLM providers
-   LLM rotation/cooldowns
-   Notification Guard execution
-   SQLite persistence
-   routing
-   Telegram delivery

The category owns domain knowledge; Loki owns the machinery.

------------------------------------------------------------------------

# 21. Design Decisions and Known Boundaries

The following are intentional characteristics of the system.

## Telegram recovery: 2,000 messages

The recovery pass is deliberately bounded at 2,000 messages to prevent
unbounded startup work.

A recovery barrier and durable watermark protect ordering and progress.

## FreeHub recovery: 10 pages

FreeHub backfill is intentionally capped at 10 pages per recovery pass.

The limitation is a practical bound around page-based pagination rather
than an assumption that page numbers are permanent cursors.

## LLM candidate rotation

Loki tries configured LLM candidates rather than abandoning a job
because every candidate is locally marked as cooling down.

This favors **not missing potentially recoverable classification work**
over strict adherence to a local cooldown estimate.

## FreeHub uses HTTP

FreeHub is intentionally accessed through its HTTP interface. The source
boundary isolates this choice from the rest of the application.

## Full Stack is arbitration-only

Full Stack is not a normal classification category.

It exists as an arbitration outcome when a job genuinely spans multiple
technical domains.

The system therefore avoids treating Full Stack as an ordinary keyword
bucket.

## Single-process SQLite architecture

The application intentionally uses:

``` text
one process
one SQLite database
serialized DB access
persistent local state
```

This is appropriate for the current operating scale and greatly
simplifies correctness.

It is not a horizontally scalable distributed architecture.

## Bot API vs Telethon

The two Telegram interfaces have different jobs:

``` text
Telethon
  → source ingestion from channels

Telegram Bot API
  → user commands
  → subscriptions
  → notification delivery
```

They intentionally coexist in the same application.

## Durable state over in-memory state

In-memory locks, semaphores, and cooldowns are performance/concurrency
mechanisms.

Durable SQLite/state records are the source of truth for correctness and
recovery.

------------------------------------------------------------------------

# Operational Mental Model

When debugging Loki, follow the data in this order:

``` text
SOURCE
  ↓
PARSE
  ↓
NORMALIZE
  ↓
IDENTITY
  ↓
DEDUP
  ↓
CLASSIFY
  ↓
ARBITRATE IF NEEDED
  ↓
GUARD IF APPLICABLE
  ↓
ROUTE
  ↓
QUEUE
  ↓
DELIVER
  ↓
PERSIST RESULT
```

If a job appears to be missing, identify the **last stage it
successfully crossed** before investigating anything else.

For a user notification:

``` text
job exists
  ↓
final category exists
  ↓
Guard allows notification
  ↓
user is subscribed
  ↓
user_notification row exists
  ↓
row is claimable
  ↓
Telegram send succeeds
  ↓
row becomes Sent
```

That sequence is the fastest way to localize most operational problems.

------------------------------------------------------------------------

## Architecture Summary

Loki's architecture can be reduced to five layers:

``` text
1. Sources
   Telegram · FreeHub · LinkedIn · Wuzzuf
          │
          ▼
2. Processing
   Parsing · normalization · identity · dedup
          │
          ▼
3. Decisions
   Deterministic classification · LLM arbitration · Guard
          │
          ▼
4. Delivery
   Routing · durable queues · Telegram Bot API

5. Reliability
   SQLite · state · retries · leases · health
   │
   └──── supports ingestion, processing,
         decisions, and delivery
```

The result is a source-independent pipeline where ingestion,
decision-making, and delivery can evolve independently while durable
state preserves correctness across retries, failures, and restarts.
