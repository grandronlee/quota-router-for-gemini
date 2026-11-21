"""Candidate selection and the fallback loop, without HTTP (DESIGN §5, §5.1, §9).

The HTTP layer (Phase 2) supplies an `Upstream` callable that makes one call to
Google and maps the result to an `UpstreamResult`. For streams it returns `Served`
as soon as the first byte is available. After the response is finished, the HTTP
layer calls `Router.complete()` with the reported usage and any tool-call IDs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Mapping, Protocol

from .affinity import AffinityStore
from .classify import classify_429
from .clock import Clock
from .config import Config, ModelConfig
from .quota import Blocked, QuotaStore, Reservation
from .turns import TurnReason, TurnRef, new_turn_id

log = logging.getLogger("quota_router")

ATTEMPT_TIMEOUT_CAP_S = 120.0  # time to first byte (DESIGN §9)
CHARS_PER_TOKEN = 4.0
ESTIMATE_SAFETY = 1.2
NON_TEXT_PART_TOKENS = 1500  # images and other non-text parts; their bytes say little about tokens
STATE_ERROR_RETRY_S = 30.0  # Retry-After hint when the quota state can't be read or written
MIN_ATTEMPT_S = 5.0  # a later attempt with less time left than this isn't started (DESIGN §5.2)

_FAILED = object()  # sentinel: a state read failed
_STOP = object()  # sentinel: the last-resort pass has no attempts or time left

Reason = Literal["disabled", "too_large", "quota", "unavailable", "not_tried"]
Affinity = Literal["pinned", "unknown", "mixed", "handoff"]

# Google's documented signature for history "transferred from a different model" (DESIGN §16.8).
PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"
HandoffHook = Callable[[str, str, str], None]  # (from model ID, to model ID, result)


# ---- upstream results (supplied by the HTTP layer) -----------------------

# Every result's `status` is the HTTP status, or for an SSE error event its code (None when it
# has none). Only the request log line uses it (DESIGN §22.4), and Rejected's recovery rule.

@dataclass(frozen=True)
class Served:
    """2xx, and (for streams) the first byte is ready. `payload` is opaque to the router.
    A stream's recovery is recorded later, at [DONE] (DESIGN §22.1)."""
    payload: Any
    stream: bool = False
    status: int | None = None


@dataclass(frozen=True)
class RateLimited:
    body: Any
    headers: Mapping[str, str] | None = None
    status: int | None = 429


@dataclass(frozen=True)
class Unavailable:
    """500/502/503/504, connect error, or timeout before the first byte.

    `not_sent`: the request never left the machine (connect error or timeout, pool timeout,
    or a failure before sending started), so Google can't have counted it (DESIGN §5 step 4).
    `counts`: a failure that grows the 5xx streak whatever its timing: a 5xx status, a 5xx or
    code-less error event, or a stream that ends empty (DESIGN §22.1). A timeout counts only
    if `body_sent` (the request was fully written) and it used the attempt's full budget."""
    detail: str
    not_sent: bool = False
    counts: bool = False
    body_sent: bool = False
    status: int | None = None


@dataclass(frozen=True)
class ModelNotFound:
    body: Any
    status: int | None = 404


@dataclass(frozen=True)
class Rejected:
    """Any other 4xx: returned to the client unchanged."""
    payload: Any
    status: int | None = None


UpstreamResult = Served | RateLimited | Unavailable | ModelNotFound | Rejected

# 4xx answers that show the model is reachable, so they end a 5xx streak (DESIGN §22.1).
RECOVERY_STATUSES = frozenset({400, 401, 403, 422})


@dataclass
class Attempt:
    """One upstream call, for the request log line (DESIGN §22.4). `outcome` stays
    `client_cancelled` if the call never returns. A served stream's entry is finished again
    when the stream ends."""
    model: str
    started: float = field(default_factory=time.monotonic)
    outcome: str = "client_cancelled"
    status: int | None = None
    ms: int | None = None

    def finish(self, outcome: str, status: int | None) -> None:
        self.outcome, self.status = outcome, status
        self.ms = round((time.monotonic() - self.started) * 1000)

    def log(self) -> dict:
        return {"model": self.model, "outcome": self.outcome, "status": self.status, "ms": self.ms}


