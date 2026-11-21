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
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

from .affinity import AffinityStore
from .classify import classify_429
from .clock import Clock
from .config import Config, ModelConfig
from .quota import Blocked, QuotaStore, Reservation

log = logging.getLogger("quota_router")

ATTEMPT_TIMEOUT_CAP_S = 120.0  # time to first byte (DESIGN §9)
CHARS_PER_TOKEN = 4.0
ESTIMATE_SAFETY = 1.2
NON_TEXT_PART_TOKENS = 1500  # images and other non-text parts; their bytes say little about tokens
STATE_ERROR_RETRY_S = 30.0  # Retry-After hint when the quota state can't be read or written

_FAILED = object()  # sentinel: a state read failed

Reason = Literal["disabled", "too_large", "quota", "unavailable", "not_tried"]
Affinity = Literal["pinned", "unknown", "mixed"]


# ---- upstream results (supplied by the HTTP layer) -----------------------

@dataclass(frozen=True)
class Served:
    """2xx, and (for streams) the first byte is ready. `payload` is opaque to the router."""
    payload: Any


@dataclass(frozen=True)
class RateLimited:
    body: Any
    headers: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Unavailable:
    """500/502/503/504, connect error, or timeout before the first byte."""
    detail: str


@dataclass(frozen=True)
class ModelNotFound:
    body: Any


@dataclass(frozen=True)
class Rejected:
    """Any other 4xx: returned to the client unchanged."""
    payload: Any


UpstreamResult = Served | RateLimited | Unavailable | ModelNotFound | Rejected


class Upstream(Protocol):
    async def __call__(self, model: ModelConfig, timeout_s: float) -> UpstreamResult: ...


# ---- route outcomes ------------------------------------------------------

@dataclass
class Candidate:
    model: ModelConfig
    reason: Reason | None = None
    ready_at: float | None = None
    detail: str = ""


@dataclass
class RouteServed:
    model: ModelConfig
    payload: Any
    reservation: Reservation
    path: list[str]
    affinity: Affinity | None


@dataclass
class RoutePassthrough:
    """Upstream 4xx, returned unchanged (including thought-signature 400s)."""
    model: ModelConfig
    payload: Any
    reservation: Reservation
    path: list[str]
    affinity: Affinity | None


@dataclass
class RouteFailed:
    status: int
    code: str
    message: str
    retry_after_s: int | None
    path: list[str] = field(default_factory=list)
    affinity: Affinity | None = None
    candidates: list[Candidate] = field(default_factory=list)

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


def active_turn_tool_call_ids(messages: Any) -> list[str]:
    """IDs of assistant tool calls after the last user message, oldest first."""
    if not isinstance(messages, list):
        return []
    last_user = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = i
    ids: list[str] = []
    for m in messages[last_user + 1:]:
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        calls = m.get("tool_calls")
        for tc in calls if isinstance(calls, list) else []:
            tc_id = tc.get("id") if isinstance(tc, dict) else None
            if isinstance(tc_id, str) and tc_id:
                ids.append(tc_id)
    return ids


