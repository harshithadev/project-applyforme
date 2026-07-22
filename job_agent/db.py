from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH, DOCS_DIR, GENERATED_DIR


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (DATA_DIR, DOCS_DIR, GENERATED_DIR):
        path.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              path TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'source',
              content TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              sha256 TEXT NOT NULL DEFAULT '',
              extractor TEXT NOT NULL DEFAULT '',
              ingest_status TEXT NOT NULL DEFAULT 'ready',
              ingest_error TEXT NOT NULL DEFAULT '',
              size_bytes INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_profiles (
              id INTEGER PRIMARY KEY CHECK(id = 1),
              profile_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              company TEXT NOT NULL,
              url TEXT NOT NULL UNIQUE,
              description TEXT NOT NULL,
              location TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'manual',
              status TEXT NOT NULL DEFAULT 'new',
              score INTEGER NOT NULL DEFAULT 0,
              posted_at TEXT,
              discovered_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'drafted',
              resume_tex_path TEXT NOT NULL DEFAULT '',
              resume_pdf_path TEXT NOT NULL DEFAULT '',
              cover_letter TEXT NOT NULL DEFAULT '',
              statements TEXT NOT NULL DEFAULT '[]',
              email_subject TEXT NOT NULL DEFAULT '',
              email_body TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS answer_rules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              question TEXT NOT NULL UNIQUE,
              answer TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              risky INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              confidence INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT NOT NULL DEFAULT 'info',
              message TEXT NOT NULL,
              meta TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            """
        )
        document_columns = {
            "sha256": "TEXT NOT NULL DEFAULT ''",
            "extractor": "TEXT NOT NULL DEFAULT ''",
            "ingest_status": "TEXT NOT NULL DEFAULT 'ready'",
            "ingest_error": "TEXT NOT NULL DEFAULT ''",
            "size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
        }
        existing_columns = {item["name"] for item in conn.execute("PRAGMA table_info(documents)").fetchall()}
        for name, definition in document_columns.items():
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
        defaults = {
            "mode": "review",
            "role_keywords": "software engineer, developer, full stack",
            "locations": "remote, new york, nyc",
            "posted_within_days": "14",
            "daily_application_limit": "10",
            "daily_email_limit": "15",
            "scan_interval_minutes": "0",
            "target_companies": "",
            "career_urls": "",
            "email_mode": "approval",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        found = conn.execute(query, params).fetchone()
        return dict(found) if found else None


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(query, params)
        return int(cur.lastrowid or 0)


def setting(key: str, default: str = "") -> str:
    found = row("SELECT value FROM settings WHERE key = ?", (key,))
    return str(found["value"]) if found else default


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def all_settings() -> dict[str, str]:
    return {item["key"]: item["value"] for item in rows("SELECT key, value FROM settings")}


def log(message: str, level: str = "info", meta: dict[str, Any] | None = None) -> None:
    execute(
        "INSERT INTO events(level, message, meta, created_at) VALUES(?, ?, ?, ?)",
        (level, message, json.dumps(meta or {}), now_iso()),
    )


def db_info() -> dict[str, str]:
    return {
        "database": str(DB_PATH),
        "docs": str(DOCS_DIR),
        "generated": str(GENERATED_DIR),
    }
