"""Per-model quota state (DESIGN §5 step 4, §6, §7).

- RPM and input-TPM: rolling 60 s windows, in memory only.
- RPD: counter per (model, PT date), persisted to SQLite.
- exhausted_until (per-day 429): persisted to SQLite.
- Cooldowns and "disabled": in memory only.

Every method is synchronous and never awaits, so in the single event-loop thread
each call is atomic. `try_reserve` checks eligibility and reserves in one call.
Writes that grant quota (an RPD slot, a refund) go to SQLite before memory changes,
so a failed write grants nothing. Writes that block a model (exhausted) change memory
first, so a failed write still blocks it until restart.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .classify import Classification
from .clock import Clock, next_pt_midnight, pt_date, pt_day_start
from .config import ModelConfig

WINDOW_S = 60.0
UNAVAILABLE_COOLDOWN_S = 30.0
DEFAULT_429_COOLDOWN_S = 60.0
RPD_KEEP_DAYS = 7
# A per-day 429 this soon after PT midnight is taken as Google's reset lagging behind
# ours, not as a used-up day. Every model gets this grace (DESIGN §5 notes, changed after
# the Phase 4 review): a day really used up gets a few 429s, 60 s apart, then is exhausted.
MIDNIGHT_GRACE_S = 600.0

log = logging.getLogger("quota_router")


@dataclass(eq=False)  # compared by identity: `release` must remove this reservation's own entry
class _TokenEntry:
    t: float
    tokens: int


@dataclass
class Reservation:
    model_id: str
    day: str
    at: float  # when the request was reserved (sent)
    tokens: _TokenEntry
    rpd_refunded: bool = False
    settled: bool = False


@dataclass(frozen=True)
class Blocked:
    reason: Literal["quota", "unavailable"]
    ready_at: float
    detail: str


@dataclass
class _State:
    day: str
    rpd: int = 0
    rpm: deque[float] = field(default_factory=deque)
    tokens: deque[_TokenEntry] = field(default_factory=deque)
    quota_cooldown_until: float = 0.0
    unavailable_cooldown_until: float = 0.0
    tpm_full_until: float = 0.0
    exhausted_until: float = 0.0
    disabled: bool = False


class QuotaStore:
    def __init__(self, models: Iterable[ModelConfig], conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock
        self._models = {m.id: m for m in models}
        now = clock.now()
        today = pt_date(now)
        self.prune()
        self._state: dict[str, _State] = {}
        for model_id in self._models:
            st = _State(day=today, rpd=self._load_rpd(model_id, today))
            row = conn.execute("SELECT until FROM exhausted WHERE model_id = ?", (model_id,)).fetchone()
            if row:
                st.exhausted_until = float(row[0])
            self._state[model_id] = st

    def prune(self) -> None:
        """Delete RPD rows older than RPD_KEEP_DAYS and expired exhausted rows (DESIGN §20.10).
        Memory is untouched: it only ever holds today's count and live states."""
        now = self._clock.now()
        oldest = (dt.date.fromisoformat(pt_date(now)) - dt.timedelta(days=RPD_KEEP_DAYS)).isoformat()
        self._conn.execute("DELETE FROM rpd WHERE day < ?", (oldest,))
        self._conn.execute("DELETE FROM exhausted WHERE until <= ?", (now,))

    # ---- internals -------------------------------------------------------

    def _load_rpd(self, model_id: str, day: str) -> int:
        row = self._conn.execute(
            "SELECT count FROM rpd WHERE model_id = ? AND day = ?", (model_id, day)).fetchone()
        return int(row[0]) if row else 0

    def _save_rpd(self, model_id: str, day: str, count: int) -> None:
        self._conn.execute(
            "INSERT INTO rpd (model_id, day, count) VALUES (?, ?, ?) "
            "ON CONFLICT (model_id, day) DO UPDATE SET count = excluded.count",
            (model_id, day, count))

    def _get(self, model: ModelConfig, now: float) -> _State:
        st = self._state[model.id]
        day = pt_date(now)
        if st.day != day:  # new PT date: the daily counter starts again
            st.rpd = self._load_rpd(model.id, day)
            st.day = day
        cutoff = now - WINDOW_S
        while st.rpm and st.rpm[0] <= cutoff:
            st.rpm.popleft()
        while st.tokens and st.tokens[0].t <= cutoff:
            st.tokens.popleft()
        return st

    def _blocked(self, model: ModelConfig, st: _State, input_tokens: int, now: float) -> Blocked | None:
        quota: list[str] = []
        ready = now

        if st.exhausted_until > now:
            quota.append("exhausted")
            ready = max(ready, st.exhausted_until)
        if st.quota_cooldown_until > now:
            quota.append("429 cooldown")
            ready = max(ready, st.quota_cooldown_until)
        if model.rpm is not None and len(st.rpm) >= model.rpm:
            quota.append("rpm")
            times = sorted(st.rpm)
            ready = max(ready, times[len(times) - model.rpm] + WINDOW_S)
        if model.rpd is not None and st.rpd >= model.rpd:
            quota.append("rpd")
            ready = max(ready, next_pt_midnight(now))
        tpm_ready = self._tpm_ready(model, st, input_tokens, now)
        if tpm_ready > now:
            quota.append("tpm")
            ready = max(ready, tpm_ready)

        unavailable = st.unavailable_cooldown_until > now
        if unavailable:
            ready = max(ready, st.unavailable_cooldown_until)

        if quota:
            return Blocked("quota", ready, ", ".join(quota))
        if unavailable:
            return Blocked("unavailable", ready, "5xx cooldown")
        return None

    @staticmethod
    def _tpm_ready(model: ModelConfig, st: _State, input_tokens: int, now: float) -> float:
        """Earliest time the input-TPM window has room for `input_tokens`."""
        if model.tpm_input is None:  # no limit, but a TPM 429 still blocks for its window
            return max(now, st.tpm_full_until)
        if input_tokens > model.tpm_input:
            return math.inf  # never fits; the router reports this as too_large first
        t = max(now, st.tpm_full_until)
        live = sorted((e for e in st.tokens if e.t + WINDOW_S > t), key=lambda e: e.t)
        used = sum(e.tokens for e in live)
        if used + input_tokens <= model.tpm_input:
            return t
        for e in live:
            used -= e.tokens
            if used + input_tokens <= model.tpm_input:
                return e.t + WINDOW_S
        return t  # unreachable: an empty window always has room

    # ---- public API ------------------------------------------------------

    def check(self, model: ModelConfig, input_tokens: int) -> Blocked | None:
        """Why the model can't take this request now (None if it can). Reserves nothing."""
        now = self._clock.now()
        return self._blocked(model, self._get(model, now), input_tokens, now)

    def ready_at(self, model: ModelConfig, input_tokens: int) -> float:
        now = self._clock.now()
        b = self._blocked(model, self._get(model, now), input_tokens, now)
        return b.ready_at if b else now

    def try_reserve(self, model: ModelConfig, input_tokens: int) -> Reservation | Blocked:
        """Check eligibility and, if eligible, reserve one RPM slot, one RPD slot and the input tokens."""
        now = self._clock.now()
        st = self._get(model, now)
        blocked = self._blocked(model, st, input_tokens, now)
        if blocked:
            return blocked
        self._save_rpd(model.id, st.day, st.rpd + 1)
        st.rpd += 1
        st.rpm.append(now)
        entry = _TokenEntry(now, input_tokens)
        st.tokens.append(entry)
        return Reservation(model.id, st.day, now, entry)

    def refund_rpd(self, res: Reservation) -> None:
        """Give back the RPD slot (after a 429 or 404). The RPM slot and tokens are kept."""
        if res.rpd_refunded:
            return
        model = self._models[res.model_id]
        st = self._get(model, self._clock.now())
        if st.day == res.day and st.rpd > 0:  # an old day's count no longer matters
            self._save_rpd(model.id, st.day, st.rpd - 1)
            st.rpd -= 1
        res.rpd_refunded = True

    def release(self, res: Reservation) -> None:
        """Undo a reservation whose call was never started (DESIGN §5 notes): give back the
        RPD slot, the RPM slot and the tokens. Memory first, then the RPD write."""
        st = self._state[res.model_id]
        try:
            st.rpm.remove(res.at)
        except ValueError:
            pass  # already outside the minute window
        try:
            st.tokens.remove(res.tokens)
        except ValueError:
            pass
        self.refund_rpd(res)

    def settle(self, res: Reservation, prompt_tokens: int | None) -> None:
        """Replace the estimated input tokens with the reported `prompt_tokens`, if any."""
        if res.settled:
            return
        res.settled = True
        if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) and prompt_tokens >= 0:
            res.tokens.tokens = prompt_tokens

    def on_rate_limited(self, model: ModelConfig, c: Classification, sent_at: float | None = None) -> None:
        """Apply a 429. `sent_at` is when the rejected request was sent: a per-day 429 is about
        that request's PT day, even if it is handled after midnight."""
        now = self._clock.now()
        # Not `_get`: blocking state doesn't depend on the day, and `_get` can read SQLite
        # (first call of a new PT day). A failed read must not drop the state (DESIGN §5 notes).
        st = self._state[model.id]
        if c.kind == "rpd":
            sent = now if sent_at is None else sent_at
            day_end = next_pt_midnight(sent)
            if day_end <= now or sent - pt_day_start(sent) < MIDNIGHT_GRACE_S:
                # The day it was about has ended, or it came minutes after midnight (every
                # model, DESIGN §5 notes): back off briefly instead of losing the whole new day.
                log.warning("per-day 429 from %s near PT midnight; cooling down instead", model.id)
                # At least 60 s, and never shorter than a delay Google gave.
                delay = max(DEFAULT_429_COOLDOWN_S, c.retry_after_s or 0.0)
                st.quota_cooldown_until = max(st.quota_cooldown_until, now + delay)
                return
            until = max(st.exhausted_until, day_end)
            st.exhausted_until = until
            self._conn.execute(
                "INSERT INTO exhausted (model_id, until) VALUES (?, ?) "
                "ON CONFLICT (model_id) DO UPDATE SET until = excluded.until",
                (model.id, until))
            return
        if c.kind == "unknown":
            delay = max(DEFAULT_429_COOLDOWN_S, c.retry_after_s or 0.0)
        else:
            delay = c.retry_after_s if c.retry_after_s is not None else DEFAULT_429_COOLDOWN_S
        delay = max(1.0, delay)
        st.quota_cooldown_until = max(st.quota_cooldown_until, now + delay)
        if c.kind == "tpm":
            st.tpm_full_until = max(st.tpm_full_until, now + WINDOW_S)

    def on_unavailable(self, model: ModelConfig) -> None:
        now = self._clock.now()
        st = self._state[model.id]  # not `_get`, as in on_rate_limited
        st.unavailable_cooldown_until = max(st.unavailable_cooldown_until, now + UNAVAILABLE_COOLDOWN_S)

    def disable(self, model: ModelConfig) -> None:
        self._state[model.id].disabled = True

    def is_disabled(self, model: ModelConfig) -> bool:
        return self._state[model.id].disabled

    def snapshot(self) -> dict[str, dict]:
        """Current usage per model, for /status."""
        now = self._clock.now()
        out = {}
        for model_id, model in self._models.items():
            st = self._get(model, now)
            cooldown = max(st.quota_cooldown_until, st.unavailable_cooldown_until)
            out[model_id] = {
                "rpm_used": len(st.rpm), "rpm_limit": model.rpm,
                "tpm_input_used": sum(e.tokens for e in st.tokens), "tpm_input_limit": model.tpm_input,
                "rpd_used": st.rpd, "rpd_limit": model.rpd, "day": st.day,
                "cooldown_until": cooldown if cooldown > now else None,
                "exhausted_until": st.exhausted_until if st.exhausted_until > now else None,
                "disabled": st.disabled,
            }
        return out
