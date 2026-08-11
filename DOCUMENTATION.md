# Loki Freelance Assistant — Technical Documentation

**A bot that monitors freelance job sources in real time (Telegram
channels and FreeHub), classifies each post against a personalized
Data Analysis / Business Intelligence skill profile using a
deterministic bilingual keyword engine, escalates borderline posts to
Google Gemini (with a Groq fallback), logs every decision to an Excel
workbook, and notifies you on Telegram.**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [Production Architecture](#4-production-architecture)
5. [Telegram Ingestion](#5-telegram-ingestion)
6. [FreeHub Ingestion](#6-freehub-ingestion)
7. [Parsing](#7-parsing)
8. [Normalization](#8-normalization)
9. [Job Processing](#9-job-processing)
10. [Deterministic Classifier](#10-deterministic-classifier)
11. [LLM Subsystem](#11-llm-subsystem)
12. [Notification Guard](#12-notification-guard)
13. [Notifications](#13-notifications)
14. [Excel Logging](#14-excel-logging)
15. [State](#15-state)
16. [Recovery](#16-recovery)
17. [Deployment](#17-deployment)
18. [Troubleshooting](#18-troubleshooting)
19. [Testing / Development](#19-testing--development)

---

## 1. Overview

Loki ingests freelance job postings from two independent sources —
Telegram channels (via a logged-in Telethon **user** account) and the
FreeHub API (via periodic polling) — and funnels both through the same
processing pipeline. After parsing, the deterministic keyword
classifier either rejects the job, accepts it directly, or routes it
to Gemini/Groq review. Direct classifier acceptances pass through the
Notification Guard before notification; LLM-reviewed jobs bypass the
Notification Guard. Accepted jobs are then sent to the configured
Telegram notification destinations and logged to Excel.

**Pipeline, at a glance:**

```text
Telegram channels                 FreeHub (polled every
(Telethon, app/handlers/           FREEHUB_POLL_INTERVAL)
 telegram.py)                      app/freehub.py +
        │                          app/freehub_worker.py
        ▼                                  │
app/message_processor.py                   │
        │                                  │
        └──────────────┬───────────────────┘
                        ▼
              app/job_processor.py
                        │
                        ▼
      Deterministic classifier — app/filters.py
          (data: app/keywords.py, app/normalize.py)
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
     reject      notify_directly    needs_gemini
                        │                │
                        │                ▼
                        │      app/llm/manager.py
                        │      (Gemini → Groq fallback)
                        │                │
                        └───────┬────────┘
                                ▼
                 [only if run_guarded.py installed
                  the guard AND it's enabled]
                 app/notification_guard/ — direct-
                 accept jobs only; LLM-reviewed jobs
                 bypass it
                                │
                                ▼
              app/notifier.py (private chat)
        app/channel_notifier.py (optional channel)
                                │
                                ▼
                   app/logger.py (Excel workbook)
```

**Reading order:**
- First-time setup → [Installation](#2-installation) → [Configuration](#3-configuration) → [Deployment](#17-deployment).
- Retargeting the classifier → [Deterministic Classifier](#10-deterministic-classifier).
- Understanding AI review → [LLM Subsystem](#11-llm-subsystem) → [Notification Guard](#12-notification-guard).
- Contributing code → [Production Architecture](#4-production-architecture) → [Testing / Development](#19-testing--development).

---

## 2. Installation

### Prerequisites

- Python 3.11+
- A personal Telegram account (Loki logs in as a **user**, not a bot,
  via Telethon, so it can read channel history and receive live
  messages the way a bot account cannot)
- A separate Telegram Bot (via BotFather) used only for sending
  notifications
- One or more Google Gemini API keys
- A Groq API key (used as a fallback if every Gemini key fails)
- A FreeHub user ID

### 2.1 Clone and Install

```bash
git clone <repository-url>
cd freelance-assistant

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

Installed packages (`requirements.txt`):

| Package | Version | Role |
|---|---|---|
| `Telethon` | 1.44.0 | Logs in as a Telegram user, listens for new channel messages, performs recovery backfill |
| `python-telegram-bot` | 22.8 | Sends notification messages via a Telegram Bot |
| `google-genai` | 2.11.0 | Calls the Gemini API for borderline-job review |
| `groq` | 1.6.0 | Calls the Groq API — both the main LLM fallback and the Notification Guard |
| `openpyxl` | 3.1.5 | Reads/writes the `.xlsx` audit log |
| `python-dotenv` | 1.2.2 | Loads `.env` into environment variables |
| `tenacity` | 9.1.4 | Retries transient LLM API failures |
| `aiohttp` | 3.14.2 | Polls the FreeHub API |
| `requests` | 2.34.2 | Transitive dependency |

### 2.2 Configure `.env`

```bash
cp .env.example .env
```

Fill in every value in `.env.example` — see [Configuration](#3-configuration)
for the full variable reference, and note in particular:

- `GEMINI_API_KEYS` (plural) takes a **comma-separated list** of one or
  more keys — not a single `GEMINI_API_KEY`.
- `BOT_CHANNEL_ID` and the entire Notification Guard block are
  optional.

### 2.3 First Run — Telethon Login

```bash
python run.py
```

On the first run, Telethon has no saved session and prompts
interactively in the terminal: a login code sent to your Telegram
app, and your 2FA cloud password if enabled. The session is then
saved (SQLite file, path derived from `SESSION_NAME` in
`app/config.py`) and reused on subsequent runs.

Because systemd cannot answer this interactive prompt, always
complete this step manually before installing a systemd service — see
[Deployment](#17-deployment).

`app/bot.py` also calls `initialize_workbook()` and `state.load()`
before starting Telegram/FreeHub, creating `logs/freelance_bot_logs.xlsx`
and `database/state.json` if they don't already exist.

---

## 3. Configuration

All configuration is environment-variable based, loaded via
`python-dotenv` and validated at import time in `app/config.py`. A
missing or malformed required variable raises `RuntimeError`
immediately on startup.

### Environment Variables

| Variable | Required | Type | Description |
|---|---|---|---|
| `API_ID` | Yes | integer | Telegram application ID (my.telegram.org), used by Telethon. |
| `API_HASH` | Yes | string | Telegram application hash, paired with `API_ID`. |
| `PHONE_NUMBER` | Yes | string | Phone number (international format) of the account Loki logs in as. |
| `GEMINI_API_KEYS` | Yes | comma-separated strings | One or more Gemini API keys. Parsed into a list; each is tried in order (see [LLM Subsystem](#11-llm-subsystem)). At least one non-empty key is required. |
| `GROQ_API_KEY` | Yes | string | API key for the main Groq fallback LLM. |
| `BOT_TOKEN` | Yes | string | Token for the notification Bot (BotFather). |
| `BOT_CHAT_ID` | Yes | integer | Chat ID the bot sends every private notification to. |
| `BOT_CHANNEL_ID` | No | string | Optional channel ID for broadcasting accepted jobs publicly. If unset, `app/channel_notifier.py` skips channel notifications entirely (returns success without sending). |
| `TARGET_CHANNEL_IDS` | Yes | comma-separated integers | Telegram channel/supergroup IDs Loki listens to, parsed into a `set[int]`. |
| `FREEHUB_USER_ID` | Yes | string | FreeHub account/user identifier used to build the polling URL. |
| `FREEHUB_POLL_INTERVAL` | No (default `60`) | integer | Seconds between FreeHub polls. |
| `FREEHUB_PAGE_SIZE` | No (default `30`) | integer | Page size requested from the FreeHub API. |
| `NOTIFICATION_GUARD_ENABLED` | No (default `false`) | boolean-like string (`1`/`true`/`yes`/`on`) | Enables the Notification Guard. Read independently by `app/notification_guard/config.py`, which loads `.env` itself. |
| `GROQ_NOTIFICATION_GUARD_API_KEY` | Only if guard enabled | string | Groq API key used exclusively by the Notification Guard. |
| `GROQ_NOTIFICATION_GUARD_MAX_RETRIES` | No (default `2`) | integer | `stop_after_attempt` value for the guard's transient-error retry. |

Two derived, non-environment values are also defined in
`app/config.py`:

- `BASE_DIR` — the project root.
- `SESSION_NAME` — `BASE_DIR / "sessions" / "telegram"`, the path
  Telethon persists its login session under.

### Validation Behavior

`app/config.py` defines helpers used at import time:

- **`_require_env(name)`** — raises `RuntimeError` if missing or
  empty/whitespace-only.
- **`_require_int_env(name)`** — `_require_env` plus an `int()` cast,
  raising `RuntimeError` with the offending value on failure.
- **`_require_channel_ids(name)`** — splits on commas, strips
  whitespace, casts each entry to `int`, returns a `set[int]`.
- **`_optional_int_env(name)`** — like `_require_int_env` but returns
  `None` if unset (defined but not currently used by any variable in
  this file — the FreeHub interval/page-size defaults instead use a
  plain `os.getenv(..., "default")` pattern).

`app/notification_guard/config.py` is intentionally independent of
`app/config.py` — it calls its own `load_dotenv()` so the guard can be
imported and configured before `app.config` runs.

### Notification Guard Models — Code Constant, Not Configurable

`NOTIFICATION_GUARD_MODELS` (the list of Groq models the guard rotates
through) is a hardcoded list in `app/notification_guard/config.py`,
not read from any environment variable. There is deliberately no
`GROQ_NOTIFICATION_GUARD_MODEL(S)` environment variable.

### TARGET_CHANNEL_IDS

`app/handlers/telegram.py` passes the parsed set directly to
Telethon's event filter:

```python
@client.on(events.NewMessage(chats=list(TARGET_CHANNELS)))
```

Telegram channel/supergroup IDs are negative numbers, conventionally
prefixed `-100`. Loki's user account must already be a member of a
channel to receive its messages. Changing this list requires a
restart — there is no hot-reload.

---

## 4. Production Architecture

Loki is a single-process, async application. `app/bot.py` runs two
concurrent coroutines under `asyncio.gather`:

```python
await asyncio.gather(
    start(),            # app/handlers/telegram.py — Telethon client
    freehub_worker(),    # app/freehub_worker.py — FreeHub polling loop
)
```

Both ultimately call the same `app.job_processor.process_job()`, so
Telegram-sourced and FreeHub-sourced jobs share identical
classification, review, notification, and logging behavior — only the
ingestion and parsing stages differ.

### Module Responsibilities

| Module | Responsibility |
|---|---|
| `run.py` | Standard entry point. Calls `app.bot.main()`. |
| `run_guarded.py` | Same as `run.py`, but first calls `app.notification_guard.integration.install()` to wrap the notification functions with the guard. |
| `app/bot.py` | Initializes the Excel workbook, loads persisted state, then runs the Telegram client and FreeHub worker concurrently. |
| `app/config.py` | Loads and validates all core environment variables. |
| `app/handlers/telegram.py` | Owns the Telethon client: logs in, performs per-channel startup recovery, registers the `NewMessage` handler, dispatches to `process_message`. |
| `app/message_processor.py` | Telegram-specific entry point: extracts text/source/URL(via buttons) from a Telethon event, calls `parse_job`, then `process_job`; catches and logs any exception with a safety-net save. |
| `app/parser.py` | Converts raw message text into a structured job dict (title, description, budget, url, source, raw_text). Source-specific logic for Nafezly; generic fallback otherwise. |
| `app/freehub.py` | Polls the FreeHub API for two sources (`kafiil`, `freelancer`), deduplicates against a persisted "seen" set, and returns only new projects (with bounded multi-page backfill). |
| `app/freehub_worker.py` | Runs `poll_once()` in a loop (`FREEHUB_POLL_INTERVAL` seconds), builds a job dict per new project, and calls `process_job` directly (FreeHub projects don't go through `parser.py` — they're already structured). |
| `app/normalize.py` | Text normalization (Arabic character unification, diacritic/tatweel stripping, punctuation/separator handling, lowercasing) used before keyword matching. |
| `app/keywords.py` | The classifier's data: `POSITIVE_KEYWORDS`, `NEGATIVE_KEYWORDS`, `HARD_REJECT_KEYWORDS`, each keyword tagged with a category, tier (`core`/`supporting`), and weight. |
| `app/filters.py` | The classifier logic: `keyword_filter(text, title)`, a rule-based decision table (not additive scoring) built from `keywords.py` + `normalize.py`. |
| `app/job_processor.py` | The shared orchestrator for both ingestion paths: dedup check, runs the classifier, routes to Gemini/Groq if needed, sends notifications, logs everything. |
| `app/llm/manager.py` | `evaluate_job()` — tries Gemini first, falls back to Groq only if Gemini raises. |
| `app/llm/gemini.py` | Gemini client(s), one per configured API key; per-key retry on transient errors, then tries the next key. |
| `app/llm/groq.py` | Groq client; rotates through a fixed model list on failure. |
| `app/llm/prompt.py` | The shared system prompt (freelancer profile + evaluation rules) used by both Gemini and Groq. |
| `app/llm/utils.py` | `build_prompt()` (embeds the classifier's evidence trail + job text) and `parse_response()` (strips code fences, validates required JSON keys). |
| `app/notification_guard/` | Optional, independently-loaded second-opinion safety layer — see [Notification Guard](#12-notification-guard). |
| `app/message_builder.py` | Builds the HTML-formatted Telegram notification text shared by both notifiers, with length truncation to stay under Telegram's message limit. |
| `app/notifier.py` | Sends the private notification to `BOT_CHAT_ID`. |
| `app/channel_notifier.py` | Sends the public channel notification to `BOT_CHANNEL_ID`, if configured. |
| `app/telegram_bot.py` | The single shared `python-telegram-bot` `Bot` instance used by both notifiers. |
| `app/state.py` | Persists Telegram per-channel watermarks and FreeHub per-source seen-ID lists to `database/state.json`, with atomic (temp-file + rename) writes. |
| `app/logger.py` | `ExcelLogger` — all workbook reads/writes funneled through one dedicated worker thread (`ThreadPoolExecutor(max_workers=1)`), since `openpyxl`'s `Workbook` is not thread-safe and multiple coroutines mutate it concurrently. |

### Why This Shape

- **Deterministic classifier before any LLM call.** `app/filters.py`
  is a rule-based decision table (see
  [Deterministic Classifier](#10-deterministic-classifier)), not an
  additive score. It runs first because it's fast and free; Gemini
  (then Groq) is reserved for genuinely ambiguous posts.
- **Two independent ingestion paths, one shared pipeline.** Telegram
  and FreeHub each have their own ingestion/parsing code, but both
  call `app.job_processor.process_job()`, so classification, LLM
  review, notification, and logging are identical regardless of
  source.
- **The Notification Guard is opt-in and additive.** It is installed
  by replacing `app.job_processor`'s in-memory references to
  `send_notification`/`send_channel_notification` at runtime
  (`app/notification_guard/integration.py`) — no production module is
  edited to add it. Running `run.py` instead of `run_guarded.py`
  produces byte-identical behavior to a build that never had the
  guard.
- **One dedicated logger thread.** Every workbook read/write, from
  every subsystem (including the Notification Guard's own logging),
  is routed through `ExcelLogger.run()` onto the same single worker
  thread, so no two workbook operations — across either ingestion
  path — can ever race.

---

## 5. Telegram Ingestion

`app/handlers/telegram.py` constructs a `TelegramClient(SESSION_NAME,
API_ID, API_HASH)`, logs in, performs startup recovery for every
channel in `TARGET_CHANNELS` (see [Recovery](#16-recovery)), then
registers:

```python
@client.on(events.NewMessage(chats=list(TARGET_CHANNELS)))
async def handler(event):
    ...
    await process_message(event)
    state.set_last_message_id(event.chat_id, event.id)
```

Telethon filters at the transport level — messages from chats outside
`TARGET_CHANNELS` never reach the handler. Any exception from
`process_message` is caught, logged to the Errors sheet as
`"MessageHandler"`, and printed; it does not kill the client's
`run_until_disconnected()` loop.

`app/message_processor.py` (`process_message`) is the actual entry
point called by the handler above:

1. Extracts `event.raw_text` and the chat title as `source`.
2. Calls `parse_job(source, text)`.
3. If no URL was found in the text itself, checks the message's
   inline keyboard buttons (`event.buttons`), trying both
   `button.url` and `button.button.url` (different Telethon button
   wrapper shapes), using the first non-empty URL found.
4. Calls `process_job(job=job, job_id=str(event.id))`.
5. On any exception, logs it to the Errors sheet as
   `"Message Processor"` and performs a safety-net `logger.save()` —
   needed only because `process_job()`'s own save wouldn't have run if
   the exception occurred before `process_job` was even reached (e.g.
   inside `parse_job`).

---

## 6. FreeHub Ingestion

`app/freehub.py` polls an HTTP API (backend host is hardcoded in the
module; the account is identified by `FREEHUB_USER_ID`) across two
sources, `kafiil` and `freelancer`, defined as `SOURCES` in the
module.

`poll_once()`:

1. Lazily seeds an in-memory "seen" `deque` per source from
   `database/state.json` on first call (deferred because `state.load()`
   runs after this module is imported).
2. Fetches page 1 for each source.
3. If nothing has been seen yet for a source (true first run), seeds
   the cache from page 1 and returns nothing — no jobs are processed
   on the very first poll for a source.
4. Otherwise, if **every** project on the most recently fetched page
   is unseen, walks additional pages (oldest direction) until a page
   contains an already-seen project, or `_MAX_BACKFILL_PAGES` (10) is
   reached — printing a `[FREEHUB WARNING]` if the cap is hit.
5. Returns every newly-seen project (oldest → newest across whatever
   pages were fetched this cycle) and persists the updated seen list
   via `app.state.state.set_freehub_seen`.

`app/freehub_worker.py` runs this in a `while True` loop, sleeping
`FREEHUB_POLL_INTERVAL` seconds between polls. For each new project it
builds a job dict directly (no `parser.py` involvement — FreeHub
already returns structured fields):

```python
job = {
    "title": project["title"],
    "description": project["description"],
    "raw_text": f"{project['title']}\n\n{project['description']}",
    "source": project.get("platform", "FreeHub"),
    "budget": project.get("price", ""),
    "url": project.get("project_link", ""),
}
```

and calls `process_job(job=job, job_id=project["uid"])`. Both a
per-project exception (logged as `"FreeHub Project"`) and a
poll-cycle-level exception (logged as `"FreeHub Worker"`) are caught
so one bad project or one failed poll never kills the loop.

---

## 7. Parsing

`app/parser.py`'s `parse_job(source, text)` only runs for
**Telegram-sourced** jobs (FreeHub jobs are built directly, see
above). It returns a dict: `title`, `description`, `budget`, `url`,
`source`, `raw_text`.

- If `source` (the chat title, lowercased) contains `"nafezly"`,
  Arabic-language regexes extract `عنوان المشروع` (title),
  `تفاصيل المشروع` (description, up to `الميزانية` or end of string),
  and `الميزانية` (budget). If the title regex doesn't match,
  `_fallback_title()` is used instead.
- For every other source, the fallback path runs directly: title =
  first non-empty line (`_fallback_title`), description = the full
  text run through `_normalize_description()` (collapses single
  newlines to spaces while preserving paragraph breaks, trims
  trailing whitespace per line, collapses repeated spaces).
- `url` is extracted via `_extract_url()` (a plain `https?://\S+`
  regex against the raw text) for both paths.

---

## 8. Normalization

`app/normalize.py`'s `normalize(text)` runs before every keyword
match (both against the job text and, once, against every keyword
itself at import time in `app/filters.py`):

1. Lowercase.
2. Arabic character unification via a translation table: hamza
   variants (`أ إ آ ٱ` → `ا`), `ى` → `ي`, `ؤ` → `و`, `ئ` → `ي`, and
   taa marbuta → haa (`ة` → `ه`) — the last specifically to handle
   common informal Telegram spelling variance (e.g. `لوحه` vs.
   `لوحة`).
3. Arabic diacritics stripped (`\u064B`–`\u065F`, `\u0670`).
4. Arabic tatweel/kashida stripped (`\u0640`).
5. Separator characters (`- _ / \ |`) replaced with spaces.
6. Remaining punctuation stripped.
7. Whitespace collapsed and trimmed.

---

## 9. Job Processing

`app/job_processor.py`'s `process_job(job, job_id)` is the shared
orchestrator for both ingestion paths.

1. **Deterministic dedup.** A `job_uuid` is derived via `uuid5` from
   `(source, job_id)` — the *same* underlying message/project always
   maps to the same UUID, so if it's reprocessed (state reset, cache
   reset on restart) it's recognized as a duplicate rather than
   logged/notified twice. If `logger.has_job(job_uuid)` is already
   true, processing stops immediately.
2. **Classify.** `keyword_filter(f"{title}\n{description}", title=title)`
   runs (see [Deterministic Classifier](#10-deterministic-classifier)).
3. **Log the initial row** (`logger.create_job`, unsaved) with the
   full classifier evidence trail.
4. **Route** based on the classifier's decision:
   - `hard_reject` → `Rejected`, reason `"Hard Reject"`.
   - not `matched` → `Rejected`, reason `"No Matching Keywords"`.
   - `notify_directly` → `Accepted`, reason = the classifier's own
     `reason` string, `should_notify = True`.
   - `needs_gemini` → calls `app.llm.manager.evaluate_job` (off the
     event loop via `asyncio.to_thread`). On success: `Accepted`/
     `Rejected` based on `gemini["decision"]`, logged to the Gemini
     sheet. On exception (both Gemini and Groq exhausted — see
     [LLM Subsystem](#11-llm-subsystem)): `Rejected`, reason
     `"LLM Error"`, logged to the Errors sheet as `"LLM"`.
   - none of the above → `Rejected`, reason `"Below Gemini Threshold"`.

   > **Verified quirk:** this catch-all branch fires for every
   > `filters.py` rejection reason other than `hard_reject_keyword`
   > and the no-match case above — including `insufficient_signal`,
   > `core_negative_no_core_positive`, and
   > `title_core_negative_no_body_positive` — any of which can still
   > have `matched=True` (e.g. via supporting-positive hits alongside
   > a core-negative or below-threshold core-positive signal) and so
   > skip the `"No Matching Keywords"` branch too. In those cases the
   > Jobs sheet's `Decision Reason` column shows the generic
   > `"Below Gemini Threshold"` label rather than the classifier's own
   > more specific `reason` string. This is current, verified
   > behavior of `app/job_processor.py` — flagged here for anyone
   > tuning keywords off the Jobs sheet, and reported separately as a
   > candidate code fix outside the scope of this documentation pass.
5. **Update the row** with the final decision/reason.
6. **If `should_notify`**, calls `send_notification` and
   `send_channel_notification` (both awaited in sequence; each is
   independently the Notification Guard's wrap point when
   `run_guarded.py` is used — see
   [Notification Guard](#12-notification-guard)), then logs each
   attempt's `Sent`/`Failed` status.
7. **One `logger.save()`** at the end, on the same logger thread every
   other write in this job used — replacing what used to be a save
   per intermediate step.

---

## 10. Deterministic Classifier

The classifier is a **decision table**, not an additive score. Rules
are evaluated top-to-bottom; the first matching rule wins. This
replaced an earlier score-threshold model — there is no numeric
"score" anywhere in the current implementation.

### Keyword Data Model (`app/keywords.py`)

Every keyword belongs to exactly one `(polarity, category, tier)`:

- **Polarity**: `POSITIVE_KEYWORDS` (evidence the job IS Data
  Analysis / Excel / Power BI / SQL / Python-for-data — categories:
  `power_bi`, `excel`, `sql`, `data_analysis`, `python`) or
  `NEGATIVE_KEYWORDS` (evidence it's something else — categories
  include `web`, `backend`, `mobile`, `design`, `education`,
  `enterprise`, `nocode`, `ai_apps`, `devops`, `automation`, `cad`,
  `marketing`).
- **Tier**: `core` (unambiguous alone — presence of one core keyword
  is sufficient evidence) or `supporting` (consistent with the domain
  but too generic alone — only matters in aggregate or to break ties).
- A separate `HARD_REJECT_KEYWORDS` set (unpaid work, internships,
  translation, etc.) acts as an independent safety net that overrides
  everything else, checked against the full text regardless of any
  positive match.

Both English and Arabic keyword variants are included throughout,
since the monitored sources post in both languages.

### Matching (`app/filters.py`)

- `_contains_keyword` uses `\b`-word-boundary regex matching — Python's
  `\b` is Unicode-aware and works correctly for Arabic script too
  (verified directly against Arabic word-boundary cases), with one
  known, accepted tradeoff: a keyword won't match when a common
  Arabic prefix (`ل ب و ك ال`) is attached directly with no space.
- Within a tier, keywords are matched longest-normalized-phrase-first
  and matched text is masked out afterward, so a longer phrase (e.g.
  `"excel sheet"`) claims its span before a shorter contained keyword
  (`"sheet"`) can also match. Matching is independent **across**
  tiers/polarities by design.
- Hard-reject keywords are checked against the full normalized text
  and, unlike the previous model, **are not disabled by a positive
  match** — a post can match a positive keyword and a hard-reject
  keyword simultaneously, and hard-reject still wins.

### Title Signal

If a `title` is passed to `keyword_filter`, a core keyword found in
the title is treated as the single strongest available signal (the
client wrote the title specifically to say what the job IS) — but it
does not blindly override a genuine core-negative signal found in the
body; that combination routes to Gemini instead (`title_positive_but_body_core_negative`).

### Decision Table (evaluated top to bottom, first match wins)

1. `hard_reject` → **reject**.
2. Title core-positive, no title core-negative, no body core-negative
   → **notify_directly** (`title_core_positive`).
3. Title core-positive, no title core-negative, **but** body has a
   core-negative → **needs_gemini** (`title_positive_but_body_core_negative`).
4. Title core-negative, no title core-positive, no body core-positive
   at all → **reject** (`title_core_negative_no_body_positive`).
5. Body has both core-positive and core-negative → **needs_gemini**
   (`mixed_core_signals`).
6. Body has core-negative, no core-positive → **reject**
   (`core_negative_no_core_positive`).
7. Body has core-positive, no core-negative:
   - Exactly one core-positive hit **and** supporting-positive weight
     `< MIN_SUPPORTING_POSITIVE_FOR_LONE_CORE` (5) → **needs_gemini**
     (`lone_core_positive_insufficient_support`) — a single
     unconfirmed core hit is too thin to trust blindly.
   - Otherwise, if supporting-negative weight
     `>= SUPPORTING_NEGATIVE_DOWNGRADE_THRESHOLD` (14) →
     **needs_gemini** (`core_positive_but_heavy_supporting_negative`).
   - Otherwise → **notify_directly** (`core_positive_clean`).
8. No core signal either direction: if supporting-positive weight
   `>= SUPPORTING_POSITIVE_MIN_FOR_GEMINI` (12) → **needs_gemini**
   (`supporting_positive_only`); otherwise → **reject**
   (`insufficient_signal`).

### Tunable Thresholds

These three constants at the top of `app/filters.py` are the *only*
numeric tuning knobs in the decision path:

| Constant | Value | Meaning |
|---|---|---|
| `SUPPORTING_POSITIVE_MIN_FOR_GEMINI` | 12 | Minimum supporting-positive weight to send to Gemini when no core signal fired at all. |
| `SUPPORTING_NEGATIVE_DOWNGRADE_THRESHOLD` | 14 | Supporting-negative weight at/above which a clean core-positive match is downgraded to Gemini review instead of a direct accept. |
| `MIN_SUPPORTING_POSITIVE_FOR_LONE_CORE` | 5 | Minimum supporting-positive weight required to trust a single, uncorroborated core-positive hit. |

### Retargeting the Classifier

1. Add/adjust categories and keywords in `app/keywords.py` — for every
   new/changed keyword in one language, look for a corresponding
   equivalent in the other (English ↔ Arabic) so bilingual coverage
   stays balanced.
2. Adjust the three thresholds above in `app/filters.py` if evidence
   supports it — the decision-table *logic* itself is intended to
   stay stable; only these constants are meant to be tuned.
3. If the target niche changes materially, the freelancer profile in
   `app/llm/prompt.py`'s `SYSTEM_PROMPT` must be rewritten too — it is
   what Gemini/Groq actually judge borderline posts against,
   independent of the keyword categories.
4. Run the relevant tests in `tests/` (see
   [Testing / Development](#19-testing--development)) against
   representative sample posts before deploying a change.

---

## 11. LLM Subsystem

Invoked only when the classifier returns `needs_gemini` (see
[Deterministic Classifier](#10-deterministic-classifier)). Both
providers share the same system prompt (`app/llm/prompt.py`) and
request/response contract (`app/llm/utils.py`).

### Manager (`app/llm/manager.py`)

```python
def evaluate_job(text, filter_result):
    try:
        return gemini_evaluate(text, filter_result)
    except Exception as gemini_error:
        try:
            return groq_evaluate(text, filter_result)
        except Exception as groq_error:
            raise RuntimeError(...) from groq_error
```

Groq is only attempted if Gemini's own multi-key attempt (below)
raises. If Groq also fails, a single `RuntimeError` combining both
providers' final exceptions is raised back to `job_processor.py`,
which logs it once to the Errors sheet as `"LLM"` (not
provider-specific, since by this point both providers failed).

### Gemini (`app/llm/gemini.py`)

- Configured via `GEMINI_API_KEYS` (comma-separated) — one
  `genai.Client` is constructed per key at import time.
- Model: **`gemini-3.5-flash`**, called via
  `client.models.generate_content()`.
- Per-key retry: `@retry(retry_if_exception(_is_transient),
  stop_after_attempt(2), wait_fixed(1), reraise=True)` — only retries
  errors matching transient markers (`429`, `503`,
  `resource_exhausted`, `quota exceeded`, `unavailable`, `timeout`,
  `timed out`); a malformed request or parse failure is not retried.
- **Key rotation**: keys are tried in order; **any** exception from a
  key (including a non-transient one, including a response-parsing
  failure) moves on to the next key — a bad response from key #1 says
  nothing about whether key #2 would work. Only once every key has
  failed does `evaluate_job` re-raise the last exception.

### Groq (`app/llm/groq.py`)

- Configured via `GROQ_API_KEY` (single key).
- **Model rotation list** (`GROQ_MODELS`, tried in this exact order):
  1. `openai/gpt-oss-120b`
  2. `llama-3.3-70b-versatile`
  3. `qwen/qwen3.6-27b`
- Same transient-error retry pattern as Gemini
  (`stop_after_attempt(2)`, `wait_fixed(1)`).
- Same any-failure-moves-to-next-model behavior as Gemini's key
  rotation. Once every model has failed, the last exception is
  re-raised.
- Requests use `response_format={"type": "json_object"}`.

### Prompt Construction and Response Parsing (`app/llm/utils.py`)

`build_prompt()` embeds the classifier's full evidence trail
(decision, reason, matched categories, core/supporting matches on
both polarities, supporting weights) alongside the raw job text,
wrapped in an explicit `<JobDescription>` tag with an instruction to
treat it as **untrusted content** and ignore any instructions
contained within it — a prompt-injection defense, since job post text
is attacker-controllable public input.

`parse_response()`:
1. Strips a leading/trailing ` ```json `/` ``` ` fence if present.
2. Parses as JSON.
3. Validates all six required keys are present (`decision`,
   `confidence`, `project_type`, `primary_deliverable`, `reason`,
   `skills_detected`) — raises `ValueError` if any are missing. This
   is what makes a malformed response count as a provider failure and
   trigger key/model/provider rotation rather than silently returning
   a default.

### The System Prompt (`app/llm/prompt.py`)

A single shared `SYSTEM_PROMPT` hardcodes the freelancer profile
(Data Analysis / BI / Power BI / Excel / SQL / Python — explicitly
**not** web/frontend/backend/full-stack/mobile/DevOps/enterprise
software) and detailed evaluation rules, most notably:

- Judge the **primary deliverable**, not which technologies are
  merely mentioned.
- A dedicated **data-entry / transcription exclusion**: Excel, CSV,
  Power BI, or a dashboard as the deliverable does *not* by itself
  mean "Data Analysis" — manual data entry, PDF/OCR-to-Excel
  transcription, spreadsheet population, contact/lead list building,
  and form-filling are explicitly rejected even when the final
  artifact is an Excel file.
- Be conservative when uncertain — false positives are treated as
  worse than false negatives.
- Confidence bands: 95–100 excellent match, 80–94 strong match, 60–79
  borderline but possible, 0–59 reject.

---

## 12. Notification Guard

An **optional, independent second-opinion safety layer** — not part
of the classifier, and not a mechanism for automatically correcting
or retraining the classifier. It exists to catch cases where the
deterministic classifier's `notify_directly` decision (i.e. no
LLM ever reviewed this specific job) turns out, on a second Groq-based
look, to not actually be Data Analysis / BI work.

**Important distinction:** a guard result of `do_not_notify` means the
notification safety layer blocked delivery for this one job — it does
**not** mean the classifier itself was wrong, and it does not
automatically train, correct, or modify the classifier or its
keyword data in any way. Guard decisions are recorded for later human
review, not consumed by any automated feedback loop in this codebase.

### Installation

The guard is not active under `run.py`. It is installed only by
`run_guarded.py`, via `app.notification_guard.integration.install()`,
which replaces `app.job_processor`'s in-memory
`send_notification`/`send_channel_notification` references with
wrapped versions — no production source file is edited to do this.

### Which Jobs It Evaluates

- **Direct-accept jobs** (`ai_used=False` at notification time, i.e.
  the classifier's `notify_directly` fired and no Gemini/Groq review
  ever happened) — evaluated by the guard.
- **LLM-reviewed jobs** (`ai_used=True`, i.e. Gemini or Groq already
  made the accept call) — **bypass the guard completely** and are
  always allowed through (see `NotificationGuardIntegration._allow`).

### Evaluation and Caching

The guard is evaluated **exactly once per job**, on whichever of the
two notification calls (private or channel) runs first; the same
decision is cached and reused for the other call so a single job
never triggers two guard evaluations. The cache entry is discarded
after both notification calls have consumed it.

### Enabled / Disabled Behavior

`NotificationGuard.__init__` reads `NOTIFICATION_GUARD_ENABLED` once.
If disabled, `allow()` always returns `True` immediately — the guard
has zero effect, and `run_guarded.py` behaves identically to `run.py`.

### Fail-Closed Behavior

If enabled, any exception during evaluation — a Groq call failing
after all models are exhausted, a malformed response, anything —
results in `allow()` returning `False`: the notification is
**suppressed**, not allowed through. The failure is still logged (see
below) with `guard_decision="error"` and the exception text.

### Model Rotation

Uses its own `GroqNotificationGuard` client
(`app/notification_guard/groq.py`), configured from
`GROQ_NOTIFICATION_GUARD_API_KEY`, rotating through the **same three
models, in the same order**, as the main Groq fallback (this is a
code constant in `app/notification_guard/config.py`, not an
environment variable — see [Configuration](#3-configuration)):

1. `openai/gpt-oss-120b`
2. `llama-3.3-70b-versatile`
3. `qwen/qwen3.6-27b`

Retry count per model is `GROQ_NOTIFICATION_GUARD_MAX_RETRIES`
(default `2`). Once a model returns a **valid** `notify`/`do_not_notify`
decision, that result is final — the guard does not keep rotating
models after a valid response. A model is only skipped (moving to the
next) on an exception (network/transient error, malformed JSON, or an
invalid `decision` value).

### Response Contract

The guard's own system prompt (`app/notification_guard/prompt.py`)
instructs the model to return only `{"decision": "notify" |
"do_not_notify"}` given the job's title and description — no
confidence score, no reasoning field. Any other value for `decision`
raises `ValueError`, which counts as a model failure for rotation
purposes.

### Logging

Every guard evaluation — success or `error` — is recorded in the
**NotificationGuard** worksheet via `app/notification_guard/logger.py`,
which shares the existing `ExcelLogger`'s workbook, worker thread, and
save mechanism (no separate workbook or executor). The sheet is
created lazily on the first guard evaluation, since a workbook that's
never had the guard run doesn't need the sheet at all.

---

## 13. Notifications

`app/message_builder.py`'s `build_job_message()` builds the
HTML-formatted text shared by both notifiers, with a
`channel_style` flag controlling the two formats:

- **Private** (`app/notifier.py`, `channel_style=False`): full detail
  — AI-used vs. direct-match header, platform, project title, budget,
  description (truncated at 3000 characters), categories, and the
  classifier/LLM's reason text.
- **Channel** (`app/channel_notifier.py`, `channel_style=True`):
  simplified — no AI/direct header, no reason text, hashtag-style
  categories instead of a plain list.

The full message (not just the description) is bounded to Telegram's
4096-character limit minus a 96-character safety margin, since title/
budget/reason can each independently push the message over the limit
even with the description already truncated.

`app/notifier.py` always targets `BOT_CHAT_ID`. `app/channel_notifier.py`
returns `True` immediately (without sending) if `BOT_CHANNEL_ID` is
unset. Both attach an inline "Open Project" button if a URL is
present, and both independently catch and log send failures (to the
Errors sheet) rather than propagating exceptions — a failed private
notification does not prevent the channel notification from being
attempted, and vice versa.

Both notifiers share a single `python-telegram-bot` `Bot` instance
(`app/telegram_bot.py`), since both send to the same bot account.

---

## 14. Excel Logging

`app/logger.py`'s `ExcelLogger` is Loki's only persistence layer for
job history — there is no database.

### Workbook Location and Initialization

`logs/freelance_bot_logs.xlsx`, created via `initialize_workbook()`
(called once, from `app/bot.py`, before Telegram/FreeHub start). If
the file doesn't exist, a new workbook is created with four sheets —
**Jobs**, **Gemini**, **Notifications**, **Errors** — each with its
header row, and saved. The **NotificationGuard** sheet is *not*
created at initialization; it's added lazily by
`app/notification_guard/logger.py` the first time the guard actually
evaluates a job (so a workbook from a deployment that's never enabled
the guard won't have this sheet at all).

Either way, the workbook is then loaded and an in-memory
`job_uuid → row number` index is rebuilt by scanning the Jobs sheet,
so later updates don't require a linear search.

### Thread Safety

All five worksheets are read/written exclusively through
`ExcelLogger.run()`, which dispatches to a single dedicated
`ThreadPoolExecutor(max_workers=1)` thread. `openpyxl`'s `Workbook` is
not thread-safe, and multiple coroutines (Telegram handler, FreeHub
worker, both running under `asyncio.gather`) mutate the same
in-memory workbook — routing every access through one worker thread
makes all of it strictly serial, with no two logger calls (read or
write, from either ingestion path or the guard) ever touching the
workbook concurrently.

### The Five Worksheets

- **Jobs** — one row per job (via `create_job`), updated in place
  (via `update_job`) as the pipeline progresses. Columns include the
  full classifier evidence trail (categories, core/supporting hit
  counts and weights, matched keywords per tier/polarity, hard-reject
  matches), plus `Gemini Decision`, `Notification Status`, `Final
  Decision`, `Decision Reason`, and `Filter Time (ms)`.
- **Gemini** — one row per Gemini/Groq review call (`log_gemini`):
  decision/reason before review, response time, decision, confidence.
  `Prompt Tokens`/`Completion Tokens` columns exist in the header but
  are currently always written as empty strings — reserved, not
  populated.
- **Notifications** — one row per notification *attempt* (private and
  channel logged independently): platform (`"Telegram"` or `"Telegram
  Channel"`), status (`"Sent"` or `"Failed"`).
- **Errors** — one row per caught exception anywhere in the pipeline,
  with a short module label (`"Message Processor"`, `"LLM"`,
  `"Notifier"`, `"ChannelNotifier"`, `"StartupRecovery"`,
  `"MessageHandler"`, `"FreeHub Project"`, `"FreeHub Worker"`,
  `"Logger"`) and `str(exception)`.
- **NotificationGuard** — one row per guard evaluation (only present
  if the guard has run at least once): original decision, guard
  decision (`notify`/`do_not_notify`/`error`), provider, model,
  response time, and error text if applicable.

### Save Behavior

Most mutating calls accept `save=False` so a job's several writes
(create → update → gemini log → update → notification logs) can be
batched into a **single** full-workbook save at the end of
`process_job` (via `logger.run(logger.save)`), instead of a save per
intermediate step. `message_processor.py`'s exception handler performs
its own safety-net save only if an exception occurred *before*
`process_job` had a chance to run its own final save.

---

## 15. State

`app/state.py`'s `StateManager` persists to `database/state.json`,
loaded once at startup (`state.load()`, called from `app/bot.py`) and
written atomically (temp file + `os.replace()`) on every mutation, so
a crash mid-write can't corrupt the file (a corrupt/missing file is
otherwise treated as "no state at all," which would trigger a full
Telegram re-recovery and FreeHub re-seed).

Two independent schemas share the same file:

- **Telegram watermarks** — bare top-level keys, `str(channel_id) →
  last_message_id`.
- **FreeHub seen state** — nested under the `_freehub_seen` key,
  `source → [seen project uids]`.

---

## 16. Recovery

### Telegram

On startup, `app/handlers/telegram.py`'s `_recover_channel()` runs for
every monitored channel:

- If no state is recorded for a channel yet (`last_id == 0`), the
  channel is **seeded** from its current newest message only — no
  backfill, since there's no natural stopping point for "how far back"
  the very first time a channel is seen.
- Otherwise, every message newer than `last_id` is fetched (oldest →
  newest) and processed through the normal pipeline, up to a safety
  cap of `MAX_RECOVERY_MESSAGES` (2000) per channel. If the cap is
  hit, a `[RECOVERY WARNING]` is printed — a subsequent restart will
  continue recovering from where this run left off, since the
  watermark still only advances as far as messages actually got
  processed.
- Each recovered message's processing failure is caught individually
  (logged to Errors as `"StartupRecovery"`) so one bad message doesn't
  abort recovery for the rest of the channel.

### FreeHub

Handled by the persisted seen-ID deque described in
[FreeHub Ingestion](#6-freehub-ingestion) and [State](#15-state) — a
restart resumes from the last persisted seen list rather than
resetting, and a poll that finds a full page of unseen projects walks
up to `_MAX_BACKFILL_PAGES` (10) additional pages to catch up on a gap
larger than one page.

---

## 17. Deployment

### Entry Point

Two entry points exist in the repository: `run.py` (no Notification
Guard) and `run_guarded.py` (installs the guard, still gated by
`NOTIFICATION_GUARD_ENABLED`). **Which one is actually deployed in
production could not be verified from the repository contents alone**
— this depends on the host's systemd unit file (or other process
supervisor), which was not included in the provided archive. Confirm
the actual `ExecStart` value on the deployment host rather than
assuming.

### systemd Unit File (example — adjust paths/user, and the script name per the note above)

```ini
[Unit]
Description=Loki Freelance Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_linux_username
WorkingDirectory=/path/to/freelance-assistant
ExecStart=/path/to/freelance-assistant/.venv/bin/python run.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/path/to/freelance-assistant/.env

[Install]
WantedBy=multi-user.target
```

No unit file was included in the provided archive, so the exact
service name, `ExecStart`, and working directory used in production
**cannot be verified from the repository** — the block above is an
illustrative example only, following the pattern implied by
`app/config.py`'s `load_dotenv()` + `EnvironmentFile` usage, not a
confirmed production configuration.

Notes on what *is* verifiable from source:
- `app/config.py` calls `load_dotenv()` itself, so `EnvironmentFile`
  is a belt-and-suspenders measure, not strictly required for the
  Python process itself to see the variables — but it does make them
  visible to systemd/the shell environment too.
- `Restart=on-failure` is a reasonable choice given the application
  has no internal reconnect logic beyond what Telethon/aiohttp already
  provide.

### First-Run Requirement

The manual, interactive Telethon login (see
[Installation](#2-installation)) must be completed **before** the
service is installed — systemd cannot answer the login prompt.

### Updating

```bash
cd /path/to/freelance-assistant
git pull

source .venv/bin/activate
pip install -r requirements.txt

sudo systemctl restart <service-name>   # replace with the actual unit name
```

---

## 18. Troubleshooting

### Missing Environment Variables

**Symptom:** `RuntimeError: Missing required environment variable:
<NAME>`.

**Cause:** `app/config.py` validates every required variable at
import time.

**Fix:** Check `.env` exists in the project root (not just
`.env.example`) and contains the named variable. A related error,
`"... must be an integer (got '<value>')"`, means `API_ID` or
`BOT_CHAT_ID` contains non-numeric text; the same message for
`TARGET_CHANNEL_IDS` means one comma-separated entry isn't a valid
integer.

**Note:** `GEMINI_API_KEYS` fails with a distinct message,
`"GEMINI_API_KEYS is required"`, raised directly in `config.py` if the
comma-separated list is empty after parsing — not the generic
`_require_env` message, since the raw variable being unset vs. being
set to an empty/comma-only string both need to fail here.

### Telethon Session Issues

**Symptom:** Loki prompts for a login code again despite having
logged in before.

**Cause:** The session (a SQLite file at the path derived from
`SESSION_NAME`) was deleted, moved, corrupted, or its login was
revoked from the Telegram app (**Settings → Devices**). Because
re-authentication requires interactive input, this will hang
indefinitely under systemd.

**Fix:** Stop the service, run the entry point manually to
re-authenticate, then restart the service.

### Gemini / Groq Failures

**Symptom:** A job that should have reached LLM review is instead
`Rejected` with reason `"LLM Error"`, and an `"LLM"` row appears in
the Errors sheet.

**Cause:** Every configured Gemini key failed (each retried once for
transient errors), **and** every Groq model in the rotation also
failed — see [LLM Subsystem](#11-llm-subsystem). `app.llm.manager`
only raises once both providers are exhausted.

**Fix:** Check the Errors sheet for the combined exception text (it
includes both the last Gemini error and the last Groq error). Common
causes: invalid/expired API key(s), no working keys at all in
`GEMINI_API_KEYS`, network issues from the host, or a provider-side
outage/rate limit affecting every key/model tried.

### Notification Guard Blocking Everything

**Symptom:** Direct-accept jobs stop generating Telegram
notifications after switching to `run_guarded.py`.

**Cause:** Either the guard is genuinely rejecting the jobs on
review (check the NotificationGuard sheet for `do_not_notify` rows
with the model's reasoning-free decision), or `GROQ_NOTIFICATION_GUARD_API_KEY`
is invalid/missing, which causes every evaluation to fail and the
guard's fail-closed behavior suppresses every notification (check for
`guard_decision="error"` rows).

**Fix:** Inspect the NotificationGuard sheet's `Guard Decision` and
`Error` columns for the affected jobs. Remember LLM-reviewed jobs
(`ai_used=True`) bypass the guard entirely — if *those* also stopped
notifying, the problem is elsewhere in the pipeline, not the guard.

### Notification Failures

**Symptom:** A job is `Accepted` but no Telegram message arrives.

**Cause:** `notifier.py`/`channel_notifier.py` catch
`bot.send_message()` exceptions locally, log `"Failed"` to
Notifications and an entry to Errors, and return `False` without
raising.

**Fix:** Check the Notifications and Errors sheets for the job's
UUID. Common causes: invalid/revoked `BOT_TOKEN`; the recipient never
started a conversation with the bot (a bot cannot message a user who
hasn't messaged it first); `BOT_CHANNEL_ID` set but the bot isn't an
administrator of that channel.

### Excel Logging Issues

**Symptom:** Loki fails to save `logs/freelance_bot_logs.xlsx`, or the
file appears locked.

**Cause:** `openpyxl` cannot write while the file is open elsewhere
(e.g. open in Excel/LibreOffice on the same machine).

**Fix:** Close the workbook in any spreadsheet application before
Loki needs to save. To inspect the log while Loki runs, copy the file
first rather than opening the original.

### FreeHub Backfill Cap Reached

**Symptom:** `[FREEHUB WARNING] <source>: hit the 10-page backfill
safety cap` in the console/journal output.

**Cause:** More new projects were posted since the last successful
poll than 10 pages (`FREEHUB_PAGE_SIZE` × 10) can cover — likely
extended downtime or a long stretch of failed polls.

**Fix:** No action strictly required — the next poll continues from
where this one left off, and the backfill logic will keep walking
forward. If this happens repeatedly, investigate why polls are
failing or being missed rather than increasing the cap blindly.

---

## 19. Testing / Development

The `tests/` directory contains the current test suite:

- **`tests/test_parser.py`** — runs `parse_job()` against a
  Nafezly-formatted Arabic sample message.
- **`tests/test_keyword_filter.py`** — runs `keyword_filter()` against
  a set of representative sample posts.
- **`tests/test_llm_gemini.py`** — exercises `app.llm.gemini.evaluate_job`
  (via a real `keyword_filter()` result, not a hand-built dict) —
  requires working Gemini credentials.
- **`tests/test_llm_groq.py`** — exercises `app.llm.groq.evaluate_job`
  directly — requires working Groq credentials.
- **`tests/test_llm_manager.py`** — exercises
  `app.llm.manager.evaluate_job` (the Gemini→Groq fallback path).
- **`tests/test_notification_guard.py`** — exercises
  `NotificationGuardIntegration` against a fake guard, verifying the
  once-per-job evaluation/caching behavior described in
  [Notification Guard](#12-notification-guard) without making any
  real API calls.
- **`tests/test_pipeline.py`** — an end-to-end smoke test using fake
  Telethon-shaped `Event`/`Chat`/`Message` objects (no real Telegram
  connection) run through the real `process_message()`, against a
  temporary, isolated workbook (not the production log file).

Tests import from `app.*` directly and several load `.env` via
`load_dotenv()`, so a valid `.env` is needed to run the ones that make
live LLM calls. A `tests/__init__.py` is present, and compiled
`pytest`-run artifacts exist under `tests/__pycache__/`, indicating
the suite is run with `pytest`; `pytest` itself is not listed in
`requirements.txt`, so install it separately (`pip install pytest`) if
it isn't already available in your environment.

```bash
pytest tests/
```

`replay_message.py` at the project root is a standalone script that
constructs a single fake Telegram event (Nafezly-style Arabic text)
and replays it through `process_message()` against the real
configured workbook — useful for manually re-testing a specific
sample post end-to-end without waiting for a live message.

### Contribution Notes

- **Configuration is centralized.** New settings should go through
  `app/config.py`'s validation helpers (or, for the Notification
  Guard, its own independent `app/notification_guard/config.py`
  pattern) rather than ad hoc `os.getenv()` calls elsewhere.
- **Logging is additive, not optional.** Any new pipeline stage should
  log its outcome through `ExcelLogger.run()`, following the existing
  pattern of catching exceptions locally and writing to the Errors
  sheet rather than letting them propagate.
- **Niche-specific content lives in `keywords.py` and `llm/prompt.py`**;
  the decision-table *mechanism* in `filters.py` and the
  Gemini/Groq/guard *routing* mechanism are intended to stay
  niche-agnostic.
- **The Notification Guard is additive by design.** Changes to it
  should preserve the property that `run.py` (guard never installed)
  and `run_guarded.py` with `NOTIFICATION_GUARD_ENABLED=false` behave
  identically.

---

## Notes on Verified vs. Unverifiable Details

- Every model name, environment variable, threshold constant, and
  worksheet name in this document was read directly from the source
  files listed alongside each claim above, not carried over from
  prior documentation or assumed.
- The **production deployment configuration** (which entry point is
  actually running, the real systemd unit name, working directory,
  and `ExecStart`) could not be verified — no systemd unit file or
  other deployment manifest was present in the provided archive. See
  [Deployment](#17-deployment).
- The FreeHub backend hostname is hardcoded in `app/freehub.py`; it is
  intentionally not reproduced in this document since it's
  infrastructure detail rather than something an operator configures.
