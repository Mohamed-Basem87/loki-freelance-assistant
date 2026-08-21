# Loki Freelance Assistant

> A production-oriented freelance job monitoring system that watches
> Telegram and FreeHub for freelance jobs, classifies them through
> reusable category profiles, uses Gemini with Groq fallback for
> borderline decisions, persists an auditable SQLite record, and routes
> accepted jobs to subscribed Telegram users.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

## What Loki Does

Loki continuously monitors freelance-job sources and processes every job
through one shared pipeline.

Current capabilities include:

-   **Telegram ingestion** through a logged-in Telethon user account.
-   **FreeHub polling** through the existing FreeHub worker.
-   **English/Arabic normalization** before classification.
-   **Tiered keyword classification** using reusable category profiles.
-   **Six active category profiles**: Data Analysis, AI/ML Data Science, Backend Development, Frontend Development, Mobile App Development, and Game Development.
-   **Gemini review with Groq fallback** for borderline classification.
-   **Optional Notification Guard** for jobs accepted directly by the
    deterministic classifier.
-   **SQLite persistence** for jobs, decisions, users, subscriptions,
    and notification state.
-   **Telegram user subscriptions** through `/start` and `/categories`.
-   **Durable per-user notification queue** with concurrent delivery
    workers.
-   **Crash recovery and retry behavior** for ingestion, state, and
    notification delivery.
-   **Docker deployment** with persistent state, session, and database
    storage.

The current category system is intentionally incremental: the shared
classification engine is category-agnostic, while each category owns its
domain-specific keywords, LLM prompt, Guard prompt, and profile
definition.

## Architecture

``` text
 Telegram sources                 FreeHub
      │                              │
      ▼                              ▼
 Telethon handler             FreeHub worker
      │                              │
      └──────────────┬───────────────┘
                     ▼
             Shared job pipeline
                     │
                     ▼
            Parse + normalize
                     │
                     ▼
              Job identity/dedup
                     │
                     ▼
          Category classification
                     │
          ┌──────────┴──────────┐
          │                     │
     confident result       ambiguous result
          │                     │
          ▼                     ▼
     final category       LLM review path
          │                     │
          └──────────┬──────────┘
                     ▼
              Final category
                     │
                     ▼
             User subscription
                matching
                     │
                     ▼
          Durable user notification
                 queue
                     │
             ┌───────┼───────┐
             ▼       ▼       ▼
          Worker   Worker   Worker
             │       │       │
             └───────┼───────┘
                     ▼
              Telegram Bot API
                     │
              subscribed users
```

### Two Telegram roles

Loki uses two Telegram interfaces inside the same application:

1.  **Telethon user account** --- reads monitored Telegram channels and
    performs startup recovery.
2.  **Telegram Bot API** --- provides `/start` and `/categories`, stores
    subscriptions, and delivers jobs to users.

They are not separate Loki applications.

## Category Architecture

The category layer separates **domain knowledge** from the shared
processing machinery.

``` text
app/categories/
├── registry.py
└── data_analysis/
    ├── profile.py
    ├── keywords.py
    ├── llm_prompt.py
    └── guard_prompt.py
```

A category profile owns:

-   category ID and display name
-   category description
-   positive/core keyword definitions
-   supporting keyword definitions
-   negative and hard-reject definitions
-   category-specific thresholds/rules
-   arbitration context used by the shared category-arbitration LLM
-   Notification Guard prompt module used for category-aware safety checks

The shared engine remains responsible for executing the tiered rules.

### Adding a category

A new category should provide the same profile components:

``` text
app/categories/web_development/
├── profile.py
├── keywords.py
├── llm_prompt.py
└── guard_prompt.py
```

Then it is registered in `app/categories/registry.py`.

The user bot reads enabled categories from the registry, so a registered
category automatically becomes available for subscription.

## Category Selection Model

A user may subscribe to **multiple categories**.

A job, however, has **one final category**.

The deterministic classifier evaluates the job against the enabled
category profiles. A category is selected only when the deterministic
evidence is sufficient and the other categories are not still plausible.

For example:

