"""SQLite persistence for RPD counters, exhausted state, tool-call affinity and the turn log.

One connection, used only from the event-loop thread (one uvicorn worker,
DESIGN §7). Each write commits immediately.

Housekeeping (DESIGN §20.10): `PRAGMA user_version` holds the schema version, and
a damaged file or one from a newer version is a startup error (`StateDBError`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

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

# Migrations from version N to N+1: lists of single statements, run with execute() in one
# transaction at startup (executescript() would commit part-way). Version 0 is a Phase 1
# database or a new file; both get SCHEMA, which is and stays the version-1 schema. Later
# changes go only into MIGRATIONS (never into SCHEMA), so a new file runs them all too.
MIGRATIONS: dict[int, list[str]] = {
    # 1 -> 2: the decision log (DESIGN §16.8 1d). Affinity rows from version 1 keep a NULL
    # turn_id; a follow-up that finds only those starts a new turn row.
    1: [
        "ALTER TABLE affinity ADD COLUMN turn_id TEXT",
        """CREATE TABLE turns (
            turn_id       TEXT PRIMARY KEY,   -- 128 random bits as hex, never from content
            day           TEXT NOT NULL,      -- PT date of the turn start, YYYY-MM-DD
            hour          INTEGER NOT NULL,   -- PT hour of the turn start, 0-23
            route         TEXT NOT NULL,      -- requested route or model ID
            first_model   TEXT,               -- served the first request (NULL: none served)
            last_model    TEXT,               -- served the latest served request
            reason        TEXT NOT NULL,      -- first, spill, pinned, handoff, unknown
            tools         INTEGER NOT NULL,   -- tool definitions in the first request
            input_tokens  INTEGER NOT NULL,   -- sums of reported usage
            output_tokens INTEGER NOT NULL,
            steps         INTEGER NOT NULL,   -- served requests
            errors        INTEGER NOT NULL,   -- failed requests
            handoffs      INTEGER NOT NULL,   -- requests on which a handoff target was called
            last_status   INTEGER NOT NULL    -- HTTP status of the latest request
        )""",
        "CREATE INDEX turns_day ON turns (day)",
    ],
}

_CHECK_ACCESS = ("Check its permissions and free disk space, and that no other proxy is using it.")
_SAFE_TO_DELETE = ("Deleting it is safe: only today's local request counts (a hint; Google's "
                   "429s still enforce the limits), open tool-turn records and the turn log "
                   "are lost.")


class StateDBError(RuntimeError):
    """The state database can't be used. The message names the file and what to do."""


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the state database. ":memory:" is allowed for tests.

    `check_same_thread=False` lets the app open the file before the event loop starts;
    it is still used only from that loop's thread.
    """
    try:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # autocommit (explicit BEGIN where needed); a short lock wait, since a wait blocks the event loop
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=1.0,
                               check_same_thread=check_same_thread)
    except (OSError, sqlite3.Error) as e:
        raise StateDBError(f"cannot open state database {path}: {e}. " + _CHECK_ACCESS) from None
    try:
        _open(conn, path)
    except BaseException:
        conn.close()
        raise
    return conn


def _open(conn: sqlite3.Connection, path: str | Path) -> None:
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        problems = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
        if problems != ["ok"]:
            raise StateDBError(f"state database {path} is damaged ({'; '.join(problems[:3])}). "
                               + _SAFE_TO_DELETE)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StateDBError(f"state database {path} has schema version {version}, newer than "
                               f"this proxy understands ({SCHEMA_VERSION}). Run a newer proxy, "
                               "or delete the file. " + _SAFE_TO_DELETE)
        conn.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            conn.execute("BEGIN")
            try:
                for v in range(max(version, 1), SCHEMA_VERSION):
                    for statement in MIGRATIONS[v]:
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except sqlite3.OperationalError as e:  # read-only, locked, disk full: not damage
        raise StateDBError(f"cannot use state database {path}: {e}. " + _CHECK_ACCESS) from None
    except sqlite3.DatabaseError as e:  # SQLITE_CORRUPT / SQLITE_NOTADB
        raise StateDBError(f"state database {path} is damaged or not a database ({e}). "
                           + _SAFE_TO_DELETE) from None
