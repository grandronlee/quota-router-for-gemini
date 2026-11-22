"""Per-model quota state (DESIGN §5 step 4, §6, §7).

- RPM and input-TPM: rolling 60 s windows, in memory only.
- RPD: counter per (model, PT date), persisted to SQLite.
- exhausted_until (per-day 429): persisted to SQLite.
- Cooldowns, the 5xx failure streak and "disabled": in memory only.

A model that keeps failing gets a growing 5xx cooldown (DESIGN §22): its first 30 s block it
(hard), the rest only moves it to the back (soft, tried as a last resort). While its streak is
above 0 and the hard part is over, one reservation at a time holds its recovery slot. An epoch
per model makes outcomes of attempts reserved before the last state change stale (§22.3).

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
UNAVAILABLE_COOLDOWN_S = 30.0  # the hard part of every 5xx cooldown (DESIGN §22.2)
UNAVAILABLE_GROWTH = 4
UNAVAILABLE_MAX_S = 900.0
# Retry-After when a model is skipped only because its recovery attempt is in flight (DESIGN
# §22.2). The attempt can take up to 120 s; this keeps a compliant client from retrying every second.
RECOVERY_RETRY_S = 5.0
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
    epoch: int = 0  # the model's epoch when reserved (DESIGN §22.3)
    slot: bool = False  # holds the model's recovery slot until `release_slot` (§22.2)


def unavailable_cooldown_s(streak: int) -> float:
    """Length of the 5xx cooldown after `streak` counted failures in a row (DESIGN §22.2)."""
    if streak <= 0:
        return 0.0
    return min(UNAVAILABLE_MAX_S, UNAVAILABLE_COOLDOWN_S * UNAVAILABLE_GROWTH ** min(streak - 1, 8))


@dataclass(frozen=True)
class Blocked:
    reason: Literal["quota", "unavailable"]
    ready_at: float
    detail: str
    # A 5xx cooldown is live, even if `reason` is "quota" because a quota limit is hit too
    # (DESIGN §5.2). Handoffs need it: an unhealthy owner hands off (§16.8).
    cooling_down: bool = False
    # Only the soft part of a 5xx cooldown, or a recovery attempt in flight, keeps the model
    # out: the router may still try it in its last-resort pass (DESIGN §22.2).
    deferred: bool = False


@dataclass
class _State:
    day: str
    rpd: int = 0
    rpm: deque[float] = field(default_factory=deque)
    tokens: deque[_TokenEntry] = field(default_factory=deque)
    quota_cooldown_until: float = 0.0
    unavailable_cooldown_until: float = 0.0  # hard: blocks the model
    soft_until: float = 0.0  # end of the whole 5xx cooldown; after the hard part, tried last
    streak: int = 0  # counted 5xx failures in a row (DESIGN §22.1)
    epoch: int = 0  # moves on whenever the streak or the 5xx cooldown changes (§22.3)
    slot: Reservation | None = None  # the recovery attempt in flight (§22.2)
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

    def _blocked(self, model: ModelConfig, st: _State, input_tokens: int, now: float,
                 last_resort: bool = False) -> Blocked | None:
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

        hard = st.unavailable_cooldown_until > now
        soft = not hard and st.soft_until > now
        recovering = not hard and st.streak > 0 and st.slot is not None
        if hard:
            ready = max(ready, st.unavailable_cooldown_until)

        if quota:
            # Kept with a quota reason too, so a failing owner still hands off (§16.8, §22.2).
            return Blocked("quota", ready, ", ".join(quota), cooling_down=hard or soft or recovering)
        if hard:
            return Blocked("unavailable", ready, "5xx cooldown", cooling_down=True)
        # Deferred models are ready now as a last resort, so they don't stretch Retry-After.
        if soft and not last_resort:
            return Blocked("unavailable", now, "5xx cooldown, tried last", cooling_down=True, deferred=True)
        if recovering:
            return Blocked("unavailable", now + RECOVERY_RETRY_S, "recovery attempt in flight",
                           cooling_down=True, deferred=True)
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

    def check(self, model: ModelConfig, input_tokens: int, *, last_resort: bool = False) -> Blocked | None:
        """Why the model can't take this request now (None if it can). Reserves nothing."""
        now = self._clock.now()
        return self._blocked(model, self._get(model, now), input_tokens, now, last_resort)

    def ready_at(self, model: ModelConfig, input_tokens: int) -> float:
        now = self._clock.now()
        b = self._blocked(model, self._get(model, now), input_tokens, now)
        return b.ready_at if b else now

    def try_reserve(self, model: ModelConfig, input_tokens: int, *,
                    last_resort: bool = False) -> Reservation | Blocked:
        """Check eligibility and, if eligible, reserve one RPM slot, one RPD slot and the input
        tokens. `last_resort` ignores the soft part of a 5xx cooldown (DESIGN §22.2). A model
        with a failure streak also gives the reservation its recovery slot, which the caller
        must free with `release_slot` (or `release`) once the attempt's outcome is known."""
        now = self._clock.now()
        st = self._get(model, now)
        blocked = self._blocked(model, st, input_tokens, now, last_resort)
        if blocked:
            return blocked
        self._save_rpd(model.id, st.day, st.rpd + 1)
        st.rpd += 1
        st.rpm.append(now)
        entry = _TokenEntry(now, input_tokens)
        st.tokens.append(entry)
        res = Reservation(model.id, st.day, now, entry, epoch=st.epoch)
        if st.streak > 0:  # `_blocked` saw the slot free and the hard part over
            res.slot = True
            st.slot = res
        return res

    def release_slot(self, res: Reservation) -> None:
        """Free the recovery slot if `res` holds it. Safe to call more than once."""
        if not res.slot:
            return
        res.slot = False
        st = self._state[res.model_id]
        if st.slot is res:
            st.slot = None

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
        RPD slot, the RPM slot, the tokens and any recovery slot. Memory first, then the RPD write."""
        self.release_slot(res)
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

    def on_unavailable(self, model: ModelConfig, res: Reservation | None = None) -> bool:
        """A failure that doesn't count towards the streak (DESIGN §22.1): the hard 30 s only.
        Changes neither the streak nor the epoch. With `res`, a stale outcome changes nothing,
        so a delayed failure can't block a model that has since recovered (§22.3). Returns
        whether the cooldown was set."""
        st = self._state[model.id]  # not `_get`, as in on_rate_limited
        if res is not None and res.epoch != st.epoch:
            return False
        now = self._clock.now()
        st.unavailable_cooldown_until = max(st.unavailable_cooldown_until, now + UNAVAILABLE_COOLDOWN_S)
        return True

    def on_failure(self, model: ModelConfig, res: Reservation) -> bool:
        """A counted failure of the attempt reserved as `res` (DESIGN §22.1). Grows the streak
        and the cooldown, unless the outcome is stale (§22.3). Returns whether it counted."""
        st = self._state[model.id]
        if res.epoch != st.epoch:
            return False
        now = self._clock.now()
        st.streak += 1
        st.epoch += 1
        st.unavailable_cooldown_until = max(st.unavailable_cooldown_until, now + UNAVAILABLE_COOLDOWN_S)
        st.soft_until = max(st.soft_until, now + unavailable_cooldown_s(st.streak))
        return True

    def on_recovered(self, model: ModelConfig, res: Reservation) -> bool:
        """The model answered the attempt reserved as `res` (DESIGN §22.1): the streak goes to
        0 and the 5xx cooldown ends, unless the outcome is stale (§22.3). At streak 0 nothing
        changes and the epoch stays, so a busy healthy model keeps today's 30 s cooldown from a
        later 503. Returns whether the state changed."""
        st = self._state[model.id]
        if st.streak == 0 or res.epoch != st.epoch:
            return False
        st.streak = 0
        st.epoch += 1
        st.unavailable_cooldown_until = 0.0
        st.soft_until = 0.0
        return True

    def pool(self, models: Iterable[ModelConfig]) -> tuple[int, int]:
        """(capacity, left) of a paced tier (DESIGN §16.9.2): capacity sums the enabled models'
        RPD; left sums what each has left today, counting nothing for an exhausted one (Google's
        count wins). Cooldowns and RPM waits don't lower `left`. Reads today's count from SQLite
        at a new PT date, so it can raise sqlite3.Error."""
        now = self._clock.now()
        capacity = left = 0
        for m in models:
            if m.rpd is None or self._state[m.id].disabled:
                continue
            st = self._get(m, now)
            capacity += m.rpd
            if st.exhausted_until <= now:
                left += max(0, m.rpd - st.rpd)
        return capacity, left

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

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
            cooldown = max(st.quota_cooldown_until, st.unavailable_cooldown_until, st.soft_until)
            out[model_id] = {
                "rpm_used": len(st.rpm), "rpm_limit": model.rpm,
                "tpm_input_used": sum(e.tokens for e in st.tokens), "tpm_input_limit": model.tpm_input,
                "rpd_used": st.rpd, "rpd_limit": model.rpd, "day": st.day,
                "cooldown_until": cooldown if cooldown > now else None,
                "exhausted_until": st.exhausted_until if st.exhausted_until > now else None,
                "disabled": st.disabled,
                "unavailable_streak": st.streak,
            }
        return out