``` text
Job
 │
 ├── Data Analysis      → strong direct match
 ├── Web Development    → clear reject
 └── Graphic Design     → clear reject
                           │
                           ▼
                  Final category:
                    Data Analysis
```

The system performs **one category-arbitration LLM call per ambiguous
job**, regardless of how many candidate categories remain. The provider
receives the candidate category definitions and must return exactly one
candidate ID or `none`.

``` text
Deterministic classification
          │
          ▼
 direct / ambiguous candidates
          │
          ▼
 ONE Gemini/Groq arbitration call
          │
          ▼
 one final category (or none)
```

## Notification Guard

The Notification Guard is an optional safety layer for jobs accepted
directly by the deterministic classifier. Its prompt is selected from
the job's final category profile.

``` text
Direct acceptance
      │
      ▼
Notification Guard
   │          │
 notify   do_not_notify
   │
   ▼
delivery
```

LLM-reviewed jobs bypass the Guard because they already received an LLM
review. Subscriber routing uses the same Guard decision as the fixed
private/channel destinations, while remaining independent of channel
send success.

Each category's Guard prompt is isolated in its category directory.
The current Data Analysis Guard prompt is:

``` text
app/categories/data_analysis/guard_prompt.py
```

The Guard implementation remains shared.

It is enabled only when:

``` text
NOTIFICATION_GUARD_ENABLED=true
```

## User Bot and Subscriptions

Users interact with the Telegram bot directly.

### `/start`

The bot:

1.  Registers the Telegram user in SQLite.
2.  Loads the enabled category registry.
3.  Displays the available categories.
4.  Lets the user select multiple categories.
5.  Persists those subscriptions.

### `/categories`

Reopens the same category selector so users can change their
subscriptions later.

The bot stores the Telegram user ID, not the user's phone number.

Subscription data is represented by:

``` text
users
categories
user_categories
```

The category list shown by the bot is derived from the enabled category
profiles, so it is dynamic with respect to registered categories.

## User Notification Delivery

After a job receives a final category:

``` text
final category
      │
      ▼
subscribers to that category
      │
      ▼
user_notifications
      │
      ▼
delivery workers
      │
      ▼
Telegram Bot API
```

Each `(job, user)` pair is queued at most once.

The delivery worker uses bounded concurrency rather than sending
sequentially. Current defaults are:

-   `DELIVERY_CONCURRENCY = 10`
-   `BATCH_SIZE = 20`
-   `MAX_ATTEMPTS = 5`

Notification states are persisted so delivery can resume after a
restart.

Telegram rate limits are delayed and retried. A user whose bot chat
becomes unavailable is deactivated.

## Persistence and Recovery

SQLite is the primary durable store.

It contains the existing audit/job data plus category and user-delivery
data.

Important logical areas include:

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

The `jobs` record stores the final category information, including the
category selection method and candidate information.

Persistent Telegram/FreeHub state remains in:

``` text
database/state.json
```

with the existing atomic-write and recovery behavior.

## Installation

### Requirements

-   Python 3.11+
-   Telegram API credentials for the Telethon user account
-   A Telegram Bot token from BotFather
-   Gemini API key(s)
-   Groq API key
-   FreeHub user ID/configuration

### Local installation