def log_outcome(result: UpstreamResult) -> str:
    """The log outcome of a call's result (DESIGN §22.4); a stream is finished later."""
    if isinstance(result, Served):
        return "stream_incomplete" if result.stream else "ok"
    if isinstance(result, RateLimited):
        return "rate_limited"
    if isinstance(result, Unavailable):
        if result.not_sent:
            return "not_sent"
        return "timeout" if result.detail == "timeout" else "unavailable"
    if isinstance(result, Rejected):
        return "rejected"
    return "not_found"


class Upstream(Protocol):
    """One upstream call. `body`, when given, replaces the client's request for this call
    (a handed-off tool turn, DESIGN §16.8); it is passed only when it differs."""
    async def __call__(self, model: ModelConfig, timeout_s: float, *,
                       body: Mapping[str, Any] | None = None) -> UpstreamResult: ...


# ---- route outcomes ------------------------------------------------------

@dataclass
class Candidate:
    model: ModelConfig
    reason: Reason | None = None
    ready_at: float | None = None
    detail: str = ""
    # Failed on this request (5xx, timeout, state error) or in a 5xx cooldown, whatever
    # `reason` says after merging with quota limits (DESIGN §5.2). Used by the handoff gate.
    unhealthy: bool = False
    # When its state was read for this request (before reserving or skipping), or when its
    # 429 was applied. The handoff gate measures the wait from here, so a cooldown set by this
    # request's own 429 counts in full however long the bookkeeping after it took, and the
    # time the 429 took to arrive doesn't count at all.
    checked_at: float | None = None


@dataclass
class RouteServed:
    model: ModelConfig
    payload: Any
    reservation: Reservation
    path: list[str]
    affinity: Affinity | None
    handoff_from: str | None = None  # the turn's owner, when another model served it
    turn: TurnRef | None = None  # its decision-log turn (DESIGN §16.8 1d)
    attempt: Attempt | None = None  # this call's log entry, finished when a stream ends


@dataclass
class RoutePassthrough:
    """Upstream 4xx, returned unchanged (including thought-signature 400s)."""
    model: ModelConfig
    payload: Any
    reservation: Reservation
    path: list[str]
    affinity: Affinity | None
    handoff_from: str | None = None
    rewritten: bool = False  # sent with placeholder signatures (DESIGN §16.8)
    turn: TurnRef | None = None


@dataclass
class RouteFailed:
    status: int
    code: str
    message: str
    retry_after_s: int | None
    path: list[str] = field(default_factory=list)
    affinity: Affinity | None = None
    candidates: list[Candidate] = field(default_factory=list)
    handoff_from: str | None = None  # the owner, when a handoff target was tried and failed too
    turn: TurnRef | None = None  # None only for an unknown model or route

    def error_body(self) -> dict:
        etype = {400: "invalid_request_error", 404: "invalid_request_error",
                 429: "rate_limit_error"}.get(self.status, "service_unavailable")
        return {"error": {"message": self.message, "type": etype, "code": self.code, "param": None}}


RouteOutcome = RouteServed | RoutePassthrough | RouteFailed


# ---- request inspection --------------------------------------------------

def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def estimate_input_tokens(request: Mapping[str, Any]) -> int:
    """input ≈ chars/4 × 1.2, over messages and tool definitions (DESIGN §5 step 3)."""
    chars = 0
    extra = 0
    messages = request.get("messages")
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, dict):
            chars += len(_dumps(msg))
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chars += len(str(part.get("text") or ""))
                else:
                    extra += NON_TEXT_PART_TOKENS
            chars += len(_dumps({k: v for k, v in msg.items() if k != "content"}))
        else:
            chars += len(_dumps(msg))
    if request.get("tools"):
        chars += len(_dumps(request["tools"]))
    return math.ceil(chars / CHARS_PER_TOKEN * ESTIMATE_SAFETY) + extra


