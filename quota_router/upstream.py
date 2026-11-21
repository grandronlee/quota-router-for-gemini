"""One call to Google's OpenAI-compatible endpoint, mapped to an `UpstreamResult` (DESIGN §20.4).

Contract with the router: every exception becomes a result; only cancellation
propagates. A response or stream that isn't handed back inside `Served` is closed
before the call returns. Each opened stream is also registered with the request's
`HandleSet`, so the HTTP layer can close anything left open (for example if the
call is cancelled just as it returns).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx

from .classify import classify_429, parse_error_body
from .config import ModelConfig
from .router import ModelNotFound, RateLimited, Rejected, Served, Unavailable, UpstreamResult
from .scrub import Scrubber

log = logging.getLogger("quota_router")

CONNECT_TIMEOUT_S = 10.0
CHUNK_TIMEOUT_S = 120.0  # between stream chunks, once streaming has started (DESIGN §9)
CAPTURE_MAX_FILES = 50
CAPTURE_MAX_BYTES = 64 * 1024
_UNAVAILABLE_STATUSES = {500, 502, 503, 504}
# Raised before any byte of the request is written, so Google never saw it (DESIGN §5 step 4).
# Read and write errors are not here: the request may have reached Google.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def make_client(transport: httpx.AsyncBaseTransport | None = None, *,
                trust_env: bool = True) -> httpx.AsyncClient:
    """`trust_env` lets HTTP(S)_PROXY settings apply. Pass False for a plain-http upstream:
    a proxy would see the API key in cleartext (over https it only sees a CONNECT tunnel)."""
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=CHUNK_TIMEOUT_S, write=30.0, pool=10.0)
    return httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False,
                             trust_env=trust_env)


# ---- SSE -------------------------------------------------------------------

@dataclass(frozen=True)
class SSEEvent:
    raw: bytes            # the event as received, re-terminated with a blank line
    data: str | None      # joined `data:` lines; None for comments and keep-alives

    @property
    def is_done(self) -> bool:
        return self.data is not None and self.data.strip() == "[DONE]"

    def json(self) -> Any:
        if self.data is None or self.is_done:
            return None
        try:
            return json.loads(self.data)
        except ValueError:
            return None

    def error(self) -> dict | None:
        """The `error` object if this is an error event, else None."""
        obj = self.json()
        return parse_error_body(obj) if isinstance(obj, (dict, list)) else None


def _event(lines: list[str]) -> SSEEvent:
    data = [ln[5:].removeprefix(" ") for ln in lines if ln.startswith("data:")]
    return SSEEvent(("\n".join(lines) + "\n\n").encode("utf-8"), "\n".join(data) if data else None)


async def sse_events(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if lines:
                yield _event(lines)
                lines = []
            continue
        lines.append(line)
    if lines:
        yield _event(lines)


# ---- results handed to the HTTP layer --------------------------------------

@dataclass
class Body:
    """A complete upstream response (non-stream success, or an error to pass through)."""
    status: int
    content: bytes
    content_type: str


@dataclass
class Stream:
    """An open upstream stream whose first event has been read."""
    response: httpx.Response
    events: AsyncIterator[SSEEvent]
    first: SSEEvent
    closed: bool = False

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.events.aclose()  # type: ignore[attr-defined]
        except Exception:
            pass
        await self.response.aclose()


@dataclass
class HandleSet:
    """Streams opened for one client request; whatever isn't served is closed. Also notes
    each model called, so a cancelled request can still say which model it reserved."""
    streams: list[Stream] = field(default_factory=list)
    attempted: list[str] = field(default_factory=list)

    async def close_except(self, keep: Any = None) -> None:
        for s in self.streams:
            if s is not keep:
                try:
                    await s.aclose()
                except Exception:
                    log.debug("closing an upstream stream failed", exc_info=True)


# ---- 429 captures ----------------------------------------------------------

class Captures:
    """Saves scrubbed 429 bodies under data/429 (DESIGN §20.7, §20.10)."""

    def __init__(self, directory: Path, scrubber: Scrubber, clock: Callable[[], float]) -> None:
        self._dir = directory
        self._scrub = scrubber
        self._clock = clock
        try:
            self._count = len(list(directory.glob("*.json"))) if directory.is_dir() else 0
        except OSError:
            self._count = CAPTURE_MAX_FILES
        self._warned = False

    def save(self, model_id: str, status: int, headers: httpx.Headers | dict, body: bytes) -> None:
        if self._count >= CAPTURE_MAX_FILES:
            if not self._warned:
                self._warned = True
                log.warning("429 capture folder is full (%d files); not saving more", CAPTURE_MAX_FILES)
            return
        clean = self._scrub.capture_body(body)  # redact the whole body first, then truncate
        truncated = len(clean) > CAPTURE_MAX_BYTES
        clean = clean[:CAPTURE_MAX_BYTES]
        try:
            parsed: Any = json.loads(clean)
        except ValueError:
            parsed = clean
        now = self._clock()
        record = {
            "at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
            "model": model_id, "status": status,
            "headers": {k: self._scrub.capture(v) for k, v in headers.items() if k.lower() == "retry-after"},
            "body": parsed, "truncated": truncated,
        }
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_model = "".join(c if c.isalnum() or c in "-._" else "_" for c in model_id)
        try:
            # Owner-only, whatever the umask: captures can name the project and its limits.
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._dir, 0o700)
            path = self._dir / f"{stamp}-{safe_model}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(record, indent=2, ensure_ascii=False))
            except BaseException:
                path.unlink(missing_ok=True)  # no partial capture files
                raise
            self._count += 1
        except OSError:
            log.warning("could not save a 429 capture", exc_info=True)


# ---- the upstream call ------------------------------------------------------

def error_event_kind(err: dict) -> tuple[str, int | None]:
    """Classify an SSE error event's `error` object: ("rate_limited" | "unavailable" | "client",
    code). Used for the first event (DESIGN §20.4) and for later ones (§8)."""
    code = err.get("code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    if code == 429 or str(err.get("status", "")).upper() == "RESOURCE_EXHAUSTED":
        return "rate_limited", code
    if code is None or code >= 500 or code < 400:
        return "unavailable", code
    return "client", code


def _is_model_not_found(body: bytes) -> bool:
    err = parse_error_body(body)
    if not err:
        return False
    return str(err.get("status", "")).upper() == "NOT_FOUND" and "model" in str(err.get("message", "")).lower()


class GeminiUpstream:
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str,
                 scrubber: Scrubber, captures: Captures | None = None,
                 on_result: Callable[[ModelConfig, UpstreamResult], None] | None = None) -> None:
        self._client = client
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._scrub = scrubber
        self._captures = captures
        self._on_result = on_result

    def bind(self, body: dict, stream: bool, handles: HandleSet) -> Callable:
        """The router's `Upstream` callable for one client request."""

        async def call(model: ModelConfig, timeout_s: float, *,
                       body: dict | None = None, _client_body: dict = body) -> UpstreamResult:
            handles.attempted.append(model.id)
            sent = _client_body if body is None else body  # a handed-off turn (DESIGN §16.8)
            result = await self._call(sent, stream, handles, model, timeout_s)
            if self._on_result:
                try:
                    self._on_result(model, result)
                except Exception:
                    log.exception("upstream result hook failed")
            return result

        return call

    def _payload(self, body: dict, stream: bool, model: ModelConfig) -> bytes:
        payload = {**body, "model": model.id}
        if stream:
            opts = body.get("stream_options")
            payload["stream_options"] = {**(opts if isinstance(opts, dict) else {}), "include_usage": True}
        try:
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            # A lone surrogate (e.g. an emoji cut in half by the client) can't be UTF-8 encoded.
            # Send it escaped, as the client did, and let Google decide; a failure here must
            # never look like the model being unavailable.
            return json.dumps(payload, ensure_ascii=True).encode("ascii")

    async def _call(self, body: dict, stream: bool, handles: HandleSet,
                    model: ModelConfig, timeout_s: float) -> UpstreamResult:
        response: httpx.Response | None = None
        handed_over = False
        sending = False  # once True, Google may have received the request
        try:
            async with asyncio.timeout(max(0.0, timeout_s)):
                request = self._client.build_request(
                    "POST", self._url, content=self._payload(body, stream, model),
                    headers={"Authorization": f"Bearer {self._key}",
                             "Content-Type": "application/json",
                             "User-Agent": "quota-router-for-gemini"})
                sending = True
                response = await self._client.send(request, stream=True)
                status = response.status_code
                if 200 <= status < 300 and stream:
                    events = sse_events(response)
                    first = await anext(events, None)
                    while first is not None and first.data is None:
                        # Comments and heartbeats commit nothing: the first data event decides
                        # whether this model serves the request or routing continues.
                        first = await anext(events, None)
                    if first is None:
                        return Unavailable("empty stream")
                    err = first.error()
                    if err is not None:  # nothing has reached the client yet (DESIGN §20.4)
                        return self._first_event_error(model, err, first)
                    s = Stream(response, events, first)
                    handles.streams.append(s)
                    handed_over = True
                    return Served(s)
                content = await response.aread()
        except TimeoutError:
            return Unavailable("timeout", not_sent=not sending)
        except httpx.TimeoutException as e:
            return Unavailable("timeout", not_sent=not sending or isinstance(e, _NOT_SENT_ERRORS))
        except httpx.HTTPError as e:
            return Unavailable(f"error: {type(e).__name__}",
                               not_sent=not sending or isinstance(e, _NOT_SENT_ERRORS))
        except Exception as e:  # noqa: BLE001 - contract: every exception becomes a result
            log.exception("unexpected error calling %s", model.id)
            return Unavailable(f"error: {type(e).__name__}", not_sent=not sending)
        finally:
            if response is not None and not handed_over:
                await _close_quietly(response)

        ctype = response.headers.get("content-type", "application/json")
        if 200 <= status < 300:
            return Served(Body(status, content, ctype))
        if status == 429:
            if self._captures:
                self._captures.save(model.id, status, response.headers, content)
            return RateLimited(content, dict(response.headers))
        if status in _UNAVAILABLE_STATUSES:
            return Unavailable(f"http {status}")
        if status == 404 and _is_model_not_found(content):
            return ModelNotFound(content)
        return Rejected(Body(status, self._scrub.bytes(content), ctype))

    def _first_event_error(self, model: ModelConfig, err: dict, event: SSEEvent) -> UpstreamResult:
        kind, code = error_event_kind(err)
        raw = (event.data or "").encode("utf-8")
        if kind == "rate_limited":
            if self._captures:
                self._captures.save(model.id, 429, {}, raw)
            return RateLimited(raw, None)
        if kind == "unavailable":
            return Unavailable(f"stream error event {code}")
        if code == 404 and _is_model_not_found(raw):
            return ModelNotFound(raw)
        return Rejected(Body(code, self._scrub.bytes(raw), "application/json"))


async def _close_quietly(response: httpx.Response) -> None:
    try:
        await response.aclose()
    except Exception:  # a failed close must not mask the real outcome; cancellation still propagates
        log.debug("closing an upstream response failed", exc_info=True)
