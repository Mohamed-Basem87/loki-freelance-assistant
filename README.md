# Loki Freelance Assistant

> **A bot that monitors freelance job sources in real time (Telegram
> channels and FreeHub), classifies each post against a personalized
> Data Analysis / Business Intelligence skill profile using a
> deterministic bilingual (English/Arabic) keyword engine, escalates
> borderline posts to Google Gemini (with a Groq fallback), logs every
> decision to a SQLite database, and notifies you on Telegram the
> moment something worth bidding on shows up.**

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

---

## Overview

Loki watches multiple freelance sources so you don't have to. It
listens to a fixed set of Telegram channels (as a logged-in user
account, via Telethon) and polls the FreeHub API, parses every new
post into a structured job, and runs it through a tiered keyword
classifier. Unambiguous posts are accepted or rejected immediately;
genuinely borderline posts are handed to Gemini (falling back to Groq
if every Gemini key fails) for a judgment call. An optional
Notification Guard can add a second, independent check before a
direct-accept job is actually sent. Every stage of the decision is
recorded in a SQLite database, and accepted jobs are pushed to
Telegram.

---

## Supported Sources

- **Telegram channels** — any channel/supergroup listed in
  `TARGET_CHANNEL_IDS`, including source-specific parsing for Nafezly
  posts and a generic fallback for everything else.
- **FreeHub** — polled periodically (`FREEHUB_POLL_INTERVAL`) across
  its `kafiil` and `freelancer` sources, with bounded multi-page
  backfill if a poll cycle misses more than one page's worth of new
  projects.

---

## Production Pipeline

```text
Telegram channels          FreeHub (polled)
        │                        │
        ▼                        ▼
   Parser (app/parser.py)   FreeHub worker
        │                        │
        └───────────┬────────────┘
                     ▼
         Deterministic keyword classifier
          (app/keywords.py + app/filters.py)
                     │
       ┌─────────────┼──────────────┐
       ▼             ▼              ▼
    Reject     Direct Accept    Needs Gemini
                     │              │
                     ▼              ▼
            Notification Guard   Gemini review
           (optional, disabled       │
              by default)      (Groq fallback
                     │           if Gemini fails)
                     │                │
                     │          Accept / Reject
                     │                │
                     └───────┬────────┘
                              ▼
              Telegram notification(s)
             (private chat + optional channel)
                              │
                              ▼
                    SQLite audit log
```

Note the two branches after the classifier are **not** symmetric: only
jobs the classifier accepted *directly* pass through the Notification
Guard. Jobs routed to Gemini/Groq already received an LLM review and
bypass the guard entirely — see
[Notification Guard](DOCUMENTATION.md#12-notification-guard) for why.

- **Deterministic classifier first.** Every job is evaluated by
  `app/filters.py` using the tiered keyword rules in `app/keywords.py`
  (core / supporting / hard-reject, English and Arabic). This is a
  rule-based decision table — hard-reject checks, title signals,
  mixed-signal detection, then core/supporting evidence — not a single
  additive score; there is no numeric threshold a job's "score" is
  compared against. Most posts are resolved here with no API call at
  all.
- **Gemini review for borderline posts**, with automatic fallback to
  Groq (rotating through several models) if every configured Gemini
  key fails.
- **Notification Guard** is an optional, independent Groq-based
  second opinion that only runs on jobs the classifier accepted
  *directly* (LLM-reviewed jobs already had their own review and skip
  the guard). It is fail-closed: any guard error blocks the
  notification rather than letting it through. Disabled by default.
- **Two notification targets**: a private chat (`BOT_CHAT_ID`) always
  receives full detail; an optional public channel (`BOT_CHANNEL_ID`)
  receives a simplified version if configured.
- **SQLite audit log** — every job, every Gemini call, every
  notification attempt, every error, and (if enabled) every
  Notification Guard decision is written to
  `loki_freelance_bot.db` (a SQLite database next to
  `docker-compose.yml`, bind-mounted read/write so it stays visible
  on the host).

---

## Tech Stack

| Component               | Technology            |
|--------------------------|------------------------|
| Telegram ingestion       | Telethon (user account) |
| Telegram notifications   | python-telegram-bot   |
| FreeHub ingestion        | aiohttp (polling)     |
| Primary LLM review       | Google Gemini (`google-genai`) |
| Fallback LLM review      | Groq                  |
| Audit logging            | SQLite (`.db`)        |
| Configuration            | python-dotenv         |
| Deployment               | systemd                |

---

## Installation

```bash
git clone <repository-url>
cd freelance-assistant

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in every value — see the
comments in `.env.example` for what each variable is for, in
particular:

- `GEMINI_API_KEYS` is a **comma-separated list** (not a single key).
- `NOTIFICATION_GUARD_*` variables are only needed if you enable the
  guard; its models are fixed in code, not configurable via `.env`.

---

## Running

Two entry points are provided:

```bash
python run.py            # standard pipeline
python run_guarded.py    # standard pipeline + Notification Guard installed
```

`run_guarded.py` installs the Notification Guard adapter (see
`app/notification_guard/`) before starting the same application code
`run.py` starts — no production module is modified to do this, the
guard wraps the notification calls at runtime. The guard still
respects `NOTIFICATION_GUARD_ENABLED`; running `run_guarded.py` with
that flag unset/false behaves identically to `run.py`.

The very first run requires an interactive Telethon login (a code
sent to your Telegram app, plus your 2FA password if enabled) — do
this manually before deploying under systemd, since a service can't
answer that prompt.

For production, deploy either entry point as a systemd service — see
`DOCUMENTATION.md` for a unit-file example. Which entry point to point
`ExecStart` at should reflect whichever deployment is actually
running; verify this on the host rather than assuming.

---

## Recovery

- **Telegram**: on startup, each monitored channel is walked forward
  from its last recorded message ID (up to a safety cap of 2000
  messages per channel) rather than a fixed recent window, so downtime
  doesn't silently skip a backlog. A channel with no prior state is
  seeded from its current newest message instead of backfilling.
- **FreeHub**: seen-project IDs are persisted to `database/state.json`
  and survive restarts; a poll that finds an entire page of unseen
  projects walks additional pages (bounded) to catch up.

---

## Configuration

All configuration is environment-variable based (`app/config.py`),
validated at startup — a missing or malformed required variable fails
immediately with a clear error rather than failing later mid-run. See
`.env.example` for the full list and `DOCUMENTATION.md` for detailed
behavior of each one.

---

## Logging

Every processed job is recorded in a SQLite database
(`loki_freelance_bot.db`, next to `docker-compose.yml`) in these
tables:

- **Jobs** — one row per job, with the full classifier evidence trail
  and final decision.
- **Gemini** — one row per Gemini review call.
- **Notifications** — one row per notification attempt (private and
  channel are logged separately).
- **Errors** — one row per caught exception anywhere in the pipeline.
- **NotificationGuard** — one row per guard evaluation (only created
  once the guard runs its first evaluation).

This is a diagnostic/audit trail for reviewing and tuning classifier
behavior over time — it does not modify the classifier automatically.

---

## License

This project is licensed under the MIT License.

---

## Author

**Mohamed Basem**

Faculty of Artificial Intelligence — Menoufia University

Focused on Data Analytics, Business Intelligence, Python automation,
and AI applications.
