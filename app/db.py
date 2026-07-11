"""SQLite access and plain-SQL migrations.

Migrations are numbered files in migrations/ (001_foo.sql, 002_bar.sql, …),
applied in order exactly once, tracked in schema_migrations. No down
migrations — this is a forward-only POC database.
"""

import sqlite3
from pathlib import Path

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate() -> list[str]:
    """Apply pending migrations; returns the filenames applied."""
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   filename TEXT NOT NULL,
                   applied_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        applied = {row["version"] for row in conn.execute(
            "SELECT version FROM schema_migrations")}
        done = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            conn.executescript(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (?, ?)",
                (version, path.name),
            )
            conn.commit()
            done.append(path.name)
        return done
    finally:
        conn.close()
