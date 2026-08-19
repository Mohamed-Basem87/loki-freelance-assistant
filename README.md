# Loki Freelance Assistant

> **A production-oriented freelance job monitoring bot that watches
> Telegram channels and FreeHub, classifies jobs against a personalized
> Data Analysis / Business Intelligence profile, escalates ambiguous
> jobs to Gemini with Groq fallback, persists an auditable SQLite
> record, and delivers accepted jobs to Telegram.**

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

## What Loki Does

Loki continuously monitors freelance-job sources so relevant Data
Analysis / Business Intelligence opportunities can be surfaced without
manually watching every source.

It currently supports:

-   **Telegram channels/supergroups** through a logged-in Telethon user
    account.
-   **FreeHub** polling across the `kafiil` and `freelancer` sources.
-   **Bilingual English/Arabic deterministic classification** using a
    tiered keyword decision table.
-   **Gemini review with Groq fallback** for genuinely ambiguous jobs.
-   **Optional Notification Guard** using a second Groq-based safety
    review for jobs accepted directly by the deterministic classifier.
-   **Private Telegram notifications** and an optional public Telegram
    channel.
-   **SQLite audit logging** for jobs, LLM decisions, notification
    attempts, errors, and Notification Guard decisions.
-   **Crash-oriented recovery** through persistent Telegram watermarks,
    FreeHub seen IDs, atomic state-file writes, database deduplication,
    and notification retry state.
-   **Docker deployment** with persistent host-mounted
    session/state/database data.

The project is intentionally conservative: false positives are treated
as more costly than false negatives, and infrastructure/LLM failures are
designed to fail toward rejection or suppression rather than accidental
notification.

------------------------------------------------------------------------

## Architecture

``` text
                 ┌──────────────────────────────┐
                 │        Telegram Sources       │
                 │ Telethon user account         │
                 └──────────────┬───────────────┘
                                │
                                ▼
                       message_processor.py
                                │
                                ▼
                           parser.py
                                │
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
                 │      Shared Pipeline        │
                 │       job_processor.py      │
                 │                             │
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
                 ▼                             ▼
        Deterministic Classifier          SQLite Dedup
      filters.py + keywords.py           create_job_if_absent()
                 │
        ┌────────┼─────────┐
        │        │         │
        ▼        ▼         ▼
     Reject   Direct     Needs LLM
              Accept         │
                 │           ▼
                 │      Gemini → Groq
                 │           │
                 └─────┬─────┘
                       ▼
              Notification State
                       │
             ┌─────────┴─────────┐
             │                   │
             ▼                   ▼
        Private Telegram    Optional Channel
             │                   │
             └─────────┬─────────┘
                       ▼
                 Retry Sweep

FreeHub API
    │
    ▼
freehub.py / freehub_worker.py
    │
    └──────────────► same job_processor.py pipeline

Supporting infrastructure:
SQLite audit DB
database/state.json + backup
Notification Guard
Docker
```

### Core design principle

Both ingestion paths converge on `app.job_processor.process_job()`.

That gives Telegram and FreeHub the same:

-   identity/deduplication model
-   classifier
-   LLM escalation
-   notification workflow
-   retry behavior
-   audit logging

The source-specific code is responsible only for acquiring and
normalizing the job.

------------------------------------------------------------------------

## Classification Flow

The classifier is **not** a single additive score.

It uses:

-   positive core keywords
-   positive supporting keywords
-   negative core keywords
-   negative supporting keywords
-   hard rejects
-   title signals
-   mixed positive/negative detection
-   supporting-positive thresholds
-   supporting-negative downgrade logic
-   explicit decision-table branches

The resulting path is approximately:

``` text
Job
 │
 ▼
Normalize text
 │
 ▼
Hard reject?
 ├── yes ──► Rejected
 └── no
       │
       ▼
 Core positive?
 ├── no ──► enough supporting evidence?
 │             ├── no ──► Rejected
 │             └── yes ─► Gemini/Groq review
 │
 └── yes
       │
       ├── core negative too? ─────► Gemini/Groq review
       ├── heavy negative support? ► Gemini/Groq review
       ├── lone core signal? ─────► Gemini/Groq review
       └── strong clean signal ───► Direct Accept
```

This structure is deliberately explainable. The classifier records the
evidence that led to its decision, including matched keywords and
category/weight information.

