"""Decision log: one metadata-only row per turn (DESIGN §16.8 1d, §16.5).

A turn starts with a request whose active turn holds no tool calls. Later requests find
their row through the `turn_id` stored with their tool calls' affinity records. Every
request writes its row with one upsert, before its tool calls' affinity is recorded, so a
follow-up's write always comes after the write of the request whose calls it names. The
columns that describe the turn's start are set only when the row is created, the counters
only add, and `last_model` and `last_status` come from the latest write. A request whose
row is missing (its start's write failed, or its calls predate schema version 2) creates it.

Nothing here holds prompt or completion text, tool names, tool-call IDs or exact times.
"""

from __future__ import annotations

import datetime as dt
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from .clock import PT, Clock

TURN_RETENTION_DAYS = 30

TurnReason = Literal["first", "spill", "pinned", "handoff", "unknown"]


def new_turn_id() -> str:
    """128 random bits as hex, never derived from content."""
    return secrets.token_hex(16)


@dataclass(frozen=True)
class TurnRef:
    """The turn a request belongs to. `reason`, `tools` and `started_at` (its PT date and
    hour) are stored only if this request creates the row."""
    turn_id: str
    reason: TurnReason
    tools: int
    started_at: float


def _pt(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, PT)


class TurnLog:
    def __init__(self, conn: sqlite3.Connection, clock: Clock,
                 retention_days: int = TURN_RETENTION_DAYS) -> None:
        self._conn = conn
        self._clock = clock
        self._retention_days = retention_days
        self.prune()

    def prune(self) -> None:
        """Delete rows whose turn started more than `retention_days` PT days ago."""
        cutoff = (_pt(self._clock.now()).date() - dt.timedelta(days=self._retention_days)).isoformat()
        self._conn.execute("DELETE FROM turns WHERE day < ?", (cutoff,))

    def note(self, turn: TurnRef, *, route: str, status: int, served_model: str | None,
             prompt_tokens: int | None = None, output_tokens: int | None = None,
             handoff: bool = False) -> None:
        """One request of `turn`, written before its tool calls' affinity is recorded (a stream:
        when it settles). `served_model` is set only when a model
        served it (a step); otherwise it counts as an error. Raises sqlite3.Error."""
        start = _pt(turn.started_at)
        step = served_model is not None
        self._conn.execute(
            "INSERT INTO turns (turn_id, day, hour, route, first_model, last_model, reason, tools, "
            "input_tokens, output_tokens, steps, errors, handoffs, last_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (turn_id) DO UPDATE SET "
            "first_model = COALESCE(turns.first_model, excluded.first_model), "
            "last_model = COALESCE(excluded.last_model, turns.last_model), "
            "input_tokens = turns.input_tokens + excluded.input_tokens, "
            "output_tokens = turns.output_tokens + excluded.output_tokens, "
            "steps = turns.steps + excluded.steps, "
            "errors = turns.errors + excluded.errors, "
            "handoffs = turns.handoffs + excluded.handoffs, "
            "last_status = excluded.last_status",
            (turn.turn_id, start.date().isoformat(), start.hour, route, served_model, served_model,
             turn.reason, turn.tools, prompt_tokens or 0, output_tokens or 0,
             int(step), int(not step), int(handoff), status))

    def today_by_route(self) -> dict[str, int]:
        """Turns started today (PT) per route, for /status."""
        day = _pt(self._clock.now()).date().isoformat()
        rows = self._conn.execute("SELECT route, COUNT(*) FROM turns WHERE day = ? GROUP BY route "
                                  "ORDER BY route", (day,))
        return dict(rows.fetchall())
