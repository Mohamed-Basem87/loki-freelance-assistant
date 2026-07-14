# Loki Freelance Assistant

An AI-assisted freelance job monitoring bot that listens to Telegram freelance channels, filters opportunities using a weighted keyword engine, uses Google Gemini for ambiguous jobs, logs every decision, and sends rich Telegram notifications.

## Features

- Telegram channel monitoring (Telethon)
- Structured job parsing
- Weighted keyword scoring
- Hard reject / soft negative filtering
- Google Gemini evaluation
- Excel logging
- Rich Telegram notifications
- Linux systemd deployment
- Automatic restart on failure

## Tech Stack

- Python
- Telethon
- Google Gemini API
- python-telegram-bot
- OpenPyXL
- systemd

## Project Structure

app/
- parser.py
- filters.py
- gemini.py
- notifier.py
- logger.py
- message_processor.py

## Configuration

Create a `.env` file containing:

- API_ID
- API_HASH
- PHONE_NUMBER
- GEMINI_API_KEY
- BOT_TOKEN
- BOT_CHAT_ID

## Run

python run.py

## Linux Service

The project includes deployment as a `systemd` service for 24/7 operation.
