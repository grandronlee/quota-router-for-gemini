"""Injectable clock and Pacific-time day boundaries (DESIGN §6).

All times are Unix epoch seconds (float). Google resets daily quotas at midnight
America/Los_Angeles; the day key is that calendar date.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manually advanced clock for tests."""

    def __init__(self, start: float) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def set(self, t: float) -> None:
        self._t = float(t)


def pt_date(now: float) -> str:
    """The PT calendar date containing `now`, as YYYY-MM-DD."""
    return dt.datetime.fromtimestamp(now, PT).date().isoformat()


def pt_day_start(now: float) -> float:
    """The PT midnight that began the day containing `now`."""
    today = dt.datetime.fromtimestamp(now, PT).date()
    return dt.datetime.combine(today, dt.time(0), tzinfo=PT).timestamp()


def next_pt_midnight(now: float) -> float:
    """The first PT midnight strictly after `now`.

    At exactly 00:00:00 PT the day has just started, so the answer is the next
    day's midnight. PT midnight never falls inside a daylight-saving gap or fold
    (the changes happen at 02:00), so building the local datetime is unambiguous.
    """
    today = dt.datetime.fromtimestamp(now, PT).date()
    tomorrow = today + dt.timedelta(days=1)
    return dt.datetime.combine(tomorrow, dt.time(0), tzinfo=PT).timestamp()
