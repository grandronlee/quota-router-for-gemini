"""HTTP layer: FastAPI app, auth, streaming and observability (DESIGN §5, §8, §10, §11, §17, §20).

All handlers are `async def`, so the SQLite connection is only used from the
event-loop thread. One uvicorn worker (DESIGN §7).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from .affinity import AffinityStore
from .classify import classify_429
from .clock import Clock, SystemClock, next_pt_midnight
from .config import PARSED_BASE_FACTOR, PARSED_OVERHEAD, PARSED_WIDTH_FACTOR, Config, ModelConfig
from .dashboard import SECURITY_HEADERS, add_dashboard, dashboard_request_ok, is_dashboard_path
from .db import connect
from .logs import REQUEST_LOGGER
from .metrics import OUTCOMES, STATUSES, Metrics, RequestStats, bounded
from .quota import QuotaStore
from .router import (Attempt, ModelNotFound, RateLimited, Rejected, RouteFailed, RoutePassthrough,
                     RouteServed, Router, Served, Unavailable, UpstreamResult, estimate_input_tokens)
from .scrub import Scrubber
from .settings import Settings, normalize_host
from .turns import TurnLog, TurnRef
from .upstream import (Body, Captures, GeminiUpstream, HandleSet, SSEEvent, Stream, error_event_kind,
                       make_client)

log = logging.getLogger("quota_router")
request_log = logging.getLogger(REQUEST_LOGGER)

PRUNE_INTERVAL_S = 3600.0
UNGUARDED_AUTH_PATHS = {"/healthz"}  # still behind the Host/Origin guard
# /dashboard paths skip the bearer token too: they use a per-tab session header and a stricter
# origin rule instead (dashboard.py, DESIGN §21.4).


def _iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="seconds")


def openai_error(status: int, code: str, message: str, etype: str = "invalid_request_error",
                 headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": etype, "code": code, "param": None}},
                        status_code=status, headers=headers)


def usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    """(prompt_tokens, output tokens). Output = total − prompt, so reasoning counts (DESIGN §7)."""
    if not isinstance(usage, dict):
        return None, None

    def _int(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    prompt, total, completion = (_int(usage.get(k)) for k in ("prompt_tokens", "total_tokens", "completion_tokens"))
    output = total - prompt if prompt is not None and total is not None and total >= prompt else completion
    return prompt, output


def tool_call_ids(obj: Any, part: str) -> list[str]:
    """IDs from choices[].<part>.tool_calls[].id, where part is "message" or "delta"."""
    ids: list[str] = []
    choices = obj.get("choices") if isinstance(obj, dict) else None
    for ch in choices if isinstance(choices, list) else []:
        inner = ch.get(part) if isinstance(ch, dict) else None
        calls = inner.get("tool_calls") if isinstance(inner, dict) else None
        for tc in calls if isinstance(calls, list) else []:
            tc_id = tc.get("id") if isinstance(tc, dict) else None
            if isinstance(tc_id, str) and tc_id:
                ids.append(tc_id)
    return ids


def attempt_outcome(result: UpstreamResult) -> str:
    if isinstance(result, Served):
        return "ok"
    if isinstance(result, RateLimited):
        return "429_" + classify_429(result.body, result.headers).kind
    if isinstance(result, Unavailable):
        d = result.detail
        if d == "timeout":
            return "timeout"
        code = d.rsplit(" ", 1)[-1]
        if d.startswith("http ") or (d.startswith("stream error event ") and code.isdigit() and int(code) >= 500):
            return "5xx"
        return "error"  # connect errors, an empty stream, an error event without a code
    if isinstance(result, ModelNotFound):
        return "404"
    if isinstance(result, Rejected):
        status = getattr(result.payload, "status", 0)
        return "4xx" if 400 <= status < 500 else "error"
    return "error"


def model_state(snap: dict) -> str:
    if snap["disabled"]:
        return "disabled"
    if snap["exhausted_until"] or (snap["rpd_limit"] is not None and snap["rpd_used"] >= snap["rpd_limit"]):
        return "exhausted"
    if snap["cooldown_until"]:
        return "cooldown"
    return "available"


class Proxy:
    """Everything one running proxy holds. Created before the event loop starts, so a bad
    config or database fails at startup with a clear message."""

    def __init__(self, settings: Settings, config: Config, *, clock: Clock | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 conn: sqlite3.Connection | None = None) -> None:
        self.settings = settings
        self.config = config
        self.clock = clock or SystemClock()
        self.transport = transport
        self.conn = conn if conn is not None else connect(config.db_path, check_same_thread=False)
        self.store = QuotaStore(config.models.values(), self.conn, self.clock)
        self.affinity = AffinityStore(self.conn, self.clock)
        self.turns = TurnLog(self.conn, self.clock)
        self.router = Router(config, self.store, self.affinity, self.clock)
        self.router.on_handoff = lambda src, dst, result: self.metrics.inc(
            "qr_handoff_total", **{"from": src, "to": dst, "result": result})
        self.scrubber = Scrubber([settings.gemini_api_key, settings.proxy_token])
        self.metrics = Metrics()
        self.stats = RequestStats(self.clock.now())
        self.auth_failures = 0
        self.body_budget = BodyBudget(config.max_inflight_parsed_bytes)
        capture_dir = Path(config.db_path).parent / "429" if config.db_path != ":memory:" else None
        self.captures = Captures(capture_dir, self.scrubber, self.clock.now) if capture_dir else None
        self.client: httpx.AsyncClient | None = None
        self.upstream: GeminiUpstream | None = None
        self.dashboard_sessions = None  # set by add_dashboard

    def status_view(self) -> dict:
        """Body of /status and /dashboard/status.json (DESIGN §17.2 layer 3, §20.8)."""
        now = self.clock.now()
        snap = self.store.snapshot()
        models = {}
        for model_id, s in snap.items():
            models[model_id] = {**s, "state": model_state(s),
                                "cooldown_until": _iso(s["cooldown_until"]),
                                "exhausted_until": _iso(s["exhausted_until"])}
        reset = next_pt_midnight(now)
        view = self.stats.view(now)
        return {
            "now": _iso(now), "next_pt_reset": _iso(reset), "next_pt_reset_in_s": math.ceil(reset - now),
            "routes": self.config.routes, "models": models,
            "requests": {"since": _iso(view["since"]), "by_status": view["by_status"],
                         "by_reason": view["by_reason"]},
            "recent_errors": [{"at": _iso(e.at), "route": e.route, "status": e.status, "reason": e.reason}
                              for e in reversed(self.stats.recent)],
            "turns_today": self._turns_today(),
            "state_errors": self.router.state_errors,
        }

    def _turns_today(self) -> dict[str, int] | None:
        try:
            return self.turns.today_by_route()
        except sqlite3.Error:
            self.router.state_errors += 1
            log.exception("could not read the turn log")
            return None

    # ---- helpers ---------------------------------------------------------

    def route_label(self, name: Any) -> str:
        if isinstance(name, str) and (name in self.config.routes or self.config.model_by_id(name)):
            return name
        return "unknown"

    def on_attempt(self, model: ModelConfig, result: UpstreamResult) -> None:
        self.metrics.inc("qr_upstream_attempts_total", model=model.id,
                         outcome=bounded(attempt_outcome(result), OUTCOMES, "error"))

    def record(self, *, request_id: str, route: str, status: int, reason: str, stream: bool = False,
               started: float, served_model: str | None = None, path: list[str] | None = None,
               affinity: str | None = None, input_estimate: int | None = None,
               prompt_tokens: int | None = None, output_tokens: int | None = None,
               first_byte_s: float | None = None, http_path: str | None = None,
               handoff_from: str | None = None, attempts: list[Attempt] | None = None) -> None:
        """One request's metrics, /status counts and its JSON log line (DESIGN §17.2)."""
        now = self.clock.now()
        self.metrics.inc("qr_requests_total", route=route, status=bounded(status, STATUSES), reason=reason)
        self.stats.record(now, route, status, reason)
        if first_byte_s is not None:
            self.metrics.observe_first_byte(first_byte_s, route)
        if served_model:
            if prompt_tokens is not None:
                self.metrics.inc("qr_tokens_total", prompt_tokens, model=served_model, kind="input")
            if output_tokens is not None:
                self.metrics.inc("qr_tokens_total", output_tokens, model=served_model, kind="output")
        fields = {
            "event": "request", "request_id": request_id, "route": route, "stream": stream,
            "status": status, "reason": reason, "served_model": served_model,
            "fallback_path": path or [], "affinity": affinity, "input_estimate": input_estimate,
            "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "first_byte_ms": None if first_byte_s is None else round(first_byte_s * 1000),
            # One entry per upstream call: model, outcome, status, ms; never error text (DESIGN §22.4).
            "attempts": [a.log() for a in attempts or []],
        }
        if http_path is not None:
            fields["path"] = http_path[:200]
        if handoff_from is not None:
            fields["handoff_from"] = handoff_from
        request_log.info("request", extra={"fields": fields})

    def note_turn(self, turn: TurnRef | None, *, route: str, status: int, served_model: str | None = None,
                  prompt_tokens: int | None = None, output_tokens: int | None = None,
                  handoff_from: str | None = None) -> None:
        """Write the request's decision-log row (DESIGN §16.8 1d). Never raises: a failed
        write is logged and counted, and never fails the request (§5.2)."""
        if turn is None:
            return
        try:
            self.turns.note(turn, route=route, status=status, served_model=served_model,
                            prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                            handoff=handoff_from is not None)
        except Exception:
            self.router.state_errors += 1
            log.exception("could not write the turn log")

    def prune_once(self) -> None:
        try:
            self.store.prune()
            self.affinity.prune()
            self.turns.prune()
        except sqlite3.Error:
            self.router.state_errors += 1
            log.exception("housekeeping prune failed; will retry")

    async def housekeeping(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_INTERVAL_S)
            try:
                self.prune_once()
            except Exception:  # keep the task alive; prune_once already handles SQLite errors
                log.exception("housekeeping failed; will retry")

    # ---- guard and auth ----------------------------------------------------

    def host_allowed(self, host: str | None) -> bool:
        return bool(host) and normalize_host(host) in self.settings.allowed_hosts

    def origin_allowed(self, origin: str) -> bool:
        try:
            parts = urlsplit(origin)
            hostname = parts.hostname
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not hostname:
            return False  # includes "null" from sandboxed frames and file:// pages
        return normalize_host(hostname) in self.settings.allowed_hosts

    def token_ok(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(token.strip().encode("utf-8"), self.settings.proxy_token.encode("utf-8"))


def _with_security_headers(send):
    async def wrapped(message) -> None:
        if message["type"] == "http.response.start":
            names = {k.lower() for k, _ in message.get("headers", [])}
            extra = [(k, v) for k, v in SECURITY_HEADERS if k not in names]
            message = {**message, "headers": list(message.get("headers", [])) + extra}
        await send(message)
    return wrapped


class Guard:
    """Pure ASGI middleware: Host/Origin check on every path, then the bearer token
    everywhere except /healthz (DESIGN §20.3). Never sends CORS headers."""

    def __init__(self, app, proxy: Proxy) -> None:
        self.app = app
        self.proxy = proxy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        p = self.proxy
        started = time.monotonic()
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        path = scope.get("path", "")
        dashboard = is_dashboard_path(path)
        if dashboard:
            send = _with_security_headers(send)
        if (not p.host_allowed(headers.get("host")) or (origin is not None and not p.origin_allowed(origin))
                or (dashboard and not dashboard_request_ok(scope.get("method", ""), headers))):
            p.record(request_id=uuid.uuid4().hex[:16], route="unknown", status=403, reason="forbidden_host",
                     started=started, http_path=path)
            resp = openai_error(403, "forbidden_host", "requests must come from this machine "
                                "(Host and Origin must be localhost)", "permission_error")
            return await resp(scope, receive, send)
        if not dashboard and path not in UNGUARDED_AUTH_PATHS and not p.token_ok(headers.get("authorization")):
            p.auth_failures += 1
            p.record(request_id=uuid.uuid4().hex[:16], route="unknown", status=401, reason="unauthorized",
                     started=started, http_path=path)
            resp = openai_error(401, "invalid_api_key", "missing or wrong bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
            return await resp(scope, receive, send)
        await self.app(scope, receive, send)


class StreamSession:
    """Forwards one served stream and settles it exactly once (DESIGN §8, §20.5)."""

    def __init__(self, proxy: Proxy, served: RouteServed, stream: Stream, *, request_id: str,
                 route: str, started: float, input_estimate: int,
                 attempts: list[Attempt] | None = None) -> None:
        self.p = proxy
        self.attempts = attempts
        self.served = served
        self.stream = stream
        self.request_id = request_id
        self.route = route
        self.started = started
        self.input_estimate = input_estimate
        self.reason = "served"
        self.done = False           # saw [DONE] from upstream
        self.settled = False        # complete() has run with usage (or for the final time)
        self.finished = False
        self.seen_ids: set[str] = set()
        self.pending_ids: list[str] = []
        self.prompt_tokens: int | None = None
        self.output_tokens: int | None = None
        self.first_byte_s: float | None = None
        # How the served call ended, for its `attempts` entry (DESIGN §22.4); None: still open.
        self.outcome: tuple[str, int | None] | None = None
        self.status = served.attempt.status if served.attempt else None

    def _complete(self, prompt_tokens: int | None) -> None:
        ids, self.pending_ids = self.pending_ids, []
        if not self.settled:
            # The turn row is written before any of this response's tool calls are recorded,
            # so a follow-up (which finds the turn only through them) always writes after it
            # (DESIGN §16.8 1d). The first settle comes with usage, or at the end without it.
            self.p.note_turn(self.served.turn, route=self.route, status=200,
                             served_model=self.served.model.id, prompt_tokens=self.prompt_tokens,
                             output_tokens=self.output_tokens, handoff_from=self.served.handoff_from)
        self.p.router.complete(self.served, prompt_tokens, ids or None)
        self.settled = True

    def _complete_if_needed(self) -> None:
        """Before the last bytes of any ending (router contract): settle with the estimate
        if usage never came, and record tool-call IDs still pending."""
        if not self.settled or self.pending_ids:
            self._complete(None)

    def _error_event(self, message: str) -> bytes:
        err = {"error": {"message": self.p.scrubber.text(message), "type": "upstream_error",
                         "code": "stream_error", "param": None}}
        return b"data: " + json.dumps(err).encode("utf-8") + b"\n\n"

    def _record_stream_error(self, err: dict, ev: SSEEvent) -> None:
        """A 429 or 5xx after streaming started still updates the model's state (DESIGN §8)."""
        kind, code = error_event_kind(err)
        self.outcome = ("stream_error", code)
        raw = (ev.data or "").encode("utf-8")
        if kind == "rate_limited" and self.p.captures:
            try:
                self.p.captures.save(self.served.model.id, 429, {}, raw)
            except Exception:  # a capture must never stop the error event reaching the client
                log.exception("could not save a mid-stream 429 capture")
        self.p.router.stream_error(self.served, kind, raw, code)  # never raises

    def _handle(self, ev: SSEEvent) -> tuple[bytes | None, bool]:
        """Process one upstream event; returns (bytes to send, stop)."""
        if ev.is_done:
            self._complete_if_needed()  # before [DONE] reaches the client (router contract)
            self.done = True
            self.outcome = ("stream_complete", self.status)
            self.p.router.stream_done(self.served)  # the model recovered (DESIGN §22.1); never raises
            return ev.raw, True
        err = ev.error()
        if err is not None:  # mid-stream upstream error: one error event, no [DONE]
            self.reason = "stream_error"
            self._complete_if_needed()
            self._record_stream_error(err, ev)
            payload = self.p.scrubber.obj({"error": err})
            return b"data: " + self.p.scrubber.text(json.dumps(payload)).encode("utf-8") + b"\n\n", True
        obj = ev.json()
        if isinstance(obj, dict):
            for i in tool_call_ids(obj, "delta"):
                if i not in self.seen_ids:
                    self.seen_ids.add(i)
                    self.pending_ids.append(i)
            usage = obj.get("usage")
            if isinstance(usage, dict) and not self.settled:
                self.prompt_tokens, self.output_tokens = usage_tokens(usage)
                self._complete(self.prompt_tokens)  # before the usage chunk is forwarded
            elif self.settled and self.pending_ids:
                self._complete(None)  # IDs that arrived after usage
        return ev.raw, False

    async def body(self):
        ev: SSEEvent | None = self.stream.first
        while True:
            out, stop = self._handle(ev)
            if out:
                if self.first_byte_s is None:
                    self.first_byte_s = time.monotonic() - self.started
                yield out
            if stop:
                return
            try:
                ev = await anext(self.stream.events, None)
            except Exception as e:  # httpx errors and anything unexpected; cancellation propagates
                if not isinstance(e, (httpx.HTTPError, httpx.StreamError)):
                    log.exception("reading the upstream stream failed")
                self.reason = "stream_error"
                self.outcome = ("stream_incomplete", self.status)  # neutral (DESIGN §22.1)
                self._complete_if_needed()
                yield self._error_event(f"upstream stream failed: {type(e).__name__}")
                return
            if ev is None:
                self.reason = "stream_error"
                self.outcome = ("stream_incomplete", self.status)  # neutral (DESIGN §22.1)
                self._complete_if_needed()
                yield self._error_event("upstream stream ended before [DONE]")
                return

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if not self.done and self.reason == "served":
            self.reason = "client_disconnected"
        if self.served.attempt is not None:
            outcome, status = self.outcome or ("client_cancelled", self.status)
            self.served.attempt.finish(outcome, status)
        try:
            self._complete_if_needed()  # estimate kept
        except Exception:
            log.exception("settling a stream failed")
        try:
            await self.stream.aclose()
        except Exception:
            log.debug("closing the upstream stream failed", exc_info=True)
        self.p.record(request_id=self.request_id, route=self.route, status=200, reason=self.reason,
                      stream=True, started=self.started, served_model=self.served.model.id,
                      path=self.served.path, affinity=self.served.affinity,
                      input_estimate=self.input_estimate, prompt_tokens=self.prompt_tokens,
                      output_tokens=self.output_tokens, first_byte_s=self.first_byte_s,
                      handoff_from=self.served.handoff_from, attempts=self.attempts)


class ManagedStream(StreamingResponse):
    """A StreamingResponse that always finishes its session: settles usage, records affinity,
    closes the upstream stream and writes the log line, even if the client went away before
    the first chunk."""

    def __init__(self, session: StreamSession, headers: dict[str, str]) -> None:
        super().__init__(session.body(), media_type="text/event-stream", headers=headers)
        self.session = session
        self.lease: BodyLease | None = None  # the request body's share of the budget

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            pass
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    try:
                        await self.body_iterator.aclose()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    await self.session.finish()
                finally:
                    if self.lease is not None:
                        self.lease.release()


def _response_headers(request_id: str, path: list[str], affinity: str | None,
                      served_model: str | None = None, handoff_from: str | None = None) -> dict[str, str]:
    h = {"x-request-id": request_id}
    if served_model:
        h["x-served-model"] = served_model
    if path:
        h["x-fallback-path"] = ",".join(path)
    if affinity:
        h["x-turn-affinity"] = affinity
    if handoff_from:
        h["x-handoff-from"] = handoff_from
    return h


async def _wait_for_disconnect(request: Request) -> None:
    """Returns when the client disconnects. Called only after the body has been read."""
    while True:
        message = await request.receive()
        if message.get("type") != "http.request":  # http.disconnect, or anything unexpected
            return
        await asyncio.sleep(0)  # never spin, even with a server whose receive() doesn't wait


class BodyBudget:
    """Bytes of request bodies in use at once, shared by all requests (DESIGN §20.3).
    Only touched from the event loop, and never across an await, so no lock is needed."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def lease(self) -> "BodyLease":
        return BodyLease(self)


class BodyLease:
    """One request's share of the budget; `release` gives it all back, once."""

    def __init__(self, budget: BodyBudget) -> None:
        self._budget = budget
        self.held = 0
        self.released = False

    @property
    def limit(self) -> int:
        return self._budget.limit

    def grow_to(self, size: int) -> bool:
        """Hold at least `size` bytes. False (holding no more than before) if over the budget."""
        extra = size - self.held
        if extra <= 0:
            return True
        if self.released or self._budget.used + extra > self._budget.limit:
            return False
        self._budget.used += extra
        self.held = size
        return True

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._budget.used -= self.held
            self.held = 0


# Python stores a string at its widest character's width; json.loads decodes the whole
# document first, so one such character sets the width of that copy (found in review).
_WIDTH_4 = re.compile(rb'[\xf0-\xf4]|\\u[dD][89abAB]')  # astral: UTF-8 lead or surrogate escape
_WIDTH_2 = re.compile(rb'[\xc4-\xef]|\\u(?:0[1-9a-fA-F]|[1-9a-fA-F])')  # U+0100..U+FFFF


def _char_width(raw: bytes) -> int:
    if _WIDTH_4.search(raw):
        return 4
    return 2 if _WIDTH_2.search(raw) else 1


# A string, or an unterminated one running to the end of the window. It always matches at the
# first quote, so a window with an unclosed string is still one linear pass (a pattern that can
# fail there is retried from every later quote: quadratic, found in review). re.S: `.` takes a
# raw newline. Group 1 is the closing quote, so a string cut off at the window's end becomes
# one unpaired quote in the stripped output: that is how the scan knows it ended inside one.
_JSON_STRING_CLOSED = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*(?:(")|\\?\Z)', re.S)
# Inside a string: up to its closing quote, a lone backslash at the window's end, or the end.
_STRING_REST = re.compile(rb'[^"\\]*(?:\\.[^"\\]*)*', re.S)
# Scan window. Scratch is at most ~90 × this (a run of `"",`, or a string of escapes): ~370 KB.
ESTIMATE_WINDOW = 4096
_BACKSLASH = 0x5C


class BodyEstimate:
    """Upper estimate of the memory a body takes while its request is in progress (DESIGN
    §20.3), built chunk by chunk as the body arrives: (3 + 3 × character width) × its size,
    plus a per-value cost counted on the structure outside strings, which is what makes JSON
    of many tiny values 10–20× its size once parsed. Calibrated against tracemalloc
    (2026-11-21).

    Each chunk is scanned in windows of ESTIMATE_WINDOW bytes, so the scan's scratch memory
    is bounded whatever the body's size or shape. It never awaits, so one runs at a time.
    The result equals stripping the joined body's strings in one pass."""

    def __init__(self) -> None:
        self.size = 0
        self._in_string = False
        self._escaped = False  # inside a string, the next byte is escaped by a lone backslash
        self._tail = b""  # last bytes seen, for a \u escape split across chunks
        self._width = 1
        self._commas = self._opens = self._objects = self._colons = 0
        self.head = b""  # the first 4 bytes, which decide the encoding json.loads would use

    @property
    def value(self) -> int:
        values = self._commas + self._opens + self._objects + 1
        factor = PARSED_BASE_FACTOR + PARSED_WIDTH_FACTOR * (self._width - 1)
        return (factor * self.size + PARSED_OVERHEAD
                + 80 * values + 160 * self._objects + 64 * self._colons)

    def feed(self, chunk: bytes) -> None:
        n = len(chunk)
        self.size += n
        if len(self.head) < 4:
            self.head = (self.head + chunk[:4])[:4]
        if self._width < 4:  # the patterns need at most 4 bytes, so 5 of carry is enough
            self._width = max(self._width, _char_width(self._tail + chunk[:6]), _char_width(chunk))
        self._tail = (self._tail + chunk[-5:])[-5:]
        pos = 0
        while pos < n:
            end = min(pos + ESTIMATE_WINDOW, n)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                    pos += 1
                    continue
                stop = _STRING_REST.match(chunk, pos, end).end()  # bounded to the window
                if stop == end:
                    pos = end
                elif chunk[stop] == _BACKSLASH:  # lone, as the window's last byte
                    self._escaped = True
                    pos = end
                else:  # the closing quote
                    self._in_string = False
                    pos = stop + 1
                continue
            window = chunk[pos:end]
            s = _JSON_STRING_CLOSED.sub(rb'"\1', window)  # commas and braces inside strings are not values
            self._commas += s.count(b",")
            self._opens += s.count(b"[")
            self._objects += s.count(b"{")
            self._colons += s.count(b":")
            if s.count(b'"') % 2:  # the window ends inside a string
                self._in_string = True
                # the match took backslashes in pairs; an odd run escapes the next byte
                self._escaped = (len(window) - len(window.rstrip(b"\\"))) % 2 == 1
            pos = end


def parsed_memory_estimate(raw: bytes) -> int:
    """BodyEstimate of a whole body at once."""
    estimate = BodyEstimate()
    estimate.feed(raw)
    return estimate.value


class BodyBuffer:
    """The body as it arrives, in blocks of at least BLOCK bytes, so a client that sends it a
    few bytes at a time can't make the per-chunk objects outweigh what is charged (found in
    review). Small chunks are gathered in one bytearray and moved into a block once it holds
    BLOCK bytes; larger chunks are kept as they are. `bound(size)` is an upper bound on the
    memory the buffer keeps for `size` bytes, so it can be charged before they are kept."""

    BLOCK = 65536
    PER_BLOCK = 64  # a bytes object's header and its list slot, with room to spare

    def __init__(self) -> None:
        self._blocks: list[bytes] = []
        self._pending = bytearray()

    @classmethod
    def bound(cls, size: int) -> int:
        # A block holds at least BLOCK bytes, except a short one flushed just before a large
        # chunk, which is itself a block of at least BLOCK: so at most 2 × (size // BLOCK) + 1
        # blocks (found in review: alternating 1-byte and 64 KB chunks). The bytearray holds
        # under 2 × BLOCK and grows by at most 1/8 + 7 bytes of spare room. 512: the buffer's
        # own objects (instance, list, bytearray), about 350 bytes measured.
        return (size + min(size // 8, cls.BLOCK // 4) + 7
                + cls.PER_BLOCK * (2 * (size // cls.BLOCK) + 1) + 512)

    def add(self, chunk: bytes) -> None:
        if len(chunk) >= self.BLOCK:
            self._flush()
            self._blocks.append(chunk)
            return
        self._pending += chunk
        if len(self._pending) >= self.BLOCK:
            self._flush()

    def _flush(self) -> None:
        if self._pending:
            self._blocks.append(bytes(self._pending))
            self._pending = bytearray()

    def take(self) -> bytes:
        """The whole body. With one block that is the block itself; otherwise a joined copy,
        which with the blocks is 2× the size, covered by the estimate (at least 6×)."""
        self._flush()
        blocks, self._blocks = self._blocks, []
        return b"".join(blocks)


def _not_utf8(head: bytes) -> bool:
    # json.loads also accepts UTF-16 and UTF-32, but the estimate reads the body as UTF-8
    # bytes: a UTF-16 character can hold a quote byte, hiding structure (found in review).
    return json.detect_encoding(head) not in ("utf-8", "utf-8-sig")


def _busy() -> JSONResponse:
    return openai_error(503, "server_busy", "too many large requests in progress; retry shortly",
                        "server_error", {"Retry-After": "1"})


def _not_utf8_error() -> JSONResponse:
    return openai_error(400, "invalid_json", "request body must be UTF-8 JSON")


async def _read_json(request: Request, limit: int, lease: BodyLease) -> dict | Response:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return openai_error(413, "request_too_large", f"request body over {limit} bytes")
    if declared and declared.isdigit() and not lease.grow_to(BodyBuffer.bound(int(declared))):
        return _busy()  # before buffering anything
    buffer = BodyBuffer()
    estimate = BodyEstimate()
    encoding_checked = False
    try:
        async for chunk in request.stream():
            if estimate.size + len(chunk) > limit:
                return openai_error(413, "request_too_large", f"request body over {limit} bytes")
            estimate.feed(chunk)
            if not encoding_checked and len(estimate.head) == 4:
                if _not_utf8(estimate.head):
                    return _not_utf8_error()
                encoding_checked = True
            # The estimate only grows, so once over the whole budget the body could never fit:
            # 413 before any 503 (DESIGN §20.3).
            if estimate.value > lease.limit:
                return openai_error(413, "request_too_large",
                                    "request body would take too much memory once parsed (DESIGN §20.3)")
            if not lease.grow_to(BodyBuffer.bound(estimate.size)):  # before the chunk is kept
                return _busy()
            buffer.add(chunk)
    except ClientDisconnect:  # gone while uploading; the caller releases the bytes held so far
        return Response(status_code=499)
    if not encoding_checked and _not_utf8(estimate.head):  # a body under 4 bytes
        return _not_utf8_error()
    # Charged before joining and parsing. The estimate is at least 6× the size, so it also
    # covers the join, which holds the blocks and the joined copy at once (found in review).
    if not lease.grow_to(estimate.value):
        return _busy()
    raw = buffer.take()
    del buffer
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return openai_error(400, "invalid_json", "request body is not valid JSON")
    if not isinstance(body, dict):
        return openai_error(400, "invalid_json", "request body must be a JSON object")
    return body


def create_app(proxy: Proxy) -> FastAPI:
    p = proxy

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        p.client = make_client(p.transport, trust_env=p.config.upstream_base_url.startswith("https://"))
        p.upstream = GeminiUpstream(p.client, p.config.upstream_base_url, p.settings.gemini_api_key,
                                    p.scrubber, p.captures, p.on_attempt)
        task = asyncio.create_task(p.housekeeping())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await p.client.aclose()
            finally:
                p.conn.close()  # folds the WAL back into the database (DESIGN §20.10)

    app = FastAPI(title="quota-router-for-gemini", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, proxy=p)
    add_dashboard(app, p)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models():
        data = [{"id": r, "object": "model", "created": 0, "owned_by": "quota-router"} for r in p.config.routes]
        data += [{"id": m.id, "object": "model", "created": 0, "owned_by": "google"}
                 for m in p.config.models.values() if not p.store.is_disabled(m)]
        return {"object": "list", "data": data}

    @app.get("/status")
    async def status():
        return p.status_view()

    @app.get("/metrics")
    async def metrics():
        snap = p.store.snapshot()
        gauges: dict[str, list] = {"qr_rpd_used": [], "qr_rpd_limit": [], "qr_rpm_used": [],
                                   "qr_tpm_input_used": [], "qr_model_state": []}
        for model_id, s in snap.items():
            lbl = {"model": model_id}
            gauges["qr_rpd_used"].append((lbl, s["rpd_used"]))
            if s["rpd_limit"] is not None:
                gauges["qr_rpd_limit"].append((lbl, s["rpd_limit"]))
            gauges["qr_rpm_used"].append((lbl, s["rpm_used"]))
            gauges["qr_tpm_input_used"].append((lbl, s["tpm_input_used"]))
            state = model_state(s)
            for st in ("available", "cooldown", "exhausted", "disabled"):
                gauges["qr_model_state"].append(({"model": model_id, "state": st}, 1 if st == state else 0))
        text = p.metrics.render(gauges, {
            "qr_auth_failures_total": p.auth_failures,
            "qr_state_errors_total": p.router.state_errors,
            "qr_tool_call_id_reused_total": p.router.tool_call_ids_reused,
        })
        return Response(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        lease = p.body_budget.lease()
        handed_over = False
        try:
            response = await _chat(request, lease)
            if isinstance(response, ManagedStream):
                response.lease = lease  # released when the stream ends (DESIGN §20.3)
                handed_over = True
            return response
        finally:
            if not handed_over:
                lease.release()

    async def _chat(request: Request, lease: BodyLease):
        started = time.monotonic()
        arrived = p.clock.now()
        request_id = uuid.uuid4().hex[:16]
        try:  # the request deadline covers the upload too (DESIGN §9); routing gets what's left
            async with asyncio.timeout(p.config.request_deadline_s):
                body = await _read_json(request, p.config.max_body_bytes, lease)
        except TimeoutError:
            body = openai_error(408, "request_timeout", "request body not received within the deadline")
        if isinstance(body, Response):
            reason = {499: "client_disconnected", 503: "busy"}.get(body.status_code, "bad_request")
            p.record(request_id=request_id, route="unknown", status=body.status_code, reason=reason,
                     started=started)
            body.headers["x-request-id"] = request_id
            return body
        route = p.route_label(body.get("model"))
        stream = body.get("stream") is True
        estimate = estimate_input_tokens(body)
        handles = HandleSet()
        attempts: list[Attempt] = []  # filled as calls start, so a cancelled request logs them too
        assert p.upstream is not None
        routing = asyncio.ensure_future(
            p.router.route(body, p.upstream.bind(body, stream, handles), started_at=arrived,
                           attempts=attempts))
        # DESIGN §8: if the client goes away before the response starts, stop routing (no
        # more attempts or fallbacks for nobody). uvicorn doesn't cancel the handler itself.
        watcher = asyncio.ensure_future(_wait_for_disconnect(request))
        try:
            await asyncio.wait({routing, watcher}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # the handler itself was cancelled
            for t in (routing, watcher):
                t.cancel()
            with anyio.CancelScope(shield=True):
                await handles.close_except(None)
            raise
        watcher.cancel()
        if not routing.done():
            routing.cancel()
            try:
                await routing
            except (asyncio.CancelledError, Exception):
                pass
            await handles.close_except(None)
            # The model being called keeps its reservation; name it, since rpd_used went up.
            p.record(request_id=request_id, route=route, status=499, reason="client_disconnected",
                     stream=stream, started=started, path=handles.attempted, input_estimate=estimate,
                     attempts=attempts)
            return Response(status_code=499)
        try:
            outcome = routing.result()
        except Exception:
            await handles.close_except(None)
            log.exception("routing failed")
            p.record(request_id=request_id, route=route, status=500, reason="internal_error",
                     stream=stream, started=started, input_estimate=estimate, attempts=attempts)
            return openai_error(500, "internal_error", "internal proxy error", "server_error",
                                {"x-request-id": request_id})
        served_payload = outcome.payload if isinstance(outcome, RouteServed) else None
        await handles.close_except(served_payload)

        if isinstance(outcome, RouteFailed):
            headers = _response_headers(request_id, outcome.path, outcome.affinity,
                                        handoff_from=outcome.handoff_from)
            if outcome.retry_after_s is not None:
                headers["Retry-After"] = str(outcome.retry_after_s)
            reason = {429: "quota", 400: "context_length_exceeded", 404: "model_not_found"}.get(outcome.status)
            if outcome.code == "no_enabled_model":
                reason = "no_enabled_model"
            reason = reason or "unavailable"
            p.record(request_id=request_id, route=route, status=outcome.status, reason=reason, stream=stream,
                     started=started, path=outcome.path, affinity=outcome.affinity, input_estimate=estimate,
                     handoff_from=outcome.handoff_from, attempts=attempts)
            p.note_turn(outcome.turn, route=route, status=outcome.status, handoff_from=outcome.handoff_from)
            return JSONResponse(p.scrubber.obj(outcome.error_body()),
                                status_code=outcome.status, headers=headers)

        if isinstance(outcome, RoutePassthrough):
            b: Body = outcome.payload
            headers = _response_headers(request_id, outcome.path, outcome.affinity, outcome.model.id,
                                        outcome.handoff_from)
            if outcome.rewritten and b.status == 400:
                # Likely a placeholder signature the model won't accept (DESIGN §16.8): returned
                # unchanged, never retried elsewhere. The fix is to remove it from handoff_targets.
                log.error("HTTP 400 from %s on a tool turn sent with placeholder signatures (%s); "
                          "if it is about a thought signature, remove %s from handoff_targets",
                          outcome.model.id,
                          f"handed off from {outcome.handoff_from}" if outcome.handoff_from
                          else "mixed turn", outcome.model.key)
            p.record(request_id=request_id, route=route, status=b.status, reason="passthrough", stream=stream,
                     started=started, served_model=outcome.model.id, path=outcome.path,
                     affinity=outcome.affinity, input_estimate=estimate, handoff_from=outcome.handoff_from,
                     attempts=attempts)
            p.note_turn(outcome.turn, route=route, status=b.status, handoff_from=outcome.handoff_from)
            headers["content-type"] = b.content_type
            return Response(b.content, status_code=b.status, headers=headers)

        assert isinstance(outcome, RouteServed)
        headers = _response_headers(request_id, outcome.path, outcome.affinity, outcome.model.id,
                                    outcome.handoff_from)
        payload = outcome.payload
        if isinstance(payload, Stream):
            session = StreamSession(p, outcome, payload, request_id=request_id, route=route,
                                    started=started, input_estimate=estimate, attempts=attempts)
            headers.update({"cache-control": "no-cache", "x-accel-buffering": "no"})
            return ManagedStream(session, headers)

        b = payload
        prompt_tokens = output_tokens = None
        ids: list[str] = []
        try:
            obj = json.loads(b.content)
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict):
            prompt_tokens, output_tokens = usage_tokens(obj.get("usage"))
            ids = tool_call_ids(obj, "message")
        # The turn row before the tool calls that let a follow-up find it (DESIGN §16.8 1d).
        p.note_turn(outcome.turn, route=route, status=b.status, served_model=outcome.model.id,
                    prompt_tokens=prompt_tokens, output_tokens=output_tokens, handoff_from=outcome.handoff_from)
        p.router.complete(outcome, prompt_tokens, ids or None)  # before the response is returned
        first_byte = time.monotonic() - started
        p.record(request_id=request_id, route=route, status=b.status, reason="served", stream=stream,
                 started=started, served_model=outcome.model.id, path=outcome.path,
                 affinity=outcome.affinity, input_estimate=estimate, prompt_tokens=prompt_tokens,
                 output_tokens=output_tokens, first_byte_s=first_byte, handoff_from=outcome.handoff_from,
                 attempts=attempts)
        headers["content-type"] = b.content_type
        return Response(b.content, status_code=b.status, headers=headers)

    return app
