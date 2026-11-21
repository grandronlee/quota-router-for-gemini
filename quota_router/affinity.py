"""Tool-call affinity: tool_call_id → model that made the call (DESIGN §5.1).

Kept in SQLite for 24 hours, so a restart in the middle of a tool turn keeps the
turn on its model. Each record also names the proxy's turn (DESIGN §16.8 1d), so a
follow-up request finds its turn-log row through its tool-call IDs.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, NamedTuple

from .clock import Clock

AFFINITY_TTL_S = 24 * 3600.0


class AffinityRecord(NamedTuple):
    model_id: str
    turn_id: str | None  # None for records written before schema version 2


class AffinityStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, ttl_s: float = AFFINITY_TTL_S) -> None:
        self._conn = conn
        self._clock = clock
        self._ttl = ttl_s
        self.prune()

    def prune(self) -> None:
        self._conn.execute("DELETE FROM affinity WHERE created_at <= ?", (self._clock.now() - self._ttl,))

    def record(self, tool_call_ids: Iterable[str], model_id: str, turn_id: str | None = None) -> int:
        """Store each ID → model (and turn). Returns how many IDs already had a live record: affinity
        assumes IDs never repeat across responses, so a non-zero count is worth a warning."""
        ids = [i for i in dict.fromkeys(tool_call_ids) if isinstance(i, str) and i]
        if not ids:
            return 0
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
                "INSERT INTO affinity (tool_call_id, model_id, created_at, turn_id) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (tool_call_id) DO UPDATE SET model_id = excluded.model_id, "
                "created_at = excluded.created_at, turn_id = excluded.turn_id",
                [(i, model_id, now, turn_id) for i in ids])
            self._conn.execute("DELETE FROM affinity WHERE created_at <= ?", (now - self._ttl,))
        return reused

    def lookup(self, tool_call_ids: Iterable[str]) -> dict[str, str]:
        """Map each known, unexpired ID to its model ID. Unknown IDs are left out."""
        return {k: r.model_id for k, r in self.lookup_records(tool_call_ids).items()}

    def lookup_records(self, tool_call_ids: Iterable[str]) -> dict[str, AffinityRecord]:
        """Map each known, unexpired ID to its model and turn. Unknown IDs are left out."""
        ids = list(dict.fromkeys(i for i in tool_call_ids if isinstance(i, str) and i))
        if not ids:
            return {}
        cutoff = self._clock.now() - self._ttl
        out: dict[str, AffinityRecord] = {}
        for start in range(0, len(ids), 500):  # stay under SQLite's parameter limit
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT tool_call_id, model_id, turn_id FROM affinity "
                f"WHERE tool_call_id IN ({marks}) AND created_at > ?", (*chunk, cutoff))
            out.update({i: AffinityRecord(m, t) for i, m, t in rows.fetchall()})
        return out
