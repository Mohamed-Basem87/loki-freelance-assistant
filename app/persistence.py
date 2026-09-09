"""Persistence composition boundary. Default remains SQLite/DBLogger."""
from app.adapters.repositories.registry import build

db = build()
