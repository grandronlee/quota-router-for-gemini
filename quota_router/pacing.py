"""Pacing the smart tier for gemini-auto (DESIGN §16.9.2, §16.9.3).

At a turn start the router asks whether the paced tier has more quota left than the rest of
the PT day's demand is expected to use:

    upgrade ⇔ left > (capacity + reserve) × (1 − demand_share(t))

`demand_share(t)` is the share of a typical PT day's turn starts that come before `t`. It is
measured from the decision log once 7 PT days in the last 14 (not counting today) have turn
starts, and otherwise comes from `working_hours`, or a flat day. Only dates, hours and counts
are read, never content (DESIGN §16.5). Nothing here sends a request.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from .clock import PT, next_pt_midnight, pt_date, pt_day_start
from .config import AutoConfig, Config, parse_working_hours
from .quota import QuotaStore
from .turns import FOLLOW_UP_REASONS

log = logging.getLogger("quota_router")

MEASURED_MIN_DAYS = 7
MEASURED_WINDOW_DAYS = 14

Source = Literal["measured", "working_hours", "flat"]


@dataclass(frozen=True)
class PaceView:
    """One reading of the pace (DESIGN §16.9.5)."""
    tier: str
    capacity: int
    left: int
    share: float
    source: Source
    upgrade_above: float  # an upgrade needs `left` above this

    @property
    def upgrade(self) -> bool:
        return self.left > self.upgrade_above

    def status(self) -> dict:
        return {"tier": self.tier, "capacity": self.capacity, "left": self.left,
                "share": round(self.share, 4), "source": self.source,
                "upgrade_above": round(self.upgrade_above, 2), "upgrade_now": self.upgrade}


class DemandCurve:
    """`share(now)`, from the first source that applies (DESIGN §16.9.3). The measured counts
    are read at most once per PT hour; a failed read keeps the last counts (or the cold-start
    source if there are none yet), and is reported through `on_error`. Each source's share
    depends only on `now` (and the counts), never on what it gave before, so it is the same after
    a restart; each one is nondecreasing within a PT day, the repeated fall-back hour included."""

    def __init__(self, conn: sqlite3.Connection, auto: AutoConfig,
                 on_error: Callable[[], None] | None = None) -> None:
        self._conn = conn
        self._on_error = on_error
        p = auto.pacing
        self._hours = parse_working_hours(p.working_hours) if p.working_hours else None
        self._tz = ZoneInfo(p.timezone) if p.timezone else None
        self._counts: list[int] | None = None  # 24 pooled counts by PT hour, once measured
        self._read_at: tuple[str, int] | None = None  # (PT date, hour) of the last read
        self._working: tuple[str, list[tuple[float, float]]] | None = None  # (PT date, intervals)

    def share(self, now: float) -> tuple[float, Source]:
        self._refresh(now)
        source: Source
        if self._counts is not None:
            value, source = self._measured(now), "measured"
        else:
            value = self._working_share(now) if self._hours is not None else None
            if value is not None:
                source = "working_hours"
            else:
                value, source = self._flat(now), "flat"
        return min(1.0, max(0.0, value)), source

    # ---- measured ----------------------------------------------------------

    def _refresh(self, now: float) -> None:
        local = dt.datetime.fromtimestamp(now, PT)
        key = (local.date().isoformat(), local.hour)
        if key == self._read_at:
            return
        self._read_at = key  # a failed read is retried next hour, not at every turn start
        today = local.date()
        oldest = (today - dt.timedelta(days=MEASURED_WINDOW_DAYS)).isoformat()
        excluded = ", ".join("?" for _ in FOLLOW_UP_REASONS)
        try:
            rows = self._conn.execute(
                "SELECT day, hour, COUNT(*) FROM turns WHERE day >= ? AND day < ? AND steps > 0 "
                f"AND reason NOT IN ({excluded}) GROUP BY day, hour",
                (oldest, today.isoformat(), *FOLLOW_UP_REASONS)).fetchall()
        except sqlite3.Error:
            log.exception("could not read the demand curve; keeping the last one")
            if self._on_error is not None:
                self._on_error()
            return
        days = {r[0] for r in rows}
        if len(days) < MEASURED_MIN_DAYS:
            self._counts = None
            return
        counts = [0] * 24
        for _, hour, n in rows:
            if isinstance(hour, int) and 0 <= hour < 24:
                counts[hour] += n
        self._counts = counts if sum(counts) > 0 else None

    def _measured(self, now: float) -> float:
        assert self._counts is not None
        local = dt.datetime.fromtimestamp(now, PT)
        # The repeated hour on the fall-back day (fold=1, the second 01:xx) stays at the end of
        # that hour, so the share never goes back with the wall clock, even after a restart.
        frac = 1.0 if local.fold else (local.minute * 60 + local.second + local.microsecond / 1e6) / 3600
        before = sum(self._counts[:local.hour]) + frac * self._counts[local.hour]
        return before / sum(self._counts)

    # ---- cold start ----------------------------------------------------------

    def _working_share(self, now: float) -> float | None:
        """Even demand inside the working hours, mapped onto today's PT day. None if no
        working time falls inside it (possible only for very short hours across a DST change)."""
        day_start, day_end = pt_day_start(now), next_pt_midnight(now)
        day = pt_date(now)
        if self._working is None or self._working[0] != day:
            self._working = (day, self._intervals(day_start, day_end))
        intervals = self._working[1]
        total = sum(b - a for a, b in intervals)
        if total <= 0:
            return None
        done = sum(max(0.0, min(b, now) - a) for a, b in intervals)
        return done / total

    def _intervals(self, day_start: float, day_end: float) -> list[tuple[float, float]]:
        """The working intervals, as epoch seconds, that overlap [day_start, day_end)."""
        assert self._hours is not None and self._tz is not None
        start_min, end_min = self._hours
        first = dt.datetime.fromtimestamp(day_start, self._tz).date() - dt.timedelta(days=1)
        last = dt.datetime.fromtimestamp(day_end, self._tz).date() + dt.timedelta(days=1)
        out = []
        d = first
        while d <= last:
            end_day = d + dt.timedelta(days=1) if end_min <= start_min else d
            a = dt.datetime.combine(d, dt.time(start_min // 60, start_min % 60), tzinfo=self._tz).timestamp()
            b = dt.datetime.combine(end_day, dt.time(end_min // 60, end_min % 60), tzinfo=self._tz).timestamp()
            a, b = max(a, day_start), min(b, day_end)
            if b > a:
                out.append((a, b))
            d += dt.timedelta(days=1)
        return out

    @staticmethod
    def _flat(now: float) -> float:
        start, end = pt_day_start(now), next_pt_midnight(now)
        return (now - start) / (end - start)


class Pacer:
    """The upgrade test for gemini-auto turn starts (DESIGN §16.9.2). It reads quota state from
    memory (SQLite only at a new PT date) and takes no lock: two turns starting at once may both
    be upgraded, which pacing accepts (§16.9.4)."""

    def __init__(self, config: Config, store: QuotaStore, curve: DemandCurve) -> None:
        assert config.auto is not None
        self._auto = config.auto
        self._store = store
        self._curve = curve
        self._models = [config.models[k] for k in config.auto.tiers[config.auto.pacing.tier]]

    def view(self, now: float) -> PaceView:
        """The pace now. Raises sqlite3.Error if today's counts can't be read."""
        capacity, left = self._store.pool(self._models)
        share, source = self._curve.share(now)
        above = (capacity + self._auto.pacing.reserve) * (1.0 - share)
        return PaceView(self._auto.pacing.tier, capacity, left, share, source, above)