------------------------------------------------------------------------

## LLM Review

Borderline jobs are sent through:

``` text
Gemini
  │
  ├── configured key 1
  ├── configured key 2
  ├── ...
  │
  └── all Gemini keys fail
              │
              ▼
            Groq
              │
              ├── model 1
              ├── model 2
              └── model 3
```

Transient failures receive a bounded retry before the next key/model is
attempted.

Responses are parsed as strict JSON and validated before being accepted.

The job description is treated as **untrusted content** in the prompt.
The application separates instructions from job text and explicitly
tells the model not to follow instructions contained inside the job
description.

### Important current limitation

If every configured Gemini key and every Groq model fails during
classification, the current implementation records the job as `Rejected`
with reason `LLM Error`. There is currently no classification retry
queue for those rows.

This is a known reliability issue and should be fixed before treating
the classification layer as fully production-grade.

------------------------------------------------------------------------

## Notification Guard

The Notification Guard is an optional second safety layer for jobs that
the deterministic classifier accepted **directly**.

``` text
Direct deterministic acceptance
            │
            ▼
     Notification Guard
            │
       ┌────┴────┐
       ▼         ▼
    notify   do_not_notify
       │         │
       ▼         ▼
    deliver    suppress
```

Characteristics:

-   Groq-based.
-   Fail-closed.
-   LLM-reviewed jobs bypass it because they already received an LLM
    classification.
-   Guard errors are recorded separately from genuine `do_not_notify`
    decisions.
-   A genuine rejection becomes terminal suppression.
-   A provider/error result remains retryable.
-   The guard caches its decision so private and channel notification
    legs use the same guard result.

The current deployment entrypoint is `run_guarded.py`, but the guard
still requires `NOTIFICATION_GUARD_ENABLED=true`. If the environment
flag is false/unset, the guard is disabled.

------------------------------------------------------------------------

## Persistence and Recovery

### SQLite

The database is the audit and deduplication source of truth.

Tables:

-   `jobs`
-   `gemini`
-   `notifications`
-   `errors`
-   `notification_guard`

`DBLogger` funnels database access through a dedicated single-worker
executor. The job UUID is the primary key, and job creation uses an
atomic check-and-create transaction.

### Telegram state

`database/state.json` stores:

``` text
Telegram:
    channel_id -> last processed message ID

FreeHub:
    _freehub_seen
        source -> seen project IDs
```

State writes use:

``` text
write temporary file
        ↓
os.replace()
        ↓
write backup snapshot
```

If the primary state file is corrupted, Loki attempts to recover from
the backup. If neither is usable, startup fails loudly instead of
silently treating the deployment as a first run.

### Telegram recovery

On startup, Loki walks forward from each channel's persisted watermark,
oldest to newest, up to:

``` text
MAX_RECOVERY_MESSAGES = 2000
```

A first-time channel is seeded from its newest message rather than
backfilling its entire history.

If a recovered message fails processing, recovery stops at that point
and does not advance beyond the failed message.

### FreeHub recovery

FreeHub keeps per-source seen project IDs in persistent state.

Projects are marked seen **only after** `process_job()` completes
successfully. This means a processing failure can be rediscovered by a
later poll.

FreeHub also performs bounded multi-page backfill, currently capped at
10 additional pages.

------------------------------------------------------------------------

## Notification Reliability

Accepted jobs use a durable notification state machine:

``` text
Pending
   │
   ├── private notification
   │       └── Sent / Failed / Suppressed
   │
   └── channel notification
           └── Sent / Failed / Suppressed

                 ↓
          Complete / Suppressed
```

The retry loop periodically finds incomplete jobs and resumes only
unresolved notification legs.

A per-job async lock prevents the live notification path and the retry
sweep from simultaneously sending the same job.

There is one unavoidable distributed-systems edge:

``` text
Telegram send succeeds
        ↓
process crashes
        ↓
DB has not yet recorded "Sent"
```

On restart the unresolved leg may be sent again. SQLite cannot
atomically commit together with Telegram's external API, so the system
provides effectively-once behavior with a narrow at-least-once edge
around the external send.

------------------------------------------------------------------------

## Telegram Startup Recovery

The live `NewMessage` handler is registered before startup recovery
begins.

The startup sequence is:

