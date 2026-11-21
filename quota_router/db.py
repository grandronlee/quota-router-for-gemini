"""SQLite persistence for RPD counters, exhausted state and tool-call affinity.

One connection, used only from the event-loop thread (one uvicorn worker,
DESIGN §7). Each write commits immediately.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rpd (
    model_id TEXT NOT NULL,
    day      TEXT NOT NULL,          -- PT calendar date, YYYY-MM-DD
    count    INTEGER NOT NULL,
    PRIMARY KEY (model_id, day)
);
CREATE TABLE IF NOT EXISTS exhausted (
    model_id TEXT PRIMARY KEY,
    until    REAL NOT NULL           -- epoch seconds
);
CREATE TABLE IF NOT EXISTS affinity (
    tool_call_id TEXT PRIMARY KEY,
    model_id     TEXT NOT NULL,
    created_at   REAL NOT NULL       -- epoch seconds
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the state database. ":memory:" is allowed for tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # autocommit (explicit BEGIN where needed); a short lock wait, since a wait blocks the event loop
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=1.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
