# Loki Freelance Assistant --- Technical Documentation

> Technical reference for the current SQLite/Docker implementation of
> Loki. This document describes the system as it exists now, including
> the category abstraction and Telegram user-subscription layer.

## Table of Contents

1.  [System Overview](#1-system-overview)
2.  [Installation](#2-installation)
3.  [Configuration](#3-configuration)
4.  [Architecture and Module
    Responsibilities](#4-architecture-and-module-responsibilities)
5.  [Telegram Ingestion](#5-telegram-ingestion)
6.  [FreeHub Ingestion](#6-freehub-ingestion)
7.  [Parsing and Normalization](#7-parsing-and-normalization)
8.  [Category Profiles and
    Classification](#8-category-profiles-and-classification)
9.  [LLM Subsystem](#9-llm-subsystem)
10. [Notification Guard](#10-notification-guard)
11. [User Bot and Subscriptions](#11-user-bot-and-subscriptions)
12. [User Notification Routing and
    Delivery](#12-user-notification-routing-and-delivery)
13. [SQLite Database](#13-sqlite-database)
14. [Persistent State](#14-persistent-state)
15. [Recovery and Failure Semantics](#15-recovery-and-failure-semantics)
16. [Concurrency and Race
    Prevention](#16-concurrency-and-race-prevention)
17. [Docker Deployment](#17-docker-deployment)
18. [Testing](#18-testing)
19. [Troubleshooting](#19-troubleshooting)
20. [Known Limitations](#20-known-limitations)
21. [Maintenance Guidelines](#21-maintenance-guidelines)

# 1. System Overview

Loki is a single Python application built around `asyncio`.

The main runtime starts four long-lived activities:

``` python
await asyncio.gather(
    start(),
    freehub_worker(),
    notification_retry_loop(NOTIFICATION_RETRY_INTERVAL),
    user_notification_worker(),
)
```

They are:

-   Telegram source ingestion and recovery.
-   FreeHub polling.
-   Existing fixed-destination notification retry sweeping.
-   User-subscription notification delivery.

The Telegram user bot is initialized before these workers start. It
handles user commands and callbacks while the source listener continues
to ingest jobs.

## High-level flow

``` text
Telegram source
      │
      ▼
handlers/telegram.py
      │
      ▼
message_processor.py
      │
      ▼
parser.py + normalize.py
      │
      └───────────────┐
                      │
FreeHub              │
  │                   │
  ▼                   │
freehub_worker.py     │
  │                   │
  └─────────┬─────────┘
            ▼
      job_processor.py
            │
            ▼
      identity + dedup
            │
            ▼
     category classification
            │
       ┌────┴─────┐
       ▼          ▼
   confident   ambiguous
       │          │
       ▼          ▼
 final category  LLM review
       │          │
       └────┬─────┘
            ▼
       final category
            │
      ┌─────┴──────────────┐
      ▼                    ▼
private notification    user routing
      │                    │
      ▼                    ▼
 BOT_CHAT_ID         user_notifications
                           │
                           ▼
                  user_notification_worker
                           │
                           ▼
                      Telegram Bot API
                           │
                    ┌──────┴──────┐
                    ▼             ▼
               subscribers   optional DA channel
```

The central boundary remains:

``` text
app.job_processor.process_job()
```

# 2. Installation

## 2.1 Prerequisites

-   Python 3.11+
-   Telegram API ID/hash
-   A Telegram user account for Telethon ingestion
-   A Telegram Bot token from BotFather
-   Gemini API key(s)
-   Groq API key
-   FreeHub user ID/configuration

The Telethon account and the Bot API account serve different roles but
run inside the same Loki application.

## 2.2 Install dependencies

``` bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## 2.3 First Telethon login

Run:

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first Telethon login is interactive. The session is then persisted
and reused.

# 3. Configuration

Configuration is loaded by `app/config.py`. Notification Guard
configuration is loaded separately by
`app/notification_guard/config.py`.

  ---------------------------------------------------------------------------------------
  Variable                                Required                Purpose
  --------------------------------------- ----------------------- -----------------------
  `API_ID`                                Yes                     Telegram application ID

  `API_HASH`                              Yes                     Telegram application
                                                                  hash

  `PHONE_NUMBER`                          Yes                     Telethon user-account
                                                                  phone number

  `BOT_TOKEN`                             Yes                     Telegram Bot API token
                                                                  used for the user bot
                                                                  and user notification
                                                                  delivery

  `BOT_CHAT_ID`                           Yes                     Existing fixed private
                                                                  notification
                                                                  destination

  `BOT_CHANNEL_ID`                        No                      Optional Telegram channel
                                                                  subscriber destination

  `GEMINI_API_KEYS`                       Yes                     Comma-separated Gemini
                                                                  API keys

  `GROQ_API_KEY`                          Yes                     Main Groq fallback key

  `TARGET_CHANNEL_IDS`                    Yes                     Telegram source IDs

  `FREEHUB_USER_ID`                       Yes                     FreeHub user identifier

  `FREEHUB_BASE_URL`                      No                      FreeHub API base URL

  `FREEHUB_POLL_INTERVAL`                 No                      FreeHub polling
                                                                  interval, default `60`

  `FREEHUB_PAGE_SIZE`                     No                      FreeHub page size,
                                                                  default `30`

  `NOTIFICATION_GUARD_ENABLED`            No                      Enables the Guard

  `GROQ_NOTIFICATION_GUARD_API_KEY`       Guard only              Dedicated Guard key

  `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`   No                      Guard retry limit
  ---------------------------------------------------------------------------------------

`BOT_TOKEN` is not a user's phone number and the user-subscription
system does not require users to share their phone numbers. The
application identifies subscribers by their Telegram user/chat ID.

# 4. Architecture and Module Responsibilities

## 4.1 Entry points

### `run.py`

Starts the normal application:

``` text
run.py
  ↓
app.bot.main()
  ↓
app.bot.run()
```

### `run_guarded.py`

Installs the Notification Guard adapter before starting the same
application.

## 4.2 Runtime components

  -----------------------------------------------------------------------
  Module                              Responsibility
  ----------------------------------- -----------------------------------
  `app/bot.py`                        Initializes database/state, starts
                                      the user bot, and starts long-lived
                                      workers

  `app/handlers/telegram.py`          Telethon source client, recovery,
                                      live message handling

  `app/freehub.py`                    FreeHub API access

  `app/freehub_worker.py`             FreeHub polling and conversion into
                                      the shared job pipeline

  `app/parser.py`                     Source-specific and generic job
                                      parsing

  `app/normalize.py`                  English/Arabic text normalization

  `app/classification.py`             Runs the shared category-selection
                                      orchestration

  `app/filters.py`                    Tiered deterministic keyword engine

  `app/categories/`                   Category-specific domain
                                      definitions

  `app/llm/`                          Shared Gemini/Groq provider
                                      infrastructure

  `app/notification_guard/`           Shared Guard infrastructure

  `app/job_processor.py`              Central job-processing
                                      orchestration

  `app/logger.py`                     SQLite persistence and serialized
                                      database access

  `app/routing.py`                    Converts a final category into
                                      durable user-notification queue
                                      records

  `app/user_bot.py`                   User commands, category selection
                                      UI, and concurrent user
                                      notification delivery

  `app/notifier.py`                   Existing fixed private notification
                                      path

  `app/user_bot.py`                   User commands, category subscriptions,
                                      subscriber queue delivery, and optional
                                      channel registration

  `app/state.py`                      Persistent Telegram/FreeHub state

  `app/message_builder.py`            Telegram message formatting
  -----------------------------------------------------------------------

# 5. Telegram Ingestion

The source listener uses Telethon as a logged-in Telegram user.

The listener:

1.  Loads persisted channel watermarks.
2.  Performs startup recovery.
3.  Processes recovered messages in order.
4.  Registers/handles live messages.
5.  Converts messages into the common job pipeline.

The source listener does not handle user subscriptions.

That responsibility belongs to the Telegram Bot API component in
`app/user_bot.py`.

# 6. FreeHub Ingestion

FreeHub is polled independently of Telegram.

`app/freehub_worker.py`:

1.  Polls the configured endpoint.
2.  Tracks seen project IDs in persistent state.
3.  Converts projects to the common job structure.
4.  Sends them through `process_job()`.

Telegram and FreeHub therefore share the same downstream classification,
persistence, routing, and notification logic.

# 7. Parsing and Normalization

`app/parser.py` extracts:

-   title
-   description
-   budget
-   URL
-   source identity

`app/normalize.py` standardizes text before keyword matching.

Normalization includes English/Arabic handling, Arabic character
unification, diacritic removal, punctuation/separator handling, and
lowercasing.

The classifier operates on normalized text while the original job
content remains available for logging and LLM review.

# 8. Category Profiles and Classification

## 8.1 Why profiles exist

Loki separates shared classification machinery from category-specific
knowledge.

The current structure is:

``` text
app/categories/
├── registry.py
└── data_analysis/
    ├── profile.py
    ├── keywords.py
    ├── llm_prompt.py
    └── guard_prompt.py
```

All currently registered categories are active. The current registry includes
Data Analysis, AI/ML Data Science, Backend Development, Frontend Development,
Mobile App Development, and Game Development.

A future category follows the same pattern:

``` text
app/categories/<category>/
├── profile.py
├── keywords.py
├── llm_prompt.py
└── guard_prompt.py
```

The profile is registered in `app/categories/registry.py`.

## 8.2 Shared engine vs category knowledge

The shared engine knows **how to classify**.

The category profile knows **what the category means**.

This prevents separate copies of `filters.py`, Gemini clients, Groq
clients, or Guard logic.

## 8.3 Tiered deterministic engine

The deterministic classifier considers:

-   core positive evidence
-   supporting positive evidence
-   negative evidence
-   hard rejects
-   title signals
-   mixed evidence
-   lone-core protection
-   supporting thresholds
-   downgrade branches
-   explicit decision rules

The current DA thresholds are carried by its profile so future
categories can define their own values.

## 8.4 One final category per job

Users can subscribe to multiple categories.

Jobs cannot.

The classification contract is:

``` text
job
 │
 ├── category A evaluation
 ├── category B evaluation
 ├── category C evaluation
 └── ...
       │
       ▼
 one final category
```

A deterministic category is selected only when the other enabled
categories are not still plausible.

If classification remains ambiguous, the final category stays unset
until the category-arbitration LLM path is implemented.

## 8.5 Current LLM arbitration limitation

The current code deliberately does not call the LLM once for every
category.

The intended future behavior is one shared call:

``` text
ambiguous job
      │
      ▼
one category-arbitration prompt
      │
      ▼
available category definitions
      │
      ▼
Gemini / Groq fallback
      │
      ▼
exactly one category
```

Ambiguous jobs use one shared multi-category arbitration request. The shared
arbitration system policy is assembled from the `SYSTEM_PROMPT` of each
candidate category's `llm_prompt.py`, so category-specific LLM scope rules are
active in production without making one provider request per category.

# 9. LLM Subsystem

The shared provider stack is:

``` text
app/llm/manager.py
        │
   ┌────┴────┐
   ▼         ▼
 Gemini     Groq
   │         │
keys/model  fallback
rotation    rotation
```

Gemini API keys are rotated on transient failures. Groq models are
rotated when the fallback provider is used.

LLM responses are validated through `app/llm/utils.py`.

The category-specific prompt is selected from the active profile.

Job descriptions are untrusted content. Prompt instructions are
separated from job text so content embedded in a freelance post cannot
redefine the classifier's instructions.

# 10. Notification Guard

The Guard is an optional second safety layer for direct deterministic
acceptances.

``` text
direct category acceptance
          │
          ▼
Notification Guard
     │           │
     ▼           ▼
  notify    do_not_notify
```

LLM-reviewed jobs bypass the Guard.

Guard behavior is durable and fail-closed.

The current Data Analysis Guard prompt is stored in the Data Analysis
profile.

# 11. User Bot and Subscriptions

## 11.1 User-facing bot

`app/user_bot.py` uses `python-telegram-bot`.

The same `BOT_TOKEN` is used for:

-   user interaction
-   subscribed-job delivery

No separate Loki application is required.

## 11.2 `/start`

When a user sends `/start`:

1.  Loki creates/updates the user record.
2.  Loki reads enabled category profiles.
3.  Loki renders inline category buttons.
4.  The user can select multiple categories.
5.  The selections are persisted.

## 11.3 `/categories`

`/categories` reopens the selector for an existing user.

The interface dynamically reads from `enabled_categories()`.

Therefore, once another profile is registered, its display name
automatically becomes available to users.

## 11.4 User identity

The system stores Telegram user/chat identifiers.

It does not require or automatically collect a user's phone number.

## 11.5 Subscription persistence

The logical relationship is:

``` text
users
  │
  └── user_categories
          │
          ▼
      categories
```

A user can have many category subscriptions.

# 12. User Notification Routing and Delivery

Routing starts only after a final category exists.

``` text
final_category
      │
      ▼
get active subscribers
      │
      ▼
create user_notifications
      │
      ▼
claim pending notifications
      │
      ▼
concurrent delivery
      │
      ▼
Telegram Bot API
```

## 12.1 Queue semantics

A notification record is associated with:

-   job UUID
-   internal user ID
-   Telegram user ID
-   category ID
-   status
-   attempts
-   last error
-   next-attempt timestamp

The database prevents duplicate `(job, user)` queue records.

## 12.2 Concurrent delivery

`user_notification_worker()` uses bounded concurrency.

Current defaults:

``` text
DELIVERY_CONCURRENCY = 10
BATCH_SIZE = 20
MAX_ATTEMPTS = 5
```

The worker:

1.  Claims pending/eligible notifications.
2.  Marks them as in-flight.
3.  Sends them concurrently up to the configured limit.
4.  Records `Sent` or `Failed`.
5.  Schedules retry after transient errors.

## 12.3 Telegram failures

-   `RetryAfter` schedules a retry after Telegram's requested delay.
-   `Forbidden` deactivates the user because the bot chat is
    unavailable.
-   Other Telegram errors are retried up to the configured limit.
-   Unexpected exceptions are logged and retried when possible.

The worker resets in-flight notifications during startup so a process
crash does not permanently strand them.

# 13. SQLite Database

SQLite is the durable application database.

The existing audit model remains intact while user subscriptions and
category data are added.

## Logical tables

### `jobs`

One durable record per deduplicated source job.

Important category fields include:

-   final category ID
-   category selection method
-   category candidates

### `gemini`

Stores LLM review information.

### `notifications`

Stores the existing fixed-destination notification state.

### `errors`

Stores application errors.

### `notification_guard`

Stores durable Guard decisions.

### `users`

Stores Telegram user identity and active status.

### `categories`

Stores the category catalogue used by the user interface.

The enabled category definitions originate from the application
registry.

### `user_categories`

Stores user subscriptions.

Primary relationship:

``` text
user_id + category_id
```

### `user_notifications`

Stores the durable per-user delivery queue.

A job/user pair is unique so a user receives a matching job at most once
even if routing is revisited.

# 14. Persistent State

`app/state.py` maintains non-job state such as:

-   Telegram channel watermarks
-   FreeHub seen IDs
-   cross-source identity claims

The state file is written atomically and uses the existing
backup/recovery mechanism.

SQLite remains the source of truth for jobs, category information,
subscriptions, and notification delivery state.

# 15. Recovery and Failure Semantics

## Job recovery

Telegram startup recovery uses persisted watermarks to find messages
that arrived while Loki was offline.

FreeHub recovery uses persisted seen IDs and bounded backfill.

## User notification recovery

At startup, user notifications left in a sending state are reset so they
can be retried.

## Fixed notification recovery

The existing notification retry sweep continues to handle the original
private notification state machine and durable subscriber queue.

## LLM failure

The current classification behavior remains fail-closed when required
LLM review cannot be completed.

# 16. Concurrency and Race Prevention

The SQLite logger serializes database operations through the existing
database worker.

The notification system uses durable state rather than in-memory "sent"
flags.

User delivery is concurrently executed through a bounded semaphore.

The `(job, user)` uniqueness constraint prevents duplicate subscriber
delivery records.

The existing job-level identity/deduplication mechanisms remain
independent from user subscriptions.

# 17. Docker Deployment

Docker remains the supported deployment model.

Persistent mounts are required for:

-   Telegram session data
-   SQLite database
-   persistent state

The application runs all runtime components together, including:

-   source ingestion
-   FreeHub polling
-   fixed-destination notification retry
-   user bot
-   user notification delivery

# 18. Testing

Run:

``` bash
pytest tests/ -q
```

The current tests cover:

-   parsing
-   normalization
-   keyword filtering
-   category selection
-   LLM providers and validation
-   SQLite persistence/migrations
-   job identity/deduplication
-   Telegram recovery
-   FreeHub polling/backfill
-   notification state machine
-   Notification Guard
-   routing/subscriptions
-   user notification queue behavior
-   message formatting
-   state-file recovery
-   pipeline behavior

Provider integration tests may require valid API credentials.

# 19. Troubleshooting

## Bot does not show categories

Check:

1.  `BOT_TOKEN` is valid.
2.  The user bot is running.
3.  The category exists in `app/categories/registry.py`.
4.  The category profile is enabled.
5.  SQLite initialization completed successfully.

## User subscribed but receives nothing

Check:

1.  The user exists in `users`.
2.  The subscription exists in `user_categories`.
3.  The job has a final category.
4.  The final category matches the subscription.
5.  A `user_notifications` row was created.
6.  The delivery worker is running.
7.  The Telegram bot can message the user.

## Job has no final category

With the current single-category deployment, inspect the deterministic
classification result and LLM result.

A job with no final category is intentionally not routed to users.

This will be resolved more completely when the single-call
multi-category LLM arbitration prompt is introduced.

# 20. Known Limitations

### Multi-category LLM arbitration

Ambiguous jobs are resolved with one provider request. The arbitration
manager composes its system policy from the candidate categories'
`llm_prompt.py` files, while the shared arbitration prompt remains
responsible for the final JSON contract and candidate-ID validation.

### Registered categories

The current registry contains six active categories: Data Analysis,
AI/ML Data Science, Backend Development, Frontend Development, Mobile App
Development, and Game Development. New categories should provide the same
profile, keyword, LLM prompt, and guard prompt components before registration.

### Private and channel notification roles

The owner's private notification path remains fixed and unchanged: every
accepted job is sent to `BOT_CHAT_ID`. Public category channels are no
longer a separate fixed notification path. When `BOT_CHANNEL_ID` is
configured, the bot verifies that it is an administrator, registers the
channel in the `users` table as a `channel` destination, and subscribes it
to `BOT_CHANNEL_CATEGORY_ID` (default: `data_analysis`). Delivery then uses
the same durable `user_notifications` queue as normal subscribers.

### Classification outage is fail-closed

A complete Gemini/Groq outage during required classification does not
currently create a durable classification retry queue.

# 21. Maintenance Guidelines

When adding a category:

1.  Create its category directory.
2.  Define its keywords and tier configuration.
3.  Define its LLM prompt context.
4.  Define its Guard prompt context.
5.  Define its profile.
6.  Register the profile.
7.  Add category-specific tests.
8.  Verify the user bot displays the new category.
9.  Verify a job can receive that category as its single final category.
10. Verify subscribers receive it through the user notification queue.

Do not create category-specific copies of:

-   `filters.py`
-   Gemini provider code
-   Groq provider code
-   database infrastructure
-   notification delivery infrastructure
-   Telegram source ingestion

Those are shared Loki machinery.

### User source preferences

Users can optionally select which freelance sources they want to receive. Category and source preferences are stored directly on the user record as comma-separated lists; an empty source value means all sources. Configured public channel destinations are not source-filtered.
