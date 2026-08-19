# Loki Freelance Assistant --- Technical Documentation

> Technical reference for the current SQLite/Docker implementation of
> Loki Freelance Assistant.

This document describes the implementation as it exists in the
repository. Where the implementation has a known limitation or
reliability gap, it is documented explicitly rather than presented as a
guarantee.

------------------------------------------------------------------------

## Table of Contents

1.  [System Overview](#1-system-overview)
2.  [Installation](#2-installation)
3.  [Configuration](#3-configuration)
4.  [Architecture and Module
    Responsibilities](#4-architecture-and-module-responsibilities)
5.  [Telegram Ingestion](#5-telegram-ingestion)
6.  [FreeHub Ingestion](#6-freehub-ingestion)
7.  [Parsing and Normalization](#7-parsing-and-normalization)
8.  [Shared Job Processing](#8-shared-job-processing)
9.  [Deterministic Classifier](#9-deterministic-classifier)
10. [LLM Subsystem](#10-llm-subsystem)
11. [Notification Guard](#11-notification-guard)
12. [Notification Delivery](#12-notification-delivery)
13. [SQLite Audit Database](#13-sqlite-audit-database)
14. [Persistent State](#14-persistent-state)
15. [Recovery and Failure Semantics](#15-recovery-and-failure-semantics)
16. [Concurrency and Race
    Prevention](#16-concurrency-and-race-prevention)
17. [Docker Deployment](#17-docker-deployment)
18. [Testing](#18-testing)
19. [Troubleshooting](#19-troubleshooting)
21. [Known Issues and Recommended
    Fixes](#21-known-issues-and-recommended-fixes)
21. [Maintenance Guidelines](#21-maintenance-guidelines)

------------------------------------------------------------------------

# 1. System Overview

Loki is a single-process Python application built around `asyncio`.

Three long-lived coroutines are started by `app.bot.run()`:

``` python
await asyncio.gather(
    start(),
    freehub_worker(),
    notification_retry_loop(NOTIFICATION_RETRY_INTERVAL),
)
```

They are:

-   Telegram ingestion and recovery.
-   FreeHub polling.
-   Notification retry sweeping.

All sources converge on:

``` text
app.job_processor.process_job()
```

That function is the central processing boundary for:

-   deterministic deduplication
-   classification
-   LLM escalation
-   durable job persistence
-   notification state
-   notification delivery
-   retry/resume behavior

## High-level flow

``` text
Telegram
   │
   ▼
message_processor
   │
   ▼
parser
   │
   └─────────────────────┐
                         │
FreeHub                  │
   │                     │
   ▼                     │
freehub_worker           │
   │                     │
   └──────────┬──────────┘
              ▼
       process_job()
              │
              ▼
      deterministic filter
              │
       ┌──────┼───────┐
       ▼      ▼       ▼
    Reject  Direct   LLM
             Accept  Review
               │      │
               │      ▼
               │   Gemini
               │      │
               │      └──► Groq fallback
               │
               └──────┬───────┘
                      ▼
              Notification state
                      │
              ┌───────┴────────┐
              ▼                ▼
       Private Telegram   Optional channel
              │                │
              └───────┬────────┘
                      ▼
               Retry sweep
```

Supporting infrastructure:

``` text
SQLite audit DB
database/state.json + backup
Notification Guard
Docker
```

------------------------------------------------------------------------

# 2. Installation

## 2.1 Prerequisites

-   Python 3.11+
-   A Telegram user account that can access the monitored channels.
-   A Telegram Bot token for outgoing notifications.
-   One or more Gemini API keys.
-   A Groq API key.
-   A FreeHub user ID.
-   Optional: a dedicated Groq API key for the Notification Guard.

Loki uses Telethon as a **user account**, not as the outgoing
notification bot. This allows the ingestion side to access channel
history and receive live updates as configured.

## 2.2 Local setup

``` bash
git clone https://github.com/Mohamed-Basem87/loki-freelance-assistant.git
cd loki-freelance-assistant

python -m venv .venv
```

Linux/macOS:

``` bash
source .venv/bin/activate
```

Windows PowerShell:

``` powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

``` bash
pip install -r requirements.txt
```

Copy the environment template:

``` text
.env.example → .env
```

and fill in the required values.

## 2.3 First Telegram login

Run one of:

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first Telethon login is interactive. Enter the Telegram code and 2FA
password if required.

The resulting session is persisted under the configured session path:

``` text
sessions/telegram*
```

The session file is sensitive and should never be committed to Git.

------------------------------------------------------------------------

# 3. Configuration

Core configuration is loaded by `app/config.py`.

Notification Guard configuration is intentionally loaded separately by
`app/notification_guard/config.py`.

## 3.1 Environment variables

  ------------------------------------------------------------------------------------------------
  Variable                                Required                       Default Purpose
  --------------------------------------- ---------------- --------------------- -----------------
  `API_ID`                                Yes                                --- Telegram
                                                                                 application ID

  `API_HASH`                              Yes                                --- Telegram
                                                                                 application hash

  `PHONE_NUMBER`                          Yes                                --- Telethon user
                                                                                 account phone

  `BOT_TOKEN`                             Yes                                --- Outgoing Telegram
                                                                                 Bot token

  `BOT_CHAT_ID`                           Yes                                --- Private
                                                                                 notification
                                                                                 destination

  `BOT_CHANNEL_ID`                        No                                 --- Optional public
                                                                                 notification
                                                                                 channel

  `GEMINI_API_KEYS`                       Yes                                --- Comma-separated
                                                                                 Gemini API keys

  `GROQ_API_KEY`                          Yes                                --- Main Groq
                                                                                 fallback key

  `TARGET_CHANNEL_IDS`                    Yes                                --- Comma-separated
                                                                                 Telegram source
                                                                                 IDs

  `FREEHUB_USER_ID`                       Yes                                --- FreeHub user
                                                                                 identifier

  `FREEHUB_BASE_URL`                      No                legacy HTTP endpoint FreeHub API base
                                                                                 URL

  `FREEHUB_POLL_INTERVAL`                 No                                `60` FreeHub poll
                                                                                 interval in
                                                                                 seconds

  `FREEHUB_PAGE_SIZE`                     No                                `30` FreeHub API page
                                                                                 size

  `NOTIFICATION_RETRY_INTERVAL`           No                               `300` Notification
                                                                                 retry sweep
                                                                                 interval

  `NOTIFICATION_GUARD_ENABLED`            No                             `false` Enable
                                                                                 Notification
                                                                                 Guard

  `GROQ_NOTIFICATION_GUARD_API_KEY`       Guard only                         --- Guard Groq API
                                                                                 key

  `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`   No                                 `2` Guard retry
                                                                                 attempts
  ------------------------------------------------------------------------------------------------

Notification Guard model names are fixed in code and are intentionally
not environment variables.

## 3.2 Validation

`app/config.py` validates required values during import.

Important helpers include:

-   `_require_env()`
-   `_require_int_env()`
-   `_require_channel_ids()`

Invalid required configuration raises `RuntimeError` during startup
rather than allowing the application to fail later in an unrelated
subsystem.

------------------------------------------------------------------------

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
application:

``` text
run_guarded.py
  ↓
notification_guard.integration.install()
  ↓
app.bot.main()
```

The guard is still disabled unless:

``` text
NOTIFICATION_GUARD_ENABLED=true
```

## 4.2 Application modules

  -----------------------------------------------------------------------
  Module                              Responsibility
  ----------------------------------- -----------------------------------
  `app/bot.py`                        Initializes DB/state and starts all
                                      long-lived workers

  `app/config.py`                     Core environment loading/validation

  `app/handlers/telegram.py`          Telethon client, startup recovery,
                                      live handler

  `app/message_processor.py`          Telegram event → parsed job →
                                      shared pipeline

  `app/parser.py`                     Telegram text/source/URL extraction

  `app/freehub.py`                    FreeHub polling, pagination and
                                      seen-ID discovery

  `app/freehub_worker.py`             FreeHub polling loop and shared
                                      pipeline dispatch

  `app/normalize.py`                  Arabic/English normalization for
                                      classification

  `app/keywords.py`                   Classifier vocabulary and weights

  `app/filters.py`                    Deterministic classifier

  `app/job_processor.py`              Central
                                      dedup/classification/notification
                                      orchestrator

  `app/llm/manager.py`                Gemini → Groq provider routing

  `app/llm/gemini.py`                 Gemini key rotation and retries

  `app/llm/groq.py`                   Groq model rotation and retries

  `app/llm/prompt.py`                 LLM evaluation system prompt

  `app/llm/utils.py`                  Prompt construction and response
                                      validation

  `app/notification_guard/`           Optional second-stage notification
                                      safety check

  `app/message_builder.py`            Telegram HTML message
                                      construction/truncation

  `app/notifier.py`                   Private Telegram notification

  `app/channel_notifier.py`           Optional public Telegram channel
                                      notification

  `app/telegram_bot.py`               Shared outgoing
                                      `python-telegram-bot` instance

  `app/logger.py`                     SQLite persistence and audit
                                      logging

  `app/state.py`                      Persistent Telegram/FreeHub state
  -----------------------------------------------------------------------

## 4.3 Architectural invariants

The most important invariants are:

1.  **Job identity is deterministic.**
2.  **SQLite is the source of truth for job deduplication.**
3.  **FreeHub seen IDs are an optimization layer, not the ultimate dedup
    boundary.**
4.  **Telegram watermarks only advance after successful message
    processing.**
5.  **The live Telegram handler is registered before startup recovery.**
6.  **Same-channel Telegram processing is serialized.**
7.  **Notification retry and live notification paths share a per-job
    lock.**
8.  **Notification state is persisted before and after external
    notification attempts.**
9.  **LLM failures fail toward rejection rather than accidental
    acceptance.**
10. **Notification Guard failures fail closed.**
11. **State-file corruption fails loudly if no valid backup exists.**

------------------------------------------------------------------------

# 5. Telegram Ingestion

`app/handlers/telegram.py` owns the Telethon user client.

## 5.1 Startup

Current startup sequence:

``` text
TelegramClient
    ↓
client.start(phone=PHONE_NUMBER)
    ↓
get_me()
    ↓
_warm_entity_cache()
    ↓
create/acquire per-channel locks
    ↓
register NewMessage handler
    ↓
_recover_channel() for every target channel
    ↓
release each channel's recovery lock
    ↓
run_until_disconnected()
```

The entity warm-up calls `get_dialogs()` and verifies that each target
channel can be resolved.

The live handler is registered before recovery so messages arriving
during startup are captured. Per-channel locks prevent those live events
from overtaking recovery for the same channel.

## 5.2 Live processing

The live handler:

1.  Receives a `NewMessage`.
2.  Gets the chat information for logging.
3.  Acquires the per-channel lock.
4.  Calls `process_message()`.
5.  Advances that channel's watermark only if processing returns
    success.

Same-channel messages therefore cannot advance the watermark out of
order.

Different channels use different locks and can run concurrently.

## 5.3 Identity

Telegram job identity is derived from:

``` text
chat_id + message_id
```

The channel's display title is not part of identity.

Therefore a channel rename does not change the job UUID.

------------------------------------------------------------------------

# 6. FreeHub Ingestion

FreeHub is polled by `app.freehub.py` and `app.freehub_worker.py`.

Current sources:

``` text
kafiil
freelancer
```

## 6.1 Polling

Each poll:

1.  Fetches page 1.
2.  Compares project UIDs with persistent seen state.
3.  If page 1 is entirely unseen, walks additional pages.
4.  Stops when an already-seen project is encountered, a page is empty,
    or the 10-page safety cap is reached.
5.  Returns newly discovered projects oldest-to-newest.

Projects are tagged internally with:

``` text
_poll_source
```

This stable source identifier is used for downstream identity.

That is intentional: the API's displayed `platform` field is treated as
mutable display data, not as an identity component.

## 6.2 Seen-state semantics

A project is **not** marked seen by `poll_once()`.

Instead:

``` text
poll_once()
    ↓
process_job()
    ↓
success
    ↓
mark_project_seen()
```

If processing raises, the project remains unseen and can be
rediscovered.

## 6.3 Backfill cap

The current maximum is 10 additional pages.

This protects the poller from walking indefinitely after a long outage.

However, an outage combined with sufficiently high posting volume can
push a project beyond the available backfill window. This is a known
operational limitation.

------------------------------------------------------------------------

# 7. Parsing and Normalization

## 7.1 Telegram parsing

`app/parser.py` handles:

-   title extraction
-   description extraction
-   budget extraction
-   URL extraction
-   source identification
-   Nafezly-specific message structure
-   generic fallback parsing

URLs can also be extracted from Telegram inline keyboard buttons by
`app.message_processor.py`.

The generic parser deliberately keeps the title out of the classifier's
description input so title keywords are not double-counted.

## 7.2 Normalization

`app/normalize.py` handles the text transformations required by the
classifier.

The Arabic path includes normalization such as:

-   character variants
-   diacritics
-   tatweel
-   punctuation/separator cleanup
-   lowercasing where applicable

The classifier intentionally uses word boundaries. Common Arabic clitic
attachment without a separating space remains a known matching
limitation.

------------------------------------------------------------------------

# 8. Shared Job Processing

`app/job_processor.py` is the central orchestrator.

Conceptually:

``` text
job
 ↓
deterministic identity
 ↓
dedup lookup
 ↓
atomic create if absent
 ↓
keyword filter
 ↓
LLM if necessary
 ↓
persist final decision
 ↓
notification state
 ↓
resume notification legs
```

## 8.1 Job UUID

Identity uses UUIDv5:

``` text
uuid5(fixed_namespace, f"{source}:{job_id}")
```

Examples:

``` text
Telegram:
chat_id:message_id

FreeHub:
poll_source:project_uid
```

The UUID is therefore stable across process restarts.

## 8.2 Atomic creation

`create_job_if_absent()` uses a transaction around the existence check
and insertion.

The `jobs` table also has:

``` text
PRIMARY KEY ("Job UUID")
```

This provides two layers of duplicate protection:

``` text
application transaction
+
database primary key
```

## 8.3 Existing jobs

If a job already exists, the pipeline does not treat it as a new job.

For jobs with incomplete notification state, the notification resume
path can continue the unresolved notification legs.

------------------------------------------------------------------------

# 9. Deterministic Classifier

The classifier is implemented by:

``` text
app/keywords.py
app/normalize.py
app/filters.py
```

## 9.1 Keyword tiers

Positive signals include:

-   core positive
-   supporting positive

Negative signals include:

-   core negative
-   supporting negative

There is also a hard-reject set and a noise-keyword mechanism.

## 9.2 Decision model

The classifier is an ordered decision table rather than a single numeric
score.

Important branches include:

1.  hard reject
2.  title/core positive signals
3.  mixed core positive + negative
4.  strong supporting-negative downgrade
5.  lone-core handling
6.  clean direct acceptance
7.  no-core but sufficient supporting evidence → LLM
8.  insufficient evidence → reject

## 9.3 Why the design exists

The project favors precision over recall.

For example:

``` text
Excel
```

alone does not necessarily mean:

``` text
Data Analysis job
```

because Excel can appear in:

-   data entry
-   transcription
-   spreadsheet population
-   office administration
-   unrelated software projects

The classifier therefore tries to require context around weaker signals.

## 9.4 Evidence trail

Classifier evidence is persisted with the job:

-   matched keywords
-   category
-   tier
-   hit counts
-   weights
-   hard-reject matches
-   final reason

This makes historical decisions explainable without rerunning the
classifier.

## 9.5 Known classifier limitation

The vocabulary is heavily hand-curated and bilingual. It is well suited
to explainability and fast tuning, but there is currently no
statistically representative labeled dataset automatically evaluated in
CI.

The existing regression cases are useful but do not replace a held-out
precision/recall benchmark.

------------------------------------------------------------------------

# 10. LLM Subsystem

The LLM stack consists of:

``` text
app/llm/manager.py
app/llm/gemini.py
app/llm/groq.py
app/llm/prompt.py
app/llm/utils.py
```

## 10.1 Provider routing

The manager tries:

``` text
Gemini
  ↓
all configured Gemini keys
  ↓
Groq fallback
  ↓
all configured Groq models
```

If all providers fail, `evaluate_job()` raises and the current
`process_job()` implementation records:

``` text
Final Decision = Rejected
Decision Reason = LLM Error
```

### Important reliability limitation

This is currently treated as a completed rejection.

There is no classification retry sweep equivalent to the notification
retry sweep.

Therefore a genuine borderline job can be permanently lost to a
temporary simultaneous Gemini/Groq outage.

This should be changed so infrastructure failure is represented as a
recoverable classification state rather than as a final semantic
rejection.

## 10.2 Gemini

Configured keys are tried in order.

Transient errors receive a bounded retry before the next key.

Non-transient failures advance to the next key.

## 10.3 Groq

The main Groq client rotates through a fixed list of models.

The same general transient-retry strategy is used before advancing.

## 10.4 Response validation

`app/llm/utils.py`:

-   strips code fences when necessary
-   parses JSON
-   requires the expected fields
-   validates confidence
-   rejects invalid response types

A boolean confidence value is explicitly rejected even though Python
considers `bool` an `int` subclass.

## 10.5 Prompt-injection boundary

Job descriptions are external/untrusted content.

The prompt construction explicitly separates:

``` text
instructions
```

from:

``` text
<JobDescription>
untrusted job text
</JobDescription>
```

The Gemini integration also uses the provider's dedicated
system-instruction mechanism rather than concatenating all instructions
into the same user content.

This is a mitigation, not a mathematical guarantee against arbitrary LLM
prompt injection.

------------------------------------------------------------------------

# 11. Notification Guard

The guard lives under:

``` text
app/notification_guard/
```

It is installed by `run_guarded.py`.

## 11.1 Purpose

The deterministic classifier may directly accept a job without LLM
review.

The guard adds:

``` text
classifier direct accept
        ↓
independent Groq safety check
        ↓
notify / do_not_notify
```

LLM-reviewed jobs bypass the guard.

## 11.2 Fail-closed

The guard's only successful allow path is an explicit valid:

``` text
notify
```

Any exception, invalid response, or provider failure produces:

``` text
allowed = False
```

That prevents guard outages from accidentally increasing notifications.

## 11.3 Error vs rejection

The guard distinguishes:

``` text
notify
do_not_notify
error
```

This matters because:

-   `do_not_notify` is a terminal suppression.
-   `error` remains retryable.

Without that distinction, the retry sweep could either hammer a
permanently rejected job forever or permanently lose a job during a
transient provider outage.

## 11.4 Two-leg decision reuse

The guard caches a decision so the private and channel notification legs
for one job share one guard evaluation.

The cache is in-memory and therefore resets after a process restart.

## 11.5 Double opt-in

The guard requires both:

``` text
run_guarded.py
```

and:

``` text
NOTIFICATION_GUARD_ENABLED=true
```

This is an operational risk because using the guarded entrypoint does
not itself guarantee that the guard is enabled.

------------------------------------------------------------------------

# 12. Notification Delivery

Notifications are implemented by:

``` text
app/notifier.py
app/channel_notifier.py
app/message_builder.py
```

## 12.1 Destinations

Private notification:

``` text
BOT_CHAT_ID
```

Optional channel:

``` text
BOT_CHANNEL_ID
```

If no channel ID is configured, the channel leg is treated as
successfully skipped.

## 12.2 HTML safety

User-controlled values are escaped before being inserted into Telegram
HTML.

The builder also has a tag-aware truncation path to avoid malformed HTML
when messages approach Telegram's length limit.

## 12.3 Durable state

Before sending:

``` text
Notification Status = Pending
```

After each leg:

``` text
Telegram: Sent
Telegram: Failed
Telegram Channel: Sent
Telegram Channel: Failed
```

Guard suppression is represented separately.

## 12.4 Retry sweep

`notification_retry_loop()` periodically queries incomplete jobs and
resumes only unresolved legs.

The retry sweep and live notification path share a per-job async lock.

That prevents:

``` text
live path → send
retry path → send
```

from racing each other for the same job.

## 12.5 External side-effect window

SQLite cannot atomically commit with Telegram.

Therefore:

``` text
Telegram send succeeds
        ↓
process crashes before DB update
        ↓
DB still says unresolved
        ↓
retry
```

A duplicate notification is possible in this narrow window.

This is an unavoidable external-side-effect boundary rather than a
SQLite transaction bug.

------------------------------------------------------------------------

# 13. SQLite Audit Database

The database is:

``` text
loki_freelance_bot.db
```

## 13.1 Tables

### `jobs`

One durable row per job.

Contains identity, source data, classifier evidence, LLM decision
information, final decision, notification state, and timing information.

### `gemini`

Records LLM review calls.

Despite the historical table name, this table represents the
application's LLM review audit data, including the Groq fallback path.

### `notifications`

Records notification attempts per destination.

### `errors`

Records caught exceptions and subsystem labels.

### `notification_guard`

Records Notification Guard evaluations.

## 13.2 Thread architecture

`DBLogger` uses:

``` python
ThreadPoolExecutor(max_workers=1)
```

All database access from application subsystems is routed through:

``` text
await logger.run(...)
```

This keeps SQLite operations serialized and moves blocking DB work off
the asyncio event loop.

## 13.3 Transactions

Explicit transactions are used where multiple statements must be atomic,
particularly:

-   job check-and-create
-   schema migration/rebuild
-   legacy-row merging
-   orphaned migration recovery

Single-statement operations use SQLite autocommit.

## 13.4 Migration

The database includes legacy-schema migration logic.

Migration handling accounts for:

-   legacy column layouts
-   case-insensitive SQLite table-name behavior
-   temporary migration tables
-   orphaned migration recovery
-   repeated initialization without duplicating merged rows

## 13.5 Schema limitations

The database is intentionally simple and inspectable, but there are some
schema-quality limitations:

-   several numeric/boolean values are stored as `TEXT`
-   append-only audit tables do not have explicit application-level
    primary keys
-   there is no retention/pruning policy
-   there is no independent application-level DB backup mechanism

At the project's current scale these are manageable, but they matter if
the database grows significantly.

------------------------------------------------------------------------

# 14. Persistent State

`app/state.py` stores state in:

``` text
database/state.json
```

## 14.1 Telegram state

Top-level keys map channel IDs to last processed message IDs:

``` json
{
    "-1001234567890": 12345
}
```

## 14.2 FreeHub state

FreeHub state lives under:

``` text
_freehub_seen
```

with per-source seen UID lists.

## 14.3 Atomic persistence

Writes use:

``` text
state.tmp.json
      ↓
os.replace()
      ↓
state.json
```

A backup is also maintained:

``` text
state.bak.json
```

## 14.4 Corruption behavior

If the primary file is corrupt:

``` text
try primary
    ↓
fail
    ↓
try backup
```

If both fail, Loki raises `StateCorruptionError` and refuses to start
with a silently reset state.

This is intentional. Silently resetting the state would make the
deployment indistinguishable from a first run and could create dangerous
recovery behavior.

## 14.5 Async persistence

State mutations from asynchronous subsystems are serialized through a
dedicated single-worker executor.

This prevents concurrent Telegram/FreeHub writes from racing the same
state file and avoids blocking the event loop with filesystem writes.

------------------------------------------------------------------------

# 15. Recovery and Failure Semantics

## 15.1 Telegram startup recovery

For each configured channel:

``` text
last watermark
     ↓
iter_messages(min_id=last_id, reverse=True)
     ↓
oldest → newest
     ↓
process each message
     ↓
advance watermark after success
```

The current cap is:

``` text
2000 messages
```

If the cap is reached, Loki warns that additional history may remain
unrecovered.

A channel remains recovery-blocked while its startup recovery is active.
Live events captured during that period wait on the same channel lock,
preventing a newer live message from advancing the watermark past an
unresolved recovery point.

## 15.2 First-time Telegram channel

If the watermark is zero:

``` text
get newest message
    ↓
seed watermark
```

It does not backfill the entire historical channel.

This avoids treating an entire channel history as "downtime."

## 15.3 Failed Telegram recovery

If a message fails:

``` text
stop recovery
do not advance beyond failed message
```

This preserves retryability on the next restart.

## 15.4 FreeHub failure

If `process_job()` raises for a project:

``` text
do not mark seen
```

The project can therefore be rediscovered on a future poll.

## 15.5 Notification failure

A failed notification leg is persisted as unresolved/failed and is
picked up by the retry sweep.

## 15.6 LLM failure

Current behavior is different:

``` text
all LLM providers fail
        ↓
Rejected / LLM Error
        ↓
job considered handled
```

This is the major recovery asymmetry in the current system and should be
corrected.

------------------------------------------------------------------------

# 16. Concurrency and Race Prevention

## 16.1 Same-channel Telegram messages

Telethon can dispatch events independently.

Loki uses:

``` python
channel_locks[event.chat_id]
```

to serialize processing for each channel.

This prevents:

``` text
message N+1 completes
      ↓
watermark = N+1
      ↓
message N fails
```

which would otherwise permanently move the recovery point beyond a
failed earlier message.

## 16.2 Different Telegram channels

Different channels use different lock keys.

Therefore:

``` text
channel A → independent
channel B → independent
```

They can process concurrently.

## 16.3 Concurrent duplicate jobs

`create_job_if_absent()` provides the database transaction boundary.

Even if two async paths process the same job concurrently:

``` text
worker A ──┐
           ├── atomic DB create
worker B ──┘
```

only one job row wins.

The UUID primary key provides a second backstop.

## 16.4 Notification retry vs live path

Both paths use the same per-job notification lock.

The retry path also re-reads the durable job row after acquiring the
lock, so it does not blindly act on a stale snapshot.

## 16.5 SQLite

All DB work is serialized through the dedicated logger executor.

## 16.6 Notification lock caveat

The current per-job lock registry uses a `WeakValueDictionary`.

The current usage pattern is safe because the returned lock remains
strongly referenced through the `async with` critical section.

However, this creates a future-maintenance invariant: a refactor that
allows the lock object to disappear before acquisition could silently
create a second lock for the same job.

This is currently a hardening concern rather than a demonstrated race.

------------------------------------------------------------------------

# 17. Docker Deployment

## 17.1 Dockerfile

The Dockerfile uses:

``` text
python:3.11-slim builder
        ↓
pip install into /install
        ↓
python:3.11-slim runtime
```

The runtime:

-   runs as user `loki`
-   does not require root privileges
-   copies only installed dependencies from the builder
-   persists runtime data through mounts

The default command is:

``` text
python run_guarded.py
```

## 17.2 Compose mounts

`docker-compose.yml` mounts:

``` text
./sessions              → /app/sessions
./database              → /app/database
./loki_freelance_bot.db → /app/loki_freelance_bot.db
```

The database file must exist on the host before the first Compose start.

Linux/macOS:

``` bash
touch loki_freelance_bot.db
```

Windows PowerShell:

``` powershell
New-Item loki_freelance_bot.db -ItemType File
```

## 17.3 Restart behavior

Compose uses:

``` yaml
restart: unless-stopped
```

A process crash therefore causes Docker to restart the container.

The application-level recovery mechanisms then restore:

-   Telegram processing position
-   FreeHub seen state
-   notification progress

## 17.4 Current operational gap

There is no Docker `HEALTHCHECK`.

A hung process or silently disconnected external subsystem can therefore
remain alive without Docker automatically replacing it.

------------------------------------------------------------------------

# 18. Testing

Current test modules include:

``` text
test_freehub.py
test_freehub_worker.py
test_job_processor.py
test_keyword_filter.py
test_llm_gemini.py
test_llm_groq.py
test_llm_manager.py
test_llm_utils.py
test_logger.py
test_message_builder.py
test_message_processor.py
test_notification_guard.py
test_notification_guard_prompt.py
test_parser.py
test_pipeline.py
test_state.py
test_telegram_recovery.py
```

## 19.1 Strongly covered areas

The suite directly exercises:

-   UUID stability
-   legacy UUID recognition
-   concurrent duplicate processing
-   SQLite migration behavior
-   orphaned migration recovery
-   repeated initialization
-   state corruption recovery
-   atomic state writes
-   concurrent state writes
-   Telegram recovery
-   per-channel live serialization
-   cross-channel concurrency
-   FreeHub backfill
-   FreeHub seen-state behavior
-   notification retry behavior
-   live-vs-retry notification race prevention
-   message HTML/truncation safety
-   Notification Guard decisions
-   prompt-injection boundaries
-   LLM response validation

## 19.2 Live provider tests

Some LLM tests make real provider calls.

That provides useful integration confidence but also means those tests
can be:

-   credential dependent
-   network dependent
-   rate-limit dependent
-   more expensive
-   less deterministic than mocked unit tests

## 19.3 Testing gaps

The most important missing tests are:

1.  Telegram live-handler registration during startup recovery.
2.  End-to-end event buffering/queuing once that bug is fixed.
3.  Automatic classification retry after total LLM outage.
4.  Notification Guard exactly-once evaluation across both notification
    legs.
5.  A labeled classifier dataset with precision/recall regression
    metrics.
6.  True process-crash simulations around external notification side
    effects.
7.  Application-level health/liveness behavior.

------------------------------------------------------------------------

# 18. Troubleshooting

## Missing environment variables

Startup `RuntimeError` usually means a required variable is absent or
malformed.

Check:

``` text
.env
.env.example
app/config.py
```

## Telegram session/login problems

Run the selected entrypoint manually.

The Telethon user session is persistent and sensitive.

If the session has been revoked, re-authentication is required.

## LLM failures

Check:

``` text
errors
gemini
```

in the SQLite database.

A complete provider outage currently results in:

``` text
Rejected / LLM Error
```

and requires manual intervention because classification retry is not yet
implemented.

## Notification Guard blocks everything

Check:

``` text
notification_guard
```

for:

``` text
do_not_notify
error
```

Also verify:

``` text
NOTIFICATION_GUARD_ENABLED=true
GROQ_NOTIFICATION_GUARD_API_KEY=...
```

## Notification failures

Check:

``` text
notifications
errors
```

and verify:

-   `BOT_TOKEN`
-   `BOT_CHAT_ID`
-   optional `BOT_CHANNEL_ID`
-   bot permissions
-   recipient accessibility
-   network/provider status

## SQLite bind mount becomes a directory

Create the host database file before starting Compose:

``` bash
touch loki_freelance_bot.db
```

## FreeHub backfill warning

If the 10-page cap is reached repeatedly, investigate:

-   host downtime
-   polling failures
-   API availability
-   unusually high source volume

Do not blindly increase the cap without understanding the backlog
behavior.

------------------------------------------------------------------------

# 18. Known Issues and Recommended Fixes

These are the current implementation findings that should be treated as
engineering work, not ignored as documentation details.

## 21.1 Telegram startup recovery ordering — fixed

The Telegram live handler is registered before startup recovery, and
per-channel `asyncio.Lock` instances are acquired before recovery starts.

A live event for a channel that is still being recovered is therefore
captured by the handler immediately but waits on that channel's lock.
Recovery processes the channel's backlog first, then releases the lock,
allowing the queued live event to continue through the normal pipeline.

Conceptually:

``` text
startup
  ↓
register live handler
  ↓
acquire per-channel recovery locks
  ↓
recover channel
  ↓
release channel lock
  ↓
queued live events continue
```

This preserves:

-   startup recovery ordering
-   real-time capture during recovery
-   same-channel message ordering

No separate readiness queue is required because the per-channel lock
provides the required serialization boundary.

## 21.2 Total LLM failure is treated as semantic rejection

### Current behavior

``` text
Gemini exhausted
       ↓
Groq exhausted
       ↓
LLM Error
       ↓
Rejected
```

### Problem

An infrastructure outage is not the same thing as a classifier
conclusion.

### Recommended fix

Introduce a recoverable classification state, for example:

``` text
Pending LLM
LLM Retry
Classified
```

Store retry metadata such as:

``` text
attempt count
last attempt
next retry time
```

and run a bounded retry loop.

Only successful LLM evaluation should produce a semantic `Accepted` or
`Rejected` decision.

## 21.3 Notification lock registry hardening

The current `WeakValueDictionary` implementation works under the current
call discipline.

Possible hardening:

-   use a normal dictionary with explicit lifecycle management, or
-   retain the weak registry but document and test the lock-lifetime
    invariant more explicitly.

This is lower priority than the two issues above.

## 21.4 Observability

Recommended additions:

-   structured logs
-   startup configuration summary without secrets
-   last successful Telegram event timestamp
-   last successful FreeHub poll timestamp
-   last successful LLM review
-   count of unresolved notification jobs
-   count of `LLM Error` jobs
-   Docker healthcheck
-   optional alerting

## 21.5 Notification Guard configuration visibility

At startup, explicitly print:

``` text
Notification Guard: ENABLED
```

or:

``` text
Notification Guard: DISABLED
```

This would make the current two-step activation model much harder to
misunderstand.

## 21.6 Classifier regression dataset

The keyword engine is valuable because it is deterministic and
explainable.

The next maturity step is to maintain a labeled dataset:

``` text
job text
expected decision
```

and calculate:

``` text
precision
recall
false-positive rate
false-negative rate
```

in CI whenever classifier vocabulary changes.

------------------------------------------------------------------------

# 18. Maintenance Guidelines

## 22.1 Do not bypass the shared pipeline

New ingestion sources should converge on:

``` python
process_job(...)
```

rather than duplicating classification or notification logic.

## 22.2 Preserve deterministic identity

New sources need a stable identity tuple.

Good:

``` text
source + immutable external UID
```

Bad:

``` text
title + timestamp + description
```

## 22.3 Preserve durable ordering

If a new asynchronous source maintains a watermark:

``` text
process
  ↓
success
  ↓
advance watermark
```

Never advance a checkpoint before the work it represents has succeeded.

## 22.4 Preserve fail-closed safety

For safety decisions:

``` text
uncertain / provider error
        ↓
do not notify
```

unless the business logic explicitly establishes a different policy.

## 22.5 Keep database access serialized

Application code should continue using:

``` python
await logger.run(...)
```

rather than touching the SQLite connection directly.

## 22.6 Keep state writes serialized

Async callers should use the state manager's async persistence wrappers
rather than performing filesystem writes directly from multiple
coroutines.

## 22.7 Test invariants, not only implementation details

The most valuable tests in this repository are tests such as:

-   concurrent duplicate processing
-   same-channel ordering
-   cross-channel independence
-   recovery after corruption
-   migration recovery
-   retry-vs-live notification races

New tests should prefer these observable invariants over merely checking
private implementation details.

------------------------------------------------------------------------

## Final Engineering Summary

The current implementation has a strong reliability-oriented core:

-   deterministic identity
-   atomic SQLite deduplication
-   persistent recovery state
-   atomic state-file writes
-   transactional migration
-   per-channel Telegram serialization
-   per-job notification serialization
-   notification retry
-   LLM provider fallback
-   fail-closed Notification Guard
-   structured classifier evidence
-   HTML-safe notification construction

The main remaining correctness issue is:

1.  **Permanent rejection of jobs when every LLM provider is temporarily
    unavailable.**

The Telegram startup recovery ordering issue has been fixed in the
implementation and is documented as such in section 21.1.

The largest non-correctness gap is observability.

Those issues should be addressed before describing Loki as fully
production-grade. The underlying architecture, however, is fundamentally
sound and substantially more robust than a simple polling/notification
bot.
