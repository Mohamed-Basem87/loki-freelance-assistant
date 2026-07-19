# Loki Freelance Assistant

> **An AI-assisted Telegram bot that monitors freelance job channels in
> real time, scores opportunities against a personalized skill profile,
> uses Google Gemini to review borderline jobs, logs every decision, and
> instantly notifies you of projects worth bidding on.**

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

------------------------------------------------------------------------

## Overview

Loki was built to solve a simple problem: **valuable freelance projects
disappear quickly**. Constantly checking multiple Telegram channels
wastes time and makes it easy to miss opportunities.

Instead of manually monitoring channels, Loki continuously watches them,
filters irrelevant jobs using a weighted keyword engine, asks Google
Gemini to evaluate only ambiguous posts, and immediately delivers
high-quality opportunities through Telegram notifications.

The result is a lightweight assistant that runs unattended 24/7 while
dramatically reducing noise.

------------------------------------------------------------------------

# Features

-   Real-time Telegram monitoring using **Telethon**
-   Structured parsing for Nafezly and generic Telegram posts
-   Weighted keyword scoring engine
-   Arabic text normalization
-   Google Gemini review for borderline jobs
-   Rich Telegram notifications with project links
-   Excel audit logging
-   Linux systemd deployment
-   Modular architecture
-   Startup recovery of missed Telegram messages after downtime

------------------------------------------------------------------------

# Decision Pipeline

``` text
Bot Starts
        │
        ▼
Recover Latest Messages
 (up to 40 per channel)
        │
        ▼
Listen for New Messages
        │
        ▼
   Parse Message
        │
        ▼
 Keyword Filter
        │
 ┌──────┼───────────┐
 │      │           │
Reject Accept    Gemini Review
 │      │           │
 └──────┴───────────┘
        │
        ▼
 Excel Log + Telegram Notification
```

------------------------------------------------------------------------

# Tech Stack

  Component           Technology
  ------------------- ---------------------
  Telegram Listener   Telethon
  Notifications       python-telegram-bot
  AI                  Google Gemini
  Logging             OpenPyXL
  Configuration       python-dotenv
  Deployment          systemd

------------------------------------------------------------------------

# Project Structure

``` text
loki-freelance-assistant/
│
├── app/
│   ├── handlers/
│   ├── state.py
│   ├── parser.py
│   ├── filters.py
│   ├── gemini.py
│   ├── notifier.py
│   ├── logger.py
│   ├── message_processor.py
│   └── ...
│
├── database/
│   └── state.json
│
├── run.py
├── requirements.txt
├── .env.example
└── README.md
```

------------------------------------------------------------------------

# Installation

``` bash
git clone https://github.com/Mohamed-Basem87/loki-freelance-assistant.git
cd loki-freelance-assistant

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

------------------------------------------------------------------------

# Configuration

Copy `.env.example` to `.env` and fill in:

``` env
API_ID=
API_HASH=
PHONE_NUMBER=

BOT_TOKEN=
BOT_CHAT_ID=

GEMINI_API_KEY=
```

------------------------------------------------------------------------

# Running

``` bash
python run.py
```

For production, configure the project as a **systemd** service.

------------------------------------------------------------------------

# Startup Recovery

Loki automatically recovers recent freelance opportunities after an unexpected shutdown, crash, or reboot.

On startup it:
- Loads the last processed message ID for every monitored channel.
- Fetches only the latest **40** messages per channel.
- Processes only unseen messages.
- Updates its persistent state after every processed message.
- Starts live monitoring after recovery completes.

------------------------------------------------------------------------

# Logging

Every processed job is stored in an Excel workbook with four worksheets:

-   Jobs
-   Gemini
-   Notifications
-   Errors

This creates a complete audit trail that can be used to tune keyword
weights over time.

------------------------------------------------------------------------

# Design Decisions

### Weighted keyword engine first

Most freelance posts can be classified without AI. Using a deterministic
scoring engine keeps the bot fast and inexpensive.

### Gemini only for borderline jobs

Only uncertain posts are sent to Gemini, significantly reducing API
usage while improving decision quality.

### Excel instead of a database

For a personal assistant, Excel provides an easy-to-inspect audit log
without introducing database infrastructure.

------------------------------------------------------------------------

# Future Improvements

-   SQLite logging backend
-   Web dashboard
-   Multiple user profiles
-   Configurable monitored channels
-   Docker deployment
-   Analytics dashboard

------------------------------------------------------------------------

# License

This project is licensed under the MIT License.

------------------------------------------------------------------------

# Author

**Mohamed Basem**

Faculty of Artificial Intelligence --- Menoufia University

Focused on Data Analytics, Business Intelligence, Python automation, and
AI applications.

