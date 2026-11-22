"""Tool-call affinity: tool_call_id → model that made the call (DESIGN §5.1).

Kept in SQLite for 24 hours, so a restart in the middle of a tool turn keeps the
turn on its model. Each record also names the proxy's turn (DESIGN §16.8 1d), so a
follow-up request finds its turn-log row through its tool-call IDs, and holds the
call's thought-signature state (DESIGN §23), so a signature the client drops can be
restored for the model that made the call.

A signature is content-derived (DESIGN §23.3): no value read or written here ever goes
into a log line, an exception message or a return value other than the record itself.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, Mapping, NamedTuple

from .clock import Clock

AFFINITY_TTL_S = 24 * 3600.0
MAX_SIGNATURE_CHARS = 16_384  # per signature (DESIGN §23.2); Phase 0 and 1a saw 196 to 1,036
MAX_SIGNATURE_TOTAL = 16 * 1024 * 1024  # all live signatures together
_SIGNATURE = re.compile(r"[A-Za-z0-9+/=_-]+")  # base64 and URL-safe base64: characters = bytes


class AffinityRecord(NamedTuple):
    model_id: str
    turn_id: str | None  # None for records written before schema version 2
    # The call's signature state (DESIGN §23.1): the signature; "" when the response carried
    # none for this call; None when not known (not seen yet, not storable, cleared, or a
    # record written before schema version 3).
    signature: str | None = None


class Recorded(NamedTuple):
    reused: int  # IDs that already had a live record (DESIGN §20.6)
    evicted: int  # signatures cleared to keep the total under MAX_SIGNATURE_TOTAL
    # The records' created_at: with the turn ID, the write generation a stream's settling
    # write is guarded on, so it can't land on a record a later response wrote (DESIGN §23.5).
    at: float | None = None


def signature_state(value: Any) -> tuple[str | None, str]:
    """A response's `extra_content.google.thought_signature` value as (state to store,
    capture action), DESIGN §23.1-23.2. Anything but a non-empty string means no signature;
    a string over the limit or outside the alphabet is stored as unknown, never cut short."""
    if not isinstance(value, str) or not value:
        return "", "none"
    if len(value) > MAX_SIGNATURE_CHARS:
        return None, "too_large"
    if not _SIGNATURE.fullmatch(value):
        return None, "invalid"
    return value, "stored"


def _storable(value: Any) -> str | None:
    """The state to write for a value a caller passes: "" and valid signatures as they are,
    anything else unknown. Keeps the per-signature limit whatever the caller checked."""
    if value == "":
        return ""
    state, action = signature_state(value)
    return state if action == "stored" else None


class AffinityStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, ttl_s: float = AFFINITY_TTL_S,
                 max_signature_total: int = MAX_SIGNATURE_TOTAL) -> None:
        self._conn = conn
        self._clock = clock
        self._ttl = ttl_s
        self._max_total = max_signature_total
        self.prune()

    def prune(self) -> None:
        # A call's signature goes with its row (DESIGN §23.2).
        self._conn.execute("DELETE FROM affinity WHERE created_at <= ?", (self._clock.now() - self._ttl,))

    def record(self, tool_call_ids: Iterable[str], model_id: str, turn_id: str | None = None,
               signatures: Mapping[str, str | None] | None = None) -> Recorded:
        """Store each ID → model, turn and signature state (an ID missing from `signatures`
        is unknown, NULL). An ID seen before is overwritten whole, signature included, so a
        stale signature never sits on a record that names another model (DESIGN §23.5).

        `reused` counts IDs that already had a live record: affinity assumes IDs never repeat
        across responses, so a non-zero count is worth a warning."""
        ids = [i for i in dict.fromkeys(tool_call_ids) if isinstance(i, str) and i]
        if not ids:
            return Recorded(0, 0, None)
        sigs = signatures or {}
        now = self._clock.now()
        with self._conn:  # one transaction: all of a response's calls or none
            self._conn.execute("BEGIN")
            reused = 0
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                reused += self._conn.execute(
                    f"SELECT COUNT(*) FROM affinity WHERE tool_call_id IN ({marks}) AND created_at > ?",
                    (*chunk, now - self._ttl)).fetchone()[0]
            self._conn.executemany(
                "INSERT INTO affinity (tool_call_id, model_id, created_at, turn_id, signature) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (tool_call_id) DO UPDATE SET model_id = excluded.model_id, "
                "created_at = excluded.created_at, turn_id = excluded.turn_id, "
                "signature = excluded.signature",
                [(i, model_id, now, turn_id, _storable(sigs.get(i))) for i in ids])
            self._conn.execute("DELETE FROM affinity WHERE created_at <= ?", (now - self._ttl,))
            evicted = self._enforce_cap()
        return Recorded(reused, evicted, now)

    def settle_signatures(self, model_id: str, turn_id: str | None,
                          updates: Iterable[tuple[str, str | None, str | None, float]]
                          ) -> tuple[list[str | None], int]:
        """A stream's settling write (DESIGN §23.1): each (ID, new state, state it was recorded
        with, created_at it was recorded with) is applied only if the record is still the one this stream wrote (same model,
        turn and created_at, the write generation) and still holds the recorded state, so it
        never lands on a record a later response wrote, even one that reused the ID on the same
        model. Returns the new states actually written, and the signatures evicted to keep the
        total cap."""
        rows = [(i, _storable(new) if new is not None else None, old, at) for i, new, old, at in updates]
        if not rows:
            return [], 0
        written: list[str | None] = []
        with self._conn:
            self._conn.execute("BEGIN")
            for i, new, old, at in rows:
                guard = "signature IS NULL" if old is None else "signature = ?"
                cur = self._conn.execute(
                    "UPDATE affinity SET signature = ? WHERE tool_call_id = ? AND model_id = ? "
                    f"AND created_at = ? AND turn_id IS ? AND {guard}",
                    (new, i, model_id, at, turn_id, *(() if old is None else (old,))))
                if cur.rowcount:
                    written.append(new)
            evicted = self._enforce_cap() if any(written) else 0
        return written, evicted

    def _enforce_cap(self) -> int:
        """Inside a write's transaction: clear the oldest signatures until all live ones fit
        in the total cap. The rows stay, so pinning still works (DESIGN §23.2)."""
        total = self._conn.execute("SELECT COALESCE(SUM(length(signature)), 0) FROM affinity").fetchone()[0]
        if total <= self._max_total:
            return 0
        over = total - self._max_total
        clear: list[tuple[int]] = []
        freed = 0
        for rowid, n in self._conn.execute(
                "SELECT rowid, length(signature) FROM affinity WHERE length(signature) > 0 "
                "ORDER BY created_at, rowid").fetchall():
            clear.append((rowid,))
            freed += n
            if freed >= over:
                break
        self._conn.executemany("UPDATE affinity SET signature = NULL WHERE rowid = ?", clear)
        return len(clear)

    def lookup(self, tool_call_ids: Iterable[str]) -> dict[str, str]:
        """Map each known, unexpired ID to its model ID. Unknown IDs are left out."""
        return {k: r.model_id for k, r in self.lookup_records(tool_call_ids).items()}

    def lookup_records(self, tool_call_ids: Iterable[str]) -> dict[str, AffinityRecord]:
        """Map each known, unexpired ID to its model, turn and signature state. Unknown IDs
        are left out."""
        ids = list(dict.fromkeys(i for i in tool_call_ids if isinstance(i, str) and i))
        if not ids:
            return {}
        cutoff = self._clock.now() - self._ttl
        out: dict[str, AffinityRecord] = {}
        for start in range(0, len(ids), 500):  # stay under SQLite's parameter limit
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT tool_call_id, model_id, turn_id, signature FROM affinity "
                f"WHERE tool_call_id IN ({marks}) AND created_at > ?", (*chunk, cutoff))
            out.update({i: AffinityRecord(m, t, s) for i, m, t, s in rows.fetchall()})
        return out
