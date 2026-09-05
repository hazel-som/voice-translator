"""Conversation log: every finished translation goes into a local SQLite file (stdlib only).

The browser makes a fresh session id each time the page loads and sends it with every
translation, so one page visit == one session. Only text is stored: no audio, no client info.

    sessions(id, started_at)
    turns(id, session_id, created_at, source, target, source_text, translated_text)

Look at it later with:  sqlite3 conversations.db 'SELECT * FROM turns ORDER BY id'
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    created_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    target          TEXT NOT NULL,
    source_text     TEXT NOT NULL,
    translated_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
"""


def is_valid_session_id(value) -> bool:
    return isinstance(value, str) and SESSION_ID_PATTERN.match(value) is not None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ConversationStore:
    """Thread-safe append-only log; the HTTP server calls save_turn from several threads."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._db:
            self._db.executescript(SCHEMA)

    def save_turn(self, session_id: str, source: str, target: str,
                  source_text: str, translated_text: str) -> int:
        """Insert one translated sentence, creating the session row on first sight. Returns the turn id."""
        now = _now()
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO sessions(id, started_at) VALUES (?, ?)", (session_id, now))
            cur = self._db.execute(
                "INSERT INTO turns(session_id, created_at, source, target, source_text, translated_text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, now, source, target, source_text, translated_text))
            return int(cur.lastrowid)

    def close(self) -> None:
        with self._lock:
            self._db.close()