def fits(model: ModelConfig, input_tokens: int, output_tokens: int | None) -> bool:
    if input_tokens > min(model.input_limit, model.tpm_input):
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

    def resolve(self, name: Any) -> list[ModelConfig] | None:
        if not isinstance(name, str):
            return None
        if name in self._config.routes:
            return self._config.route_models(name)
        m = self._config.model_by_id(name)
        return [m] if m else None

    def _pin(self, request: Mapping[str, Any], candidates: list[ModelConfig]
             ) -> tuple[list[ModelConfig], Affinity | None]:
        ids = active_turn_tool_call_ids(request.get("messages"))
        if not ids:
            return candidates, None
        try:
            known = self._affinity.lookup(ids)
        except sqlite3.Error:
            log.exception("affinity lookup failed; routing the tool turn normally")
            return candidates, "unknown"
        owners = [known[i] for i in ids if i in known]
        if not owners:
            log.warning("tool turn with %d call ID(s), none known; routing normally", len(ids))
            return candidates, "unknown"
        owner_id = owners[-1]  # the latest known call's model
        model = self._config.model_by_id(owner_id)
        if model is None:
            log.warning("tool turn belongs to %s, which is no longer configured; routing normally", owner_id)
            return candidates, "unknown"
        if len(set(owners)) > 1:
            log.warning("tool turn spans %d models; pinning to the latest call's model %s",
                        len(set(owners)), owner_id)
            return [model], "mixed"
        return [model], "pinned"

    async def route(self, request: Mapping[str, Any], upstream: Upstream,
                    *, started_at: float | None = None) -> RouteOutcome:
        started = self._clock.now() if started_at is None else started_at
        deadline = started + self._config.request_deadline_s
        name = request.get("model")
        resolved = self.resolve(name)
        if resolved is None:
            return RouteFailed(404, "model_not_found", f"unknown model or route: {name!r}", None)
        candidates, affinity = self._pin(request, resolved)
        input_tokens = estimate_input_tokens(request)
        output_tokens = requested_output_tokens(request)

        results: list[Candidate] = []
        path: list[str] = []
        attempts = 0
        for model in candidates:
            c = Candidate(model)
            results.append(c)
            if self._store.is_disabled(model):
                c.reason = "disabled"
                continue
            if not fits(model, input_tokens, output_tokens):
                c.reason = "too_large"
                continue
            async with self._lock:
                limits_left = attempts < self._config.max_attempts and self._clock.now() < deadline
                try:
                    res = self._store.try_reserve(model, input_tokens) if limits_left \
                        else self._store.check(model, input_tokens)
                except sqlite3.Error:
                    # Never call upstream without a reservation; this candidate is skipped.
                    log.exception("quota state error for %s; skipping it", model.id)
                    c.reason, c.detail = "unavailable", "quota state error"
                    c.ready_at = self._clock.now() + STATE_ERROR_RETRY_S
                    continue
            if isinstance(res, Blocked):
                c.reason, c.ready_at, c.detail = res.reason, res.ready_at, res.detail
                continue
            if res is None:  # eligible, but no attempts or time left
                c.reason, c.ready_at = "not_tried", self._clock.now()
                continue

            attempts += 1
            path.append(model.id)
            timeout = min(ATTEMPT_TIMEOUT_CAP_S, deadline - self._clock.now())
            try:
                result = await asyncio.wait_for(upstream(model, timeout), timeout + 1.0)
            except TimeoutError:
                result = Unavailable("timeout")

            if isinstance(result, Served):
                return RouteServed(model, result.payload, res, path, affinity)
            if isinstance(result, Rejected):
                return RoutePassthrough(model, result.payload, res, path, affinity)
            if isinstance(result, RateLimited):
                cls = classify_429(result.body, result.headers)
                if cls.kind == "unknown":
                    log.warning("unrecognised 429 from %s; cooling down", model.id)
                c.reason, c.detail = "quota", f"429 {cls.kind}"
                self._safely(self._store.on_rate_limited, model, cls, sent_at=res.at)
                self._safely(self._store.refund_rpd, res)  # Google's 429 is the source of truth
            elif isinstance(result, Unavailable):
                c.reason, c.detail = "unavailable", result.detail
                self._safely(self._store.on_unavailable, model)
            elif isinstance(result, ModelNotFound):
                log.error("model %s not found upstream; disabled until restart", model.id)
                self._store.disable(model)
                self._safely(self._store.refund_rpd, res)
                c.reason = "disabled"
                continue
            else:
                raise TypeError(f"unexpected upstream result: {result!r}")
            # Its state now (DESIGN §5.2): a 5xx on a model that is also out of quota counts as quota.
            now = self._clock.now()
            blocked = self._safely(self._store.check, model, input_tokens, failed=_FAILED)
            if blocked is _FAILED:  # state unreadable: don't advertise "ready now"
                c.ready_at = now + STATE_ERROR_RETRY_S
                continue
            c.ready_at = blocked.ready_at if blocked else now
            if blocked and blocked.reason == "quota" and c.reason == "unavailable":
                c.reason, c.detail = "quota", f"{c.detail}; {blocked.detail}"

        return self._final(name, results, path, affinity)

    @staticmethod
    def _safely(fn, *args, failed: Any = None, **kwargs):
        """Run a state update after an upstream call; a SQLite error is logged, not raised,
        so the request can still fall back. Returns `failed` on error."""
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error:
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

    def complete(self, served: RouteServed, prompt_tokens: int | None,
                 tool_call_ids: list[str] | None = None) -> None:
        """After the response finished: settle usage and record tool-call affinity."""
        self._store.settle(served.reservation, prompt_tokens)
        if tool_call_ids:
            try:
                self._affinity.record(tool_call_ids, served.model.id)
            except Exception:  # the response is already sent; losing affinity means "unknown" later
                log.exception("could not record tool-call affinity for %s", served.model.id)
