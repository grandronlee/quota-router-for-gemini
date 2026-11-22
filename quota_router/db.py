"""SQLite persistence for RPD counters, exhausted state, tool-call affinity and the turn log.

One connection, used only from the event-loop thread (one uvicorn worker,
DESIGN §7). Each write commits immediately.

Housekeeping (DESIGN §20.10): `PRAGMA user_version` holds the schema version, and
a damaged file or one from a newer version is a startup error (`StateDBError`).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("quota_router")

SCHEMA_VERSION = 4

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
    # 2 -> 3: each tool call's thought-signature state (DESIGN §23.2). Rows from version 2
    # keep NULL (not known), so their calls get the placeholder, as in a handoff before.
    2: [
        "ALTER TABLE affinity ADD COLUMN signature TEXT",
    ],
    # 3 -> 4: the preferred tier of a gemini-auto turn (DESIGN §16.9.5). NULL on other routes
    # and on rows from version 3.
    3: [
        "ALTER TABLE turns ADD COLUMN tier TEXT",
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
    on_disk = str(path) != ":memory:"
    try:
        if on_disk:
            folder = Path(path).parent
            created = not folder.exists()
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            _folder_owner_only(folder, created)
            # Created owner-only, so its content is never readable by others even briefly.
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            for f in _files(path):
                _file_owner_only(f)
        # autocommit (explicit BEGIN where needed); a short lock wait, since a wait blocks the event loop
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=1.0,
                               check_same_thread=check_same_thread)
    except StateDBError:
        raise
    except (OSError, sqlite3.Error) as e:
        raise StateDBError(f"cannot open state database {path}: {e}. " + _CHECK_ACCESS) from None
    try:
        _open(conn, path)
        if on_disk:  # SQLite gives new -wal and -shm files the database's mode; check anyway
            for f in _files(path):
                _file_owner_only(f)
    except BaseException:
        conn.close()
        raise
    return conn


# The database holds content-derived data (thought signatures, DESIGN §23.3, §23.8 amendment 3).
DEFAULT_STATE_DIR = "data"


def _files(path: str | Path) -> list[Path]:
    """The database file and whichever of its -wal and -shm files exist."""
    main = Path(path)
    return [main] + [p for p in (Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()]


def _file_owner_only(path: Path) -> None:
    """Make the file 0600 whatever the umask, or refuse to start: signatures must not be kept
    in a file others can read."""
    try:
        os.chmod(path, 0o600)
        mode = os.stat(path).st_mode & 0o777
    except OSError as e:
        raise StateDBError(f"cannot make state database file {path} owner-only (0600): "
                           f"{e.strerror}. " + _CHECK_ACCESS) from None
    if mode & 0o077:  # a filesystem that ignores chmod
        raise StateDBError(f"state database file {path} stays readable by others (mode "
                           f"{oct(mode)}) after chmod 0600; use a filesystem that keeps Unix "
                           "permissions (DESIGN §20.10).")


def _folder_owner_only(folder: Path, created: bool) -> None:
    """Tighten the folder to 0700 only if it is the default data/ or connect() created it: a
    custom db_path may sit in the working directory or a shared one. Elsewhere, warn if
    others can read it; the 0600 file already protects the content."""
    try:
        if created or folder.resolve() == Path(DEFAULT_STATE_DIR).resolve():
            os.chmod(folder, 0o700)
        elif os.stat(folder).st_mode & 0o044:
            log.warning("the state database's folder %s is readable by others; the database "
                        "file itself is owner-only", folder)
    except OSError as e:
        log.warning("could not make %s owner-only: %s", folder, e.strerror)


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