``` text
connect
  ↓
warm entity cache
  ↓
create/acquire per-channel locks
  ↓
register NewMessage handler
  ↓
recover channel 1
  ↓
recover channel 2
  ↓
...
  ↓
release each channel's recovery lock
  ↓
live operation
```

A live message arriving during recovery is captured by the handler but
waits on the relevant channel lock until recovery has reached the
appropriate point.

------------------------------------------------------------------------

## Installation

### Requirements

-   Python 3.11+
-   Telegram user account for Telethon ingestion
-   Telegram Bot token for notifications
-   One or more Gemini API keys
-   Groq API key
-   FreeHub user ID
-   Optional Notification Guard Groq API key

### Local installation

``` bash
git clone https://github.com/Mohamed-Basem87/loki-freelance-assistant.git
cd loki-freelance-assistant

python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Copy:

``` text
.env.example → .env
```

and configure the required variables.

### First Telegram login

Run:

``` bash
python run.py
```

or:

``` bash
python run_guarded.py
```

The first Telethon login is interactive. Enter the Telegram login code
and 2FA password if required.

The session is then persisted under:

``` text
sessions/
```

Do not commit the session file.

------------------------------------------------------------------------

## Docker Deployment

The repository includes:

-   `Dockerfile`
-   `docker-compose.yml`

The image uses a multi-stage build and runs as a non-root `loki` user.

The default Docker command is:

``` bash
python run_guarded.py
```

`docker-compose.yml` also explicitly uses the guarded entrypoint.

Persistent host data:

``` text
./sessions/              → /app/sessions
./database/              → /app/database
./loki_freelance_bot.db  → /app/loki_freelance_bot.db
```

### Important SQLite bind-mount requirement

Create the database file on the host before the first
`docker compose up`.

Linux/macOS:

``` bash
touch loki_freelance_bot.db
```

Windows PowerShell:

``` powershell
New-Item loki_freelance_bot.db -ItemType File
```

If the host path does not exist, Docker may create a directory there,
which SQLite cannot use as a database file.

### Start

``` bash
docker compose up -d
```

### Logs

``` bash
docker compose logs -f loki
```

------------------------------------------------------------------------

## Configuration

The main environment variables are:

  ---------------------------------------------------------------------------------------
  Variable                                Required                Purpose
  --------------------------------------- ----------------------- -----------------------
  `API_ID`                                Yes                     Telegram API
                                                                  application ID

  `API_HASH`                              Yes                     Telegram API
                                                                  application hash

  `PHONE_NUMBER`                          Yes                     Telethon user account
                                                                  phone

  `BOT_TOKEN`                             Yes                     Telegram Bot token

  `BOT_CHAT_ID`                           Yes                     Private notification
                                                                  destination

  `BOT_CHANNEL_ID`                        No                      Optional public
                                                                  notification channel

  `GEMINI_API_KEYS`                       Yes                     Comma-separated Gemini
                                                                  API keys

  `GROQ_API_KEY`                          Yes                     Main LLM fallback

  `TARGET_CHANNEL_IDS`                    Yes                     Comma-separated
                                                                  Telegram source IDs

  `FREEHUB_USER_ID`                       Yes                     FreeHub user ID

  `FREEHUB_BASE_URL`                      No                      FreeHub API base URL

  `FREEHUB_POLL_INTERVAL`                 No                      Poll interval, default
                                                                  60 seconds

  `FREEHUB_PAGE_SIZE`                     No                      API page size, default
                                                                  30

  `NOTIFICATION_GUARD_ENABLED`            No                      Enables the
                                                                  Notification Guard

  `GROQ_NOTIFICATION_GUARD_API_KEY`       Guard only              Dedicated guard Groq
                                                                  key

  `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`   No                      Guard retry attempts,
                                                                  default 2

  `NOTIFICATION_RETRY_INTERVAL`           No                      Notification retry
                                                                  sweep, default 300
                                                                  seconds
  ---------------------------------------------------------------------------------------

Notification Guard model names are fixed in application code and are not
environment variables.

------------------------------------------------------------------------

## Testing

Run:

``` bash
pytest tests/ -q
```

The suite covers:

-   parser behavior
-   keyword classification
-   LLM response validation
-   Gemini/Groq provider behavior
-   LLM manager fallback
-   SQLite persistence and migrations
-   concurrent job deduplication
-   notification state/retry behavior
-   Notification Guard
-   prompt-injection boundaries
-   message HTML safety
-   Telegram recovery/watermarks
-   FreeHub polling/backfill
-   state-file corruption/atomicity
-   end-to-end pipeline behavior

Some LLM tests perform live provider calls and therefore require valid
credentials.

------------------------------------------------------------------------

## Project Structure

``` text
.
├── app/
│   ├── bot.py
│   ├── config.py
│   ├── filters.py
│   ├── freehub.py
│   ├── freehub_worker.py
│   ├── handlers/
│   │   └── telegram.py
│   ├── job_processor.py
│   ├── keywords.py
│   ├── llm/
│   │   ├── gemini.py
│   │   ├── groq.py
│   │   ├── manager.py
│   │   ├── prompt.py
│   │   └── utils.py
│   ├── logger.py
│   ├── message_builder.py
│   ├── message_processor.py
│   ├── normalize.py
│   ├── notification_guard/
│   ├── notifier.py
│   ├── channel_notifier.py
│   ├── parser.py
│   ├── state.py
│   └── telegram_bot.py
├── tests/
├── database/
├── sessions/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── run.py
├── run_guarded.py
├── README.md
└── DOCUMENTATION.md
```

------------------------------------------------------------------------

## Current Known Issues

These are implementation findings, not aspirational roadmap items.

### 1. Total LLM failure is finalized as `Rejected` --- Medium

When Gemini and Groq are both unavailable, the current code records:

``` text
Final Decision = Rejected
Decision Reason = LLM Error
```

The job then behaves like a completed job and is not automatically
reclassified later.

**Recommended direction:** make classification infrastructure failure a
recoverable state with a retry mechanism.

### 1. Notification lock implementation is structurally fragile --- Low

Per-job notification locks use a `WeakValueDictionary`.

The current call pattern is safe, but the pattern depends on callers
holding the lock object strongly across the critical section.

This is primarily a maintainability/hardening concern, not a currently
demonstrated production failure.

### 1. Observability is limited

There is no dedicated health endpoint, metrics system, alerting
integration, or structured logging layer.

The service can therefore remain alive while a meaningful subsystem is
unhealthy without automatically notifying the operator.

### 1. Notification Guard is a double opt-in

The guard requires both:

``` text
run_guarded.py
+
NOTIFICATION_GUARD_ENABLED=true
```

Using `run_guarded.py` alone does not guarantee that the guard is
active.

------------------------------------------------------------------------

## Security Notes

-   API keys are expected through `.env` / deployment secrets.
-   Telegram session files are sensitive and should be protected like
    account credentials.
-   SQLite queries use parameterized values.
-   No command execution is performed from job content.
-   User/job text is treated as untrusted content when passed to LLMs.
-   Telegram notification content is HTML-escaped before being inserted
    into Telegram messages.
-   The Docker runtime runs as a non-root user.
-   The default FreeHub endpoint in the current configuration is HTTP;
    use HTTPS when the backend supports it.

------------------------------------------------------------------------

## Design Philosophy

Loki intentionally favors:

1.  **Correct identity over convenience.**
2.  **Durable state over in-memory assumptions.**
3.  **Fail-closed behavior for safety decisions.**
4.  **Deterministic filtering before expensive LLM calls.**
5.  **Explicit evidence trails instead of opaque scores.**
6.  **Recovery over silent data loss.**
7.  **A single shared processing pipeline across all sources.**
8.  **Simple, inspectable persistence over unnecessary infrastructure.**

The core reliability model is deliberately stronger than the surrounding
operational layer. The remaining work is primarily around startup event
handling, recoverable LLM classification failures, observability, and
deployment polish.

------------------------------------------------------------------------

## License

MIT

------------------------------------------------------------------------

## Author

**Mohamed Basem**

Faculty of Artificial Intelligence --- Menoufia University

Focused on Data Analytics, Business Intelligence, Python automation, and
AI applications.


## User subscriptions

Loki also exposes the same Telegram bot as a user-facing interface. Users can
send `/start` or `/categories`, select one or more enabled job categories, and
receive matching jobs by Telegram DM.

The user subscription records and notification queue are stored in the same
SQLite database. Delivery uses a bounded asynchronous worker pool so multiple
users can receive the same job concurrently without creating an unbounded
number of Telegram requests.
