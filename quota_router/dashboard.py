"""Read-only status page with a per-tab session (DESIGN §17.2 layer 4, §21.4, §21.5).

No cookies: browsers send a cookie to every port of a host, where another local service
could replay it. Signing in returns a session ID that the page keeps in sessionStorage
(scoped to this exact origin, port included, and this tab) and sends in the SESSION_HEADER
header, accepted only on /dashboard/status.json and /dashboard/logout.

Every /dashboard request also passes a stricter origin rule than the global guard
(`dashboard_request_ok`), and gets the security headers in `SECURITY_HEADERS`; both are
applied by app.Guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from .settings import normalize_host

if TYPE_CHECKING:
    from .app import Proxy

log = logging.getLogger("quota_router")

PREFIX = "/dashboard"
SESSION_HEADER = "x-qr-session"
SESSION_TTL_S = 12 * 3600
MAX_SESSIONS = 16
LOGIN_FAILURES_PER_MINUTE = 10
MAX_LOGIN_BODY = 4096
LOGIN_UPLOAD_TIMEOUT_S = 10.0  # a sign-in form is a few bytes; don't hold a slow upload open
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"dashboard.js": "text/javascript; charset=utf-8",
                "dashboard.css": "text/css; charset=utf-8",
                "favicon.svg": "image/svg+xml"}

SECURITY_HEADERS = (
    (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; "
                                 b"connect-src 'self'; img-src 'self'; form-action 'none'; "
                                 b"frame-ancestors 'none'; base-uri 'none'"),
    (b"cache-control", b"no-store"),
    # Not `no-referrer`: with it, Chrome sends `Origin: null` on the login form's POST,
    # which the origin rule must refuse. `same-origin` still sends nothing to other sites.
    (b"referrer-policy", b"same-origin"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
)

DEFAULT_PORTS = {"http": 80, "https": 443}


def is_dashboard_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _split_host_port(host: str) -> tuple[str, int | None] | None:
    """`Host` header → (normalized host, port or None). None if the port is malformed."""
    h = host.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return None
        name, rest = h[:end + 1], h[end + 1:]
    elif h.count(":") > 1:  # bare IPv6 address, no port possible
        return normalize_host(h), None
    else:
        name, sep, port = h.partition(":")
        rest = sep + port
    if not rest:
        return name, None
    digits = rest[1:]
    # isascii(): str.isdigit() also accepts characters like "²", which int() rejects.
    if not rest.startswith(":") or not (digits.isascii() and digits.isdigit()) or int(digits) > 65535:
        return None
    return name, int(digits)


def same_origin(origin: str, host: str | None) -> bool:
    """True if `Origin` is exactly this server's own origin, as named by `Host`:
    same host and same port (another app on another localhost port fails)."""
    if not host:
        return False
    try:
        o = urlsplit(origin)
        o_host, o_port = o.hostname, o.port
    except ValueError:
        return False
    if o.scheme not in DEFAULT_PORTS or not o_host or o.path or o.query or o.fragment:
        return False
    hp = _split_host_port(host)
    if hp is None:
        return False
    h_host, h_port = hp
    o_port = o_port if o_port is not None else DEFAULT_PORTS[o.scheme]
    h_port = h_port if h_port is not None else DEFAULT_PORTS[o.scheme]
    return normalize_host(o_host) == h_host and o_port == h_port


def dashboard_request_ok(method: str, headers: Mapping[str, str]) -> bool:
    """The tighter rule for /dashboard (DESIGN §21.4), on top of the global Host guard."""
    origin = headers.get("origin")
    if origin is not None and not same_origin(origin, headers.get("host")):
        return False
    site = headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    if method not in ("GET", "HEAD") and origin is None:
        return False  # every current browser sends Origin on POST
    return True


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class Sessions:
    """In-memory login sessions, so a restart logs browsers out. Keyed by a hash of the
    cookie value; at most MAX_SESSIONS, the oldest dropped first."""

    def __init__(self, now) -> None:
        self._now = now
        self._expiry: OrderedDict[str, float] = OrderedDict()

    def create(self) -> str:
        sid = secrets.token_urlsafe(32)
        self._purge()
        while len(self._expiry) >= MAX_SESSIONS:
            self._expiry.popitem(last=False)
        self._expiry[_digest(sid)] = self._now() + SESSION_TTL_S
        return sid

    def valid(self, sid: str | None) -> bool:
        if not sid:
            return False
        key = _digest(sid)
        exp = self._expiry.get(key)
        if exp is None:
            return False
        if exp <= self._now():
            del self._expiry[key]
            return False
        return True

    def drop(self, sid: str | None) -> None:
        if sid:
            self._expiry.pop(_digest(sid), None)

    def _purge(self) -> None:
        now = self._now()
        for key in [k for k, exp in self._expiry.items() if exp <= now]:
            del self._expiry[key]

    def __len__(self) -> int:
        return len(self._expiry)


class LoginThrottle:
    """After LOGIN_FAILURES_PER_MINUTE failed logins within a minute (counted from the
    first failure), every login is refused until that minute ends."""

    def __init__(self, now) -> None:
        self._now = now
        self._window_start = 0.0
        self._failures = 0

    def _expire(self, now: float) -> None:
        if self._failures and now - self._window_start >= 60:
            self._failures = 0

    def blocked_for(self) -> float:
        """Seconds until logins are accepted again; 0 if they are now."""
        now = self._now()
        self._expire(now)
        if self._failures >= LOGIN_FAILURES_PER_MINUTE:
            return max(0.0, self._window_start + 60 - now)
        return 0.0

    def failed(self) -> None:
        now = self._now()
        self._expire(now)
        if self._failures == 0:
            self._window_start = now  # the minute starts at the first failure
        self._failures += 1


WRONG_TOKEN = "Wrong token."
THROTTLED = "Too many failed sign-ins. Wait a minute and try again."
BAD_FORM = "The sign-in request was not understood. Reload the page and try again."
SLOW_FORM = "The sign-in request took too long to arrive. Try again."


def add_dashboard(app: FastAPI, p: "Proxy") -> None:
    sessions = Sessions(p.clock.now)
    throttle = LoginThrottle(p.clock.now)
    p.dashboard_sessions = sessions  # for tests
    p.dashboard_throttle = throttle

    def signed_in(request: Request) -> bool:
        return sessions.valid(request.headers.get(SESSION_HEADER))

    def fail(request: Request, status: int, reason: str, message: str) -> JSONResponse:
        p.record(request_id=uuid.uuid4().hex[:16], route="unknown", status=status, reason=reason,
                 started=time.monotonic(), http_path=request.url.path)
        return JSONResponse({"error": message}, status_code=status)

    def throttled(request: Request) -> JSONResponse | None:
        wait = throttle.blocked_for()
        if wait <= 0:
            return None
        resp = fail(request, 429, "login_throttled", THROTTLED)
        resp.headers["Retry-After"] = str(max(1, int(wait + 0.999)))
        return resp

    async def read_body(request: Request) -> bytes | JSONResponse:
        body = b""
        try:
            async with asyncio.timeout(LOGIN_UPLOAD_TIMEOUT_S):
                async for chunk in request.stream():
                    body += chunk
                    if len(body) > MAX_LOGIN_BODY:
                        return fail(request, 413, "bad_request", BAD_FORM)
        except TimeoutError:
            return fail(request, 408, "bad_request", SLOW_FORM)
        except ClientDisconnect:
            return fail(request, 499, "client_disconnected", BAD_FORM)
        return body

    @app.get(PREFIX)
    async def dashboard():
        # The page holds no data (status.json does), so it is served to anyone who passes the
        # guard; its script shows the sign-in form until it has a session.
        return Response((STATIC_DIR / "dashboard.html").read_bytes(), media_type="text/html; charset=utf-8")

    @app.get(PREFIX + "/status.json")
    async def status_json(request: Request):
        if not signed_in(request):
            return JSONResponse({"error": "signed out"}, status_code=401)
        return p.status_view()

    @app.post(PREFIX + "/login")
    async def login(request: Request):
        if (resp := throttled(request)) is not None:  # refuse early, before reading the body
            return resp
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        # Check again: other sign-ins may have failed while this body was arriving. No await
        # between here and the comparison, so concurrent requests can't slip past a lockout.
        if (resp := throttled(request)) is not None:
            return resp
        # JSON only: a cross-origin page can't send it without a CORS preflight, which fails.
        if ctype != "application/json":
            return fail(request, 400, "bad_request", BAD_FORM)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        token = data.get("token") if isinstance(data, dict) else None
        # surrogatepass: JSON allows a lone surrogate ("\ud800"), which strict UTF-8 can't encode.
        token = token.strip().encode("utf-8", "surrogatepass") if isinstance(token, str) else b""
        if not token or not hmac.compare_digest(token, p.settings.proxy_token.encode("utf-8")):
            throttle.failed()
            p.auth_failures += 1
            return fail(request, 401, "unauthorized", WRONG_TOKEN)
        sessions.drop(request.headers.get(SESSION_HEADER))
        sid = sessions.create()
        log.info("dashboard sign-in")
        return JSONResponse({"session": sid, "expires_in": SESSION_TTL_S})

    @app.post(PREFIX + "/logout")
    async def logout(request: Request):
        sessions.drop(request.headers.get(SESSION_HEADER))
        return Response(status_code=204)

    # Registered last so the fixed paths above win.
    @app.get(PREFIX + "/{name}")
    async def static(name: str):
        media = STATIC_FILES.get(name)
        if media is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response((STATIC_DIR / name).read_bytes(), media_type=media)