``` bash
git clone https://github.com/Mohamed-Basem87/loki-freelance-assistant.git
cd loki-freelance-assistant

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

### First Telegram login

Run:

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first Telethon login is interactive. The resulting session is
persisted and reused on later starts.

## Configuration

The main configuration is in `.env`.

  ---------------------------------------------------------------------------------------
  Variable                                Required                Purpose
  --------------------------------------- ----------------------- -----------------------
  `API_ID`                                Yes                     Telegram application ID
                                                                  for Telethon

  `API_HASH`                              Yes                     Telegram application
                                                                  hash

  `PHONE_NUMBER`                          Yes                     Telethon user-account
                                                                  phone number

  `BOT_TOKEN`                             Yes                     User-facing Telegram
                                                                  Bot API token and
                                                                  notification sender

  `BOT_CHAT_ID`                           Yes                     Existing fixed private
                                                                  notification
                                                                  destination

  `BOT_CHANNEL_ID`                        No                      Optional Telegram channel
                                                                  subscriber destination

  `GEMINI_API_KEYS`                       Yes                     Comma-separated Gemini
                                                                  API keys

  `GROQ_API_KEY`                          Yes                     Main Groq fallback key

  `TARGET_CHANNEL_IDS`                    Yes                     Telegram source channel
                                                                  IDs

  `FREEHUB_USER_ID`                       Yes                     FreeHub account/user
                                                                  identifier

  `FREEHUB_BASE_URL`                      No                      FreeHub API base URL

  `FREEHUB_POLL_INTERVAL`                 No                      FreeHub polling
                                                                  interval

  `FREEHUB_PAGE_SIZE`                     No                      FreeHub page size

  `NOTIFICATION_GUARD_ENABLED`            No                      Enables the optional
                                                                  Guard

  `GROQ_NOTIFICATION_GUARD_API_KEY`       Guard only              Dedicated Guard Groq
                                                                  key

  `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`   No                      Guard retry limit
  ---------------------------------------------------------------------------------------

## Docker Deployment

Loki can run through the existing Docker Compose deployment.

Persistent mounts are important because Loki stores:

-   Telegram session data
-   SQLite database
-   state data

Do not place the SQLite database on a filesystem where concurrent access
semantics are unsuitable for the application's single-file database
design.

## Testing

Run:

``` bash
pytest tests/ -q
```

The suite covers the major subsystems, including:

-   parser behavior
-   normalization and keyword classification
-   category classification
-   LLM response validation
-   Gemini/Groq provider behavior
-   SQLite persistence and migrations
-   job identity and deduplication
-   notification state/retry behavior
-   Notification Guard
-   user/category routing
-   user notification delivery logic
-   Telegram recovery
-   FreeHub polling/backfill
-   message building and HTML safety
-   state-file recovery and atomicity
-   pipeline behavior

Live LLM/provider tests require valid credentials.

## Project Structure

``` text
app/
├── bot.py
├── classification.py
├── config.py
├── filters.py
├── freehub.py
├── freehub_worker.py
├── handlers/
│   └── telegram.py
├── job_processor.py
├── categories/
│   ├── registry.py
│   └── data_analysis/
│       ├── profile.py
│       ├── keywords.py
│       ├── llm_prompt.py
│       └── guard_prompt.py
├── llm/
│   ├── gemini.py
│   ├── groq.py
│   ├── manager.py
│   ├── prompt.py
│   └── utils.py
├── notification_guard/
├── logger.py
├── message_builder.py
├── message_processor.py
├── normalize.py
├── notifier.py
├── parser.py
├── routing.py
├── state.py
└── user_bot.py

database/
tests/
Dockerfile
docker-compose.yml
.env.example
run.py
run_guarded.py
README.md
DOCUMENTATION.md
```

## Current Known Limitations

### 1. Active categories

The current registry contains six active categories: Data Analysis, AI/ML
Data Science, Backend Development, Frontend Development, Mobile App
Development, and Game Development. New categories should provide the same
profile, keyword, LLM prompt, and guard prompt components before registration.

### 2. Fixed private notifications and subscriber destinations are separate

The owner's fixed private destination remains unchanged and receives every
accepted job. If `BOT_CHANNEL_ID` is configured, that channel is registered
as a subscriber destination and receives only `BOT_CHANNEL_CATEGORY_ID`
(default: `data_analysis`) through the same durable subscriber queue.

### 3. Total LLM outage remains fail-closed

If the configured LLM providers cannot complete a required review, the
current classification path fails closed rather than retrying
classification indefinitely.

## Design Philosophy

Loki favors:

-   deterministic rules before LLM calls
-   explicit category definitions
-   one final category per job
-   durable state over in-memory assumptions
-   bounded concurrency
-   idempotent notification delivery
-   fail-closed behavior when classification or safety checks cannot be
    completed
-   keeping domain-specific knowledge separate from shared
    infrastructure

## License

MIT