def requested_output_tokens(request: Mapping[str, Any]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens"):
        v = request.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return None


def active_turn_start(messages: list) -> int:
    """Index of the `user` message that starts the active turn, or -1 (DESIGN §5.1).

    The turn starts at the last `user` message that doesn't come straight after a `tool`
    message: Zoo Code sends its environment details that way after every tool result."""
    last_user = -1
    before_users = None  # role of the message before the current run of user messages
    prev_role = None
    for i, m in enumerate(messages):
        role = m.get("role") if isinstance(m, dict) else None
        if role == "user":
            if prev_role != "user":
                before_users = prev_role
            if before_users != "tool":
                last_user = i
        prev_role = role
    return last_user


def with_placeholder_signatures(request: Mapping[str, Any], serving_id: str,
                                owners: Mapping[str, str]) -> Mapping[str, Any]:
    """The request with every active-turn tool call that `serving_id` didn't make (by the
    affinity record; unknown and ID-less calls included) carrying the placeholder signature
    (DESIGN §16.8). Changed messages and calls are copied; the client's request is untouched.
    Returns `request` itself when nothing needs changing."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return request
    new = list(messages)
    changed = False
    for i in range(active_turn_start(messages) + 1, len(messages)):
        m = messages[i]
        if not (isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list)):
            continue
        calls = []
        for tc in m["tool_calls"]:
            if isinstance(tc, dict):
                tc_id = tc.get("id")
                owner = owners.get(tc_id) if isinstance(tc_id, str) else None
                if owner != serving_id:
                    extra = tc.get("extra_content")
                    extra = extra if isinstance(extra, dict) else {}
                    google = extra.get("google")
                    google = google if isinstance(google, dict) else {}
                    tc = {**tc, "extra_content": {
                        **extra, "google": {**google, "thought_signature": PLACEHOLDER_SIGNATURE}}}
            calls.append(tc)
        if calls != m["tool_calls"]:
            new[i] = {**m, "tool_calls": calls}
            changed = True
    return {**request, "messages": new} if changed else request


def active_turn_tool_call_ids(messages: Any) -> list[str]:
    """IDs of assistant tool calls in the active turn, oldest first (DESIGN §5.1)."""
    if not isinstance(messages, list):
        return []
    ids: list[str] = []
    for m in messages[active_turn_start(messages) + 1:]:
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        calls = m.get("tool_calls")
        for tc in calls if isinstance(calls, list) else []:
            tc_id = tc.get("id") if isinstance(tc, dict) else None
            if isinstance(tc_id, str) and tc_id:
                ids.append(tc_id)
    return ids


def fits(model: ModelConfig, input_tokens: int, output_tokens: int | None) -> bool:
    limit = model.input_limit if model.tpm_input is None else min(model.input_limit, model.tpm_input)
    if input_tokens > limit:
        return False
    return output_tokens is None or output_tokens <= model.output_limit


# ---- the router ----------------------------------------------------------

class Router:
    def __init__(self, config: Config, store: QuotaStore, affinity: AffinityStore, clock: Clock) -> None:
        self._config = config
        self._store = store
        self._affinity = affinity
        self._clock = clock
        # Guards eligibility check + reservation as one step (DESIGN §5 step 4).
        # QuotaStore.try_reserve never awaits, so this also holds if the lock were removed;
        # it is kept so that a future awaiting store can't over-commit.
        self._lock = asyncio.Lock()
        # For /metrics (DESIGN §17.2, §20.6): SQLite errors and tool-call IDs seen twice.
        self.state_errors = 0
        self.tool_call_ids_reused = 0
        self.on_handoff: HandoffHook | None = None  # set by the HTTP layer for /metrics

    def resolve(self, name: Any) -> list[ModelConfig] | None:
        if not isinstance(name, str):
            return None
        if name in self._config.routes:
            return self._config.route_models(name)
        m = self._config.model_by_id(name)
        return [m] if m else None

    def _pin(self, request: Mapping[str, Any], candidates: list[ModelConfig]
             ) -> tuple[list[ModelConfig], Affinity | None, dict[str, str], str | None]:
        """Candidates after pinning, the affinity label, the known ID → model map (empty
        unless the turn is pinned to an owner, which is then `candidates[0]`), and the turn ID
        of the latest known call that has one (DESIGN §16.8 1d)."""
        ids = active_turn_tool_call_ids(request.get("messages"))
        if not ids:
            return candidates, None, {}, None
        try:
            records = self._affinity.lookup_records(ids)
        except sqlite3.Error:
            self.state_errors += 1
            log.exception("affinity lookup failed; routing the tool turn normally")
            return candidates, "unknown", {}, None
        known = {i: r.model_id for i, r in records.items()}
        owners = [known[i] for i in ids if i in known]
        if not owners:
            log.warning("tool turn with %d call ID(s), none known; routing normally", len(ids))
            return candidates, "unknown", {}, None
        turn_id = next((records[i].turn_id for i in reversed(ids)
                        if i in records and records[i].turn_id), None)
        owner_id = owners[-1]  # the latest known call's model
        model = self._config.model_by_id(owner_id)
        if model is None:
            log.warning("tool turn belongs to %s, which is no longer configured; routing normally", owner_id)
            return candidates, "unknown", {}, turn_id
        if len(set(owners)) > 1:
            # Expected after a handoff (DESIGN §16.8); without one it is worth a look.
            log.info("tool turn spans %d models; pinning to the latest call's model %s",
                     len(set(owners)), owner_id)
            return [model], "mixed", known, turn_id
        return [model], "pinned", known, turn_id

    @staticmethod
    def _turn(request: Mapping[str, Any], affinity: Affinity | None, turn_id: str | None,
              started: float) -> TurnRef:
        """The request's decision-log turn. A request with no tool calls in its active turn
        starts one; a follow-up continues the turn its calls name, or starts a row (`unknown`
        when none of its calls is known). A known call whose model is no longer configured is
        routed as `unknown` but still names its turn, so that row is `pinned`."""
        tools = request.get("tools")
        n_tools = len(tools) if isinstance(tools, list) else 0
        if affinity is None:
            reason: TurnReason = "first"
        elif affinity == "unknown" and turn_id is None:
            reason = "unknown"
        else:
            reason = "pinned"
        return TurnRef(turn_id or new_turn_id(), reason, n_tools, started)

    @staticmethod
    def _served_turn(turn: TurnRef, resolved: list[ModelConfig], model: ModelConfig,
                     handoff_from: str | None) -> TurnRef:
        """`turn` with the reason this request's serving model gives, in case it creates
        the row: a turn start served after the route's first model is a spill."""
        if turn.reason == "first" and model.id != resolved[0].id:
            return replace(turn, reason="spill")
        if turn.reason == "pinned" and handoff_from is not None:
            return replace(turn, reason="handoff")
        return turn

    def _handoff_targets(self, name: Any, resolved: list[ModelConfig],
                         owner: ModelConfig) -> list[ModelConfig]:
        """The route's other models listed in `handoff_targets`, in route order. A request
        for a real model ID gets none (DESIGN §16.8)."""
        if not (isinstance(name, str) and name in self._config.routes and self._config.handoff_targets):
            return []
        allowed = set(self._config.handoff_targets)
        return [m for m in resolved if m.key in allowed and m.id != owner.id]

    def _may_hand_off(self, owner: Candidate) -> bool:
        """Hand off only if the owner can't take the request within handoff_after_s: it
        failed or is cooling down after a 5xx, can never serve it, or is blocked for longer
        (DESIGN §16.8). A short RPM or TPM wait gets the owner's 429 instead. A per-minute
        window always frees within 60 s; a fresh 60 s cooldown after a 429 reaches the default."""
        if owner.unhealthy or owner.reason in ("unavailable", "disabled", "too_large"):
            return True
        if owner.reason == "quota":
            since = self._clock.now() if owner.checked_at is None else owner.checked_at
            return owner.ready_at is None or owner.ready_at - since >= self._config.handoff_after_s
        return False  # not_tried: attempts or the deadline ran out

    def _note_handoff(self, owner: ModelConfig | None, model: ModelConfig, result: str) -> None:
        if owner is None or model.id == owner.id or self.on_handoff is None:
            return
        try:
            self.on_handoff(owner.id, model.id, result)
        except Exception:
            log.exception("handoff hook failed")

    async def route(self, request: Mapping[str, Any], upstream: Upstream,
                    *, started_at: float | None = None,
                    attempts: list[Attempt] | None = None) -> RouteOutcome:
        """Route one request. `attempts`, when given, receives one entry per upstream call as
        it starts, so the caller can still log them if routing is cancelled (DESIGN §22.4)."""
        started = self._clock.now() if started_at is None else started_at
        deadline = started + self._config.request_deadline_s
        name = request.get("model")
        resolved = self.resolve(name)
        if resolved is None:
            return RouteFailed(404, "model_not_found", f"unknown model or route: {name!r}", None)
        candidates, affinity, known, turn_id = self._pin(request, resolved)
        turn = self._turn(request, affinity, turn_id, started)
        owner = candidates[0] if known else None
        if owner is not None:
            candidates = [owner, *self._handoff_targets(name, resolved, owner)]
        input_tokens = estimate_input_tokens(request)
        output_tokens = requested_output_tokens(request)

        results: list[Candidate] = []
        path: list[str] = []
        log_ = attempts if attempts is not None else []
        tried = 0  # upstream calls made
        hand_off: bool | None = None  # decided once, from the owner's result
        # Skipped only for the soft part of a 5xx cooldown or a recovery attempt in flight:
        # tried after the normal pass if nothing served (DESIGN §22.2).
        deferred: list[tuple[Candidate, Mapping[str, Any], int]] = []

        async def attempt(c: Candidate, body: Mapping[str, Any], tokens: int,
                          last_resort: bool) -> RouteOutcome | None | object:
            """Try one candidate. Returns an outcome to return, None to go on, or _STOP when
            a last-resort pass has no attempts or time left."""
            nonlocal tried
            model = c.model
            served_affinity = affinity if owner is None or model.id == owner.id else "handoff"
            handoff_from = owner.id if owner is not None and model.id != owner.id else None
            async with self._lock:
                c.checked_at = self._clock.now()
                left = deadline - c.checked_at
                # The first attempt runs while any time is left; a later one needs MIN_ATTEMPT_S,
                # since a call that is sure to time out would only cost quota and a cooldown.
                limits_left = tried < self._config.max_attempts and \
                    (left > 0 if tried == 0 else left >= MIN_ATTEMPT_S)
                if last_resort and not limits_left:
                    return _STOP  # keeps the reason it was deferred for
                try:
                    res = self._store.try_reserve(model, tokens, last_resort=last_resort) if limits_left \
                        else self._store.check(model, tokens)
                except sqlite3.Error:
                    # Never call upstream without a reservation; this candidate is skipped.
                    self.state_errors += 1
                    log.exception("quota state error for %s; skipping it", model.id)
                    c.reason, c.detail = "unavailable", "quota state error"
                    c.ready_at = self._clock.now() + STATE_ERROR_RETRY_S
                    c.unhealthy = True
                    return None
            if isinstance(res, Blocked):
                c.reason, c.ready_at, c.detail = res.reason, res.ready_at, res.detail
                c.unhealthy = res.cooling_down
                if res.deferred and not last_resort:
                    deferred.append((c, body, tokens))
                return None
            if res is None:  # eligible, but no attempts or time left
                c.reason, c.ready_at = "not_tried", self._clock.now()
                return None
            try:  # from here on, the recovery slot `res` may hold is freed on every path
                # The reservation's SQLite write can wait (up to 1 s): measure again, and give the
                # whole reservation back if the attempt may no longer start (DESIGN §5 notes).
                left = deadline - self._clock.now()
                if not (left > 0 if tried == 0 else left >= MIN_ATTEMPT_S):
                    self._safely(self._store.release, res)
                    c.reason, c.ready_at = "not_tried", self._clock.now()
                    return _STOP if last_resort else None

                tried += 1
                path.append(model.id)
                full_timeout = min(ATTEMPT_TIMEOUT_CAP_S, self._config.request_deadline_s)
                # The time left measured after reserving (> 0 here), so never zero or negative.
                timeout = min(full_timeout, left)
                # Clearly cut short by the request deadline, by an earlier attempt or a slow upload
                # (the deadline counts from arrival, DESIGN §20.3): at least MIN_ATTEMPT_S less than usual.
                shortened = timeout <= full_timeout - MIN_ATTEMPT_S
                # Only a timeout with the whole usual budget counts towards the streak (§22.1).
                full_budget = timeout >= full_timeout
                attempt_started = self._clock.now()
                entry = Attempt(model.id)
                log_.append(entry)
                call = upstream(model, timeout) if body is request else upstream(model, timeout, body=body)
                try:
                    result = await asyncio.wait_for(call, timeout + 1.0)
                except TimeoutError:
                    result = Unavailable("timeout")
                except asyncio.CancelledError:
                    entry.finish("client_cancelled", None)
                    raise
            finally:
                # The attempt's routing outcome is known, or it was cancelled (DESIGN §22.2).
                self._store.release_slot(res)
            entry.finish(log_outcome(result), result.status)

            if isinstance(result, Served):
                if not result.stream:
                    self._store.on_recovered(model, res)
                self._note_handoff(owner, model, "served")
                return RouteServed(model, result.payload, res, path, served_affinity, handoff_from,
                                   turn=self._served_turn(turn, resolved, model, handoff_from),
                                   attempt=entry)
            if isinstance(result, Rejected):
                if result.status in RECOVERY_STATUSES:
                    self._store.on_recovered(model, res)
                self._note_handoff(owner, model, "rejected")
                return RoutePassthrough(model, result.payload, res, path, served_affinity, handoff_from,
                                        rewritten=body is not request,
                                        turn=self._served_turn(turn, resolved, model, handoff_from))
            self._note_handoff(owner, model, "failed")
            if isinstance(result, RateLimited):
                cls = classify_429(result.body, result.headers)
                if cls.kind == "unknown":
                    log.warning("unrecognised 429 from %s; cooling down", model.id)
                c.reason, c.detail = "quota", f"429 {cls.kind}"
                # The cooldown runs from when the 429 is applied, so the handoff gate measures
                # from here: a 429 that took 45 s to arrive with a 20 s delay is a 20 s wait,
                # not 65 s (found in review). Read before applying it, so a 60 s cooldown still
                # counts in full.
                c.checked_at = self._clock.now()
                self._store.on_recovered(model, res)  # it answered (DESIGN §22.1)
                self._safely(self._store.on_rate_limited, model, cls, sent_at=res.at)
                self._safely(self._store.refund_rpd, res)  # Google's 429 is the source of truth
            elif isinstance(result, Unavailable):
                c.reason, c.detail = "unavailable", result.detail
                c.unhealthy = True  # kept even if the merge below relabels it "quota"
                # ...and the timeout is that budget running out, not an early network timeout.
                ran_out = self._clock.now() - attempt_started >= timeout - 1.0
                timed_out = result.detail == "timeout"
                if result.not_sent:
                    # This machine's network, not the model: today's 30 s, no streak (DESIGN §22.1).
                    self._store.on_unavailable(model, res)
                elif shortened and ran_out and timed_out:
                    # Not evidence the model is unhealthy: it got less than its usual time.
                    # No cooldown; the RPD reservation is kept (the call may have used quota).
                    c.detail = "timeout (cut short by the request deadline)"
                elif result.counts or (timed_out and ran_out and full_budget and result.body_sent):
                    # Counted; a stale outcome changes nothing (DESIGN §22.3).
                    self._store.on_failure(model, res)
                else:
                    # An early timeout, a write timeout, one the deadline cut by under 5 s, or a
                    # send phase that can't be told: today's 30 s, no streak (DESIGN §22.1).
                    self._store.on_unavailable(model, res)
                if result.not_sent:
                    # Google never saw it, and a locally blocked model is never called again,
                    # so nothing else would correct the daily count (DESIGN §5 step 4).
                    self._safely(self._store.refund_rpd, res)
            elif isinstance(result, ModelNotFound):
                log.error("model %s not found upstream; disabled until restart", model.id)
                self._store.disable(model)
                self._safely(self._store.refund_rpd, res)
                c.reason = "disabled"
                return None
            else:
                raise TypeError(f"unexpected upstream result: {result!r}")
            # Its state now (DESIGN §5.2): a 5xx on a model that is also out of quota counts as quota.
            now = self._clock.now()
            blocked = self._safely(self._store.check, model, tokens, failed=_FAILED)
            if blocked is _FAILED:  # state unreadable: don't advertise "ready now"
                c.ready_at = now + STATE_ERROR_RETRY_S
                # Unhealthy only after a 5xx; a 429 with an unreadable state stays a short wait.
                c.unhealthy = c.reason == "unavailable"
                return None
            c.ready_at = blocked.ready_at if blocked else now
            if blocked and blocked.reason == "quota" and c.reason == "unavailable":
                c.reason, c.detail = "quota", f"{c.detail}; {blocked.detail}"
            return None

        for model in candidates:
            if owner is not None and model is not owner:
                if hand_off is None:
                    hand_off = self._may_hand_off(results[0])
                if not hand_off:
                    break  # the owner's own result decides (DESIGN §16.8)
            # Calls the serving model didn't make carry the placeholder: after a handoff, or in
            # a mixed turn served by its latest owner. An owner serving its own turn sends the
            # body unchanged, unknown IDs included (most likely its own calls).
            foreign = owner is not None and (
                model.id != owner.id or any(v != model.id for v in known.values()))
            body = with_placeholder_signatures(request, model.id, known) if foreign else request
            # Estimated from what this model is sent: placeholders are much shorter than real
            # signatures, so the client's body would overstate a handoff's size.
            tokens = input_tokens if body is request else estimate_input_tokens(body)
            c = Candidate(model)
            results.append(c)
            if self._store.is_disabled(model):
                c.reason = "disabled"
                continue
            if not fits(model, tokens, output_tokens):
                c.reason = "too_large"
                continue
            out = await attempt(c, body, tokens, last_resort=False)
            if out is not None:
                return out  # type: ignore[return-value]

        # Last resort (DESIGN §22.2): the models skipped only for the soft part of a 5xx
        # cooldown or a busy recovery slot, in candidate order, while attempts and time remain.
        # A pinned owner comes first here, after its handoff targets have failed.
        for c, body, tokens in deferred:
            if self._store.is_disabled(c.model):
                c.reason = "disabled"
                continue
            out = await attempt(c, body, tokens, last_resort=True)
            if out is _STOP:
                break
            if out is not None:
                return out  # type: ignore[return-value]

        out = self._final(name, results, path, affinity)
        out.turn = turn
        if owner is not None and any(m != owner.id for m in path):
            out.handoff_from = owner.id  # a target was called and failed too
        return out

    def _safely(self, fn, *args, failed: Any = None, **kwargs):
        """Run a state update after an upstream call; a SQLite error is logged, not raised,
        so the request can still fall back. Returns `failed` on error."""
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error:
            self.state_errors += 1
            log.exception("quota state update failed (%s)", getattr(fn, "__name__", fn))
            return failed

    def _final(self, name: Any, results: list[Candidate], path: list[str],
               affinity: Affinity | None) -> RouteFailed:
        """DESIGN §5 step 5."""
        now = self._clock.now()
        listing = "; ".join(
            f"{c.model.id}: {c.reason}" + (f" ({c.detail})" if c.detail else "")
            + (f", ready in {max(0, math.ceil(c.ready_at - now))}s"
               if c.ready_at is not None and c.reason in ("quota", "unavailable") else "")
            for c in results)
        live = [c for c in results if c.reason not in ("disabled", "too_large")]
        if not live:
            if any(c.reason == "too_large" for c in results):
                return RouteFailed(400, "context_length_exceeded",
                                   f"request too large for every candidate of {name}: {listing}",
                                   None, path, affinity, results)
            log.error("every candidate of %s is disabled", name)
            return RouteFailed(503, "no_enabled_model",
                               f"no enabled model for {name}: {listing}", None, path, affinity, results)
        earliest = min(c.ready_at if c.ready_at is not None else now for c in live)
        retry_after = max(1, math.ceil(earliest - now))
        if all(c.reason == "quota" for c in live):
            return RouteFailed(429, "rate_limit_exceeded", f"quota used up for {name}: {listing}",
                               retry_after, path, affinity, results)
        return RouteFailed(503, "upstream_unavailable", f"no model available for {name}: {listing}",
                           retry_after, path, affinity, results)

    def stream_error(self, served: RouteServed, kind: str, body: Any = None,
                     code: int | None = None) -> None:
        """An error event after streaming started (DESIGN §8). `kind` is "rate_limited",
        "unavailable" or "client"; `code` is the event's code. Updates the model's state; the
        RPD slot is kept, because the request was served in part. A 5xx or code-less event
        counts towards the streak; a 429 or a recovery 4xx ends it (§22.1). Never raises."""
        res = served.reservation
        try:
            if kind == "rate_limited":
                self._store.on_recovered(served.model, res)
                cls = classify_429(body, None)
                self._safely(self._store.on_rate_limited, served.model, cls, sent_at=res.at)
            elif kind == "unavailable":
                self._store.on_failure(served.model, res)
            elif code in RECOVERY_STATUSES:
                self._store.on_recovered(served.model, res)
        except Exception:  # the client already has its error event; this is bookkeeping
            self.state_errors += 1
            log.exception("could not record a mid-stream error for %s", served.model.id)

    def stream_done(self, served: RouteServed) -> None:
        """The stream ended cleanly with [DONE]: the model has recovered, unless the outcome
        is stale (DESIGN §22.1, §22.3). Not called at the first event, so a model that keeps
        breaking mid-stream still builds a streak. Never raises."""
        try:
            self._store.on_recovered(served.model, served.reservation)
        except Exception:
            self.state_errors += 1
            log.exception("could not record a finished stream for %s", served.model.id)

    def complete(self, served: RouteServed, prompt_tokens: int | None,
                 tool_call_ids: list[str] | None = None) -> None:
        """After the response finished: settle usage and record tool-call affinity (with
        the response's turn, DESIGN §16.8 1d).

        Safe to call more than once for one response: usage settles only the first time,
        so a later call can record tool-call IDs that arrived after the usage chunk."""
        self._store.settle(served.reservation, prompt_tokens)
        if tool_call_ids:
            try:
                reused = self._affinity.record(tool_call_ids, served.model.id,
                                               served.turn.turn_id if served.turn else None)
            except Exception:  # losing affinity only means "unknown" on the next request
                self.state_errors += 1
                log.exception("could not record tool-call affinity for %s", served.model.id)
                return
            if reused:
                self.tool_call_ids_reused += reused
                log.warning("%d tool-call ID(s) from %s were already recorded; affinity assumes "
                            "IDs never repeat (DESIGN §20.6)", reused, served.model.id)
