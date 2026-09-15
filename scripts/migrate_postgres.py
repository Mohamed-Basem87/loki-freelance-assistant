"""Versioned on-boot-style migration runner for Postgres.

Replaces the ad-hoc "diff PRAGMA table_info every boot" approach used by
the SQLite DBLogger (app/services/logger.py). Migrations are plain .sql
files under migrations/postgres/, named "NNNN_description.sql" so
filename order is also apply order. Each file is applied at most once,
inside its own transaction, and recorded in schema_migrations so a
re-run is a no-op.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python scripts/migrate_postgres.py
"""
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"


def _pending_migrations(conn) -> list[Path]:
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    all_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in all_files if f.name not in applied]


def run(database_url: str) -> None:
    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        conn.commit()

        pending = _pending_migrations(conn)
        if not pending:
            print("[MIGRATE] up to date, nothing to apply.")
            return

        for path in pending:
            print(f"[MIGRATE] applying {path.name} ...")
            sql = path.read_text(encoding="utf-8")
            try:
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (path.name,),
                )
            except Exception:
                conn.rollback()
                print(f"[MIGRATE] FAILED on {path.name}, rolled back.", file=sys.stderr)
                raise
            else:
                conn.commit()
                print(f"[MIGRATE] applied {path.name}")


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Missing required environment variable: DATABASE_URL", file=sys.stderr)
        sys.exit(1)
    run(database_url)
