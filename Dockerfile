# syntax=docker/dockerfile:1
#
# Loki Freelance Assistant
#
# Two runtime modes (override CMD to switch):
#   python run.py            standard pipeline (default)
#   python run_guarded.py    standard pipeline + Notification Guard

FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .

# Install into a separate prefix so the runtime stage can copy exactly
# what pip resolved, leaving no build toolchain in the final image.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="Loki Freelance Assistant"
LABEL org.opencontainers.image.description="Freelance job monitor: Telegram + FreeHub ingestion, keyword classifier, Gemini/Groq review, SQLite audit log, Telegram notifications."
LABEL org.opencontainers.image.source="https://github.com/Mohamed-Basem87/loki-freelance-assistant"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . /app

# Runtime data dirs: Telethon session, SQLite audit log, dedup state.
# The audit-log DB file itself is bind-mounted from the host (see
# docker-compose.yml) and is never baked into the image.
RUN mkdir -p /app/sessions /app/database \
    && useradd --create-home --uid 1000 loki \
    && chown -R loki:loki /app

USER loki

# Mount volumes (named volumes recommended) so the session and state
# survive container restarts / rebuilds.
VOLUME ["/app/sessions", "/app/database"]

CMD ["python", "run_guarded.py"]
