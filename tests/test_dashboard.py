"""Dashboard sign-in, per-tab session, origin rule and headers (DESIGN §21.4, §21.5)."""

from __future__ import annotations

import asyncio
import json

import pytest

from apphelpers import AUTH, LOCAL, TOKEN, make_proxy, running
from quota_router.clock import FakeClock
from quota_router.dashboard import (LOGIN_FAILURES_PER_MINUTE, MAX_SESSIONS, SESSION_HEADER, SESSION_TTL_S,
                                    dashboard_request_ok, same_origin)
from helpers import T0

SAME = {"Origin": LOCAL}


def login(c, token=TOKEN, headers=None):
    return c.post("/dashboard/login", json={"token": token}, headers={**SAME, **(headers or {})})


def session(c, token=TOKEN):
    r = login(c, token)
    assert r.status_code == 200, r.text
    return r.json()["session"]


def hdr(sid):
    return {SESSION_HEADER: sid}


def metric(c, name):
    for line in c.get("/metrics", headers=AUTH).text.splitlines():
        if line.startswith(name):
            yield line


# ---- origin rule -----------------------------------------------------------

@pytest.mark.parametrize("origin,host,ok", [
    ("http://localhost:4000", "localhost:4000", True),
    ("http://LOCALHOST:4000", "localhost:4000", True),
    ("http://127.0.0.1:4000", "127.0.0.1:4000", True),
    ("http://[::1]:4000", "[::1]:4000", True),
    ("https://box.tail1234.ts.net", "box.tail1234.ts.net", True),       # default port 443 both sides
    ("https://box.tail1234.ts.net", "box.tail1234.ts.net:443", True),
    ("http://localhost", "localhost:80", True),
    ("http://localhost:5173", "localhost:4000", False),                  # another local web app
    ("http://127.0.0.1:4000", "localhost:4000", False),                  # different host name
    ("https://localhost:4000", "localhost", False),                      # port 4000 vs 443
    ("http://localhost:4000/x", "localhost:4000", False),
    ("null", "localhost:4000", False),
    ("file://", "localhost:4000", False),
    ("http://localhost:4000", None, False),
    ("http://localhost:4000", "localhost:abc", False),
    ("http://localhost:99999", "localhost:4000", False),
    ("http://localhost:4000", "localhost:", False),                      # empty port
    ("http://localhost:4000", "localhost:\xb2", False),                 # "²".isdigit() is True
    ("http://localhost:4000", "localhost:004000", True),
    ("http://localhost:4000", "localhost:99999", False),
])
def test_same_origin(origin, host, ok):
    assert same_origin(origin, host) is ok


def test_request_rule_fetch_site_and_post_origin():
    h = {"host": "localhost:4000"}
    assert dashboard_request_ok("GET", h)
    assert dashboard_request_ok("GET", {**h, "sec-fetch-site": "none"})
    assert dashboard_request_ok("GET", {**h, "sec-fetch-site": "same-origin"})
    assert not dashboard_request_ok("GET", {**h, "sec-fetch-site": "same-site"})   # localhost:5173 <img>
    assert not dashboard_request_ok("GET", {**h, "sec-fetch-site": "cross-site"})
    assert not dashboard_request_ok("POST", h)                                      # POST needs Origin
    assert dashboard_request_ok("POST", {**h, "origin": LOCAL})


# ---- sign-in and sessions ----------------------------------------------------

def test_page_holds_no_data_and_needs_no_bearer():
    with running(make_proxy()) as c:
        r = c.get("/dashboard")
    assert r.status_code == 200
    assert 'id="signin-form"' in r.text and "model-a" not in r.text


def test_sign_in_returns_a_session_and_never_sets_a_cookie():
    """Second review, finding 1: a cookie would also go to other ports of this host."""
    p = make_proxy()
    with running(p) as c:
        r = login(c)
        sid = r.json()["session"]
        data = c.get("/dashboard/status.json", headers=hdr(sid))
        status = c.get("/status", headers=AUTH)
        without = c.get("/dashboard/status.json")
    assert r.status_code == 200 and r.json()["expires_in"] == SESSION_TTL_S
    assert "set-cookie" not in r.headers and not c.cookies
    assert len(sid) >= 40 and TOKEN not in r.text
    assert data.status_code == 200 and data.json().keys() == status.json().keys()
    assert set(data.json()["models"]) == {"model-a", "model-b"}
    assert without.status_code == 401


def test_wrong_token_is_401_and_counted():
    p = make_proxy()
    with running(p) as c:
        bad = login(c, "wrong-token-wrong-token")
        empty = login(c, "")
        not_str = c.post("/dashboard/login", json={"token": 123}, headers=SAME)
        failures = list(metric(c, "qr_auth_failures_total"))
    assert [r.status_code for r in (bad, empty, not_str)] == [401, 401, 401]
    assert bad.json() == {"error": "Wrong token."} and "session" not in bad.text
    assert failures == ["qr_auth_failures_total 3"]


def test_sign_in_needs_a_json_body():
    with running(make_proxy()) as c:
        form = c.post("/dashboard/login", content=f"token={TOKEN}", headers={
            **SAME, "Content-Type": "application/x-www-form-urlencoded"})
        broken = c.post("/dashboard/login", content=b"{nope", headers={**SAME, "Content-Type": "application/json"})
        big = c.post("/dashboard/login", content=json.dumps({"token": "x" * 5000}),
                     headers={**SAME, "Content-Type": "application/json"})
    assert form.status_code == 400 and "session" not in form.text  # a plain cross-site form can't sign in
    assert broken.status_code == 401
    assert big.status_code == 413


def test_session_never_authorizes_api_paths():
    with running(make_proxy()) as c:
        sid = session(c)
        results = [c.get(path, headers=hdr(sid)).status_code for path in ("/status", "/metrics", "/v1/models")]
        bearer = [c.get(path, headers={"Authorization": f"Bearer {sid}"}).status_code
                  for path in ("/status", "/metrics")]
        chat = c.post("/v1/chat/completions", json={"model": "x", "messages": []}, headers=hdr(sid))
    assert results == [401, 401, 401] and bearer == [401, 401] and chat.status_code == 401


def test_bearer_token_does_not_open_status_json():
    with running(make_proxy()) as c:
        assert c.get("/dashboard/status.json", headers=AUTH).status_code == 401


def test_logout_ends_the_session():
    with running(make_proxy()) as c:
        sid = session(c)
        out = c.post("/dashboard/logout", headers={**SAME, **hdr(sid)})
        again = c.get("/dashboard/status.json", headers=hdr(sid))
    assert out.status_code == 204 and again.status_code == 401


def test_session_expires_after_ttl_and_restart_signs_out():
    clock = FakeClock(T0)
    p = make_proxy(clock=clock)
    with running(p) as c:
        sid = session(c)
        clock.advance(SESSION_TTL_S - 1)
        assert c.get("/dashboard/status.json", headers=hdr(sid)).status_code == 200
        clock.advance(2)
        assert c.get("/dashboard/status.json", headers=hdr(sid)).status_code == 401
        sid = session(c)
    with running(make_proxy(clock=clock)) as c2:  # a new process holds no sessions
        assert c2.get("/dashboard/status.json", headers=hdr(sid)).status_code == 401


def test_at_most_max_sessions_oldest_dropped():
    p = make_proxy()
    with running(p) as c:
        first = session(c)
        for _ in range(MAX_SESSIONS):
            session(c)
        assert len(p.dashboard_sessions) == MAX_SESSIONS
        assert c.get("/dashboard/status.json", headers=hdr(first)).status_code == 401
        assert c.get("/dashboard/status.json", headers=hdr("x" * 43)).status_code == 401


def test_signing_in_again_from_a_tab_replaces_its_old_session():
    p = make_proxy()
    with running(p) as c:
        old = session(c)
        r = login(c, headers=hdr(old))
        assert len(p.dashboard_sessions) == 1
        assert c.get("/dashboard/status.json", headers=hdr(old)).status_code == 401
        assert c.get("/dashboard/status.json", headers=hdr(r.json()["session"])).status_code == 200


def test_failed_logins_are_throttled_for_the_rest_of_the_minute():
    clock = FakeClock(T0)
    p = make_proxy(clock=clock)
    with running(p) as c:
        for _ in range(LOGIN_FAILURES_PER_MINUTE):
            assert login(c, "wrong-token-wrong-token").status_code == 401
        clock.advance(30)
        blocked = login(c)  # even the right token
        clock.advance(30)
        allowed = login(c)
        reasons = c.get("/status", headers=AUTH).json()["requests"]["by_reason"]
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "30"
    assert "session" not in blocked.text
    assert allowed.status_code == 200
    assert reasons["login_throttled"] == 1 and reasons["unauthorized"] == LOGIN_FAILURES_PER_MINUTE


def test_throttle_minute_starts_at_the_first_failure():
    clock = FakeClock(T0)
    p = make_proxy(clock=clock)
    with running(p) as c:
        assert login(c).status_code == 200          # successes don't start the window
        clock.advance(50)
        for _ in range(LOGIN_FAILURES_PER_MINUTE):  # first failure at +50 s
            login(c, "wrong-token-wrong-token")
        clock.advance(15)                           # +65 s: an old window would have reset
        blocked = login(c)
        clock.advance(45)                           # +110 s: 60 s after the first failure
        allowed = login(c)
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "45"
    assert allowed.status_code == 200


# ---- origin rule and headers through the app ----------------------------------

def test_other_local_port_or_cross_site_is_403():
    with running(make_proxy()) as c:
        other_port = login(c, headers={"Origin": "http://localhost:5173"})
        no_origin = c.post("/dashboard/login", json={"token": TOKEN})
        sid = session(c)
        same_site = c.get("/dashboard/status.json", headers={**hdr(sid), "Sec-Fetch-Site": "same-site"})
        cross = c.get("/dashboard/status.json", headers={**hdr(sid), "Sec-Fetch-Site": "cross-site"})
        logout_other = c.post("/dashboard/logout", headers={**hdr(sid), "Origin": "http://localhost:5173"})
        bad_host = c.get("/dashboard", headers={"Host": "attacker.test:4000"})
        ok_ = c.get("/dashboard/status.json", headers={**hdr(sid), "Sec-Fetch-Site": "same-origin"})
    for r in (other_port, no_origin, same_site, cross, logout_other, bad_host):
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_host"
    assert "session" not in other_port.text
    assert ok_.status_code == 200  # the logout from another origin did nothing


def test_security_headers_on_every_dashboard_response():
    with running(make_proxy()) as c:
        responses = [c.get("/dashboard"), c.get("/dashboard/dashboard.js"), c.get("/dashboard/dashboard.css"),
                     c.get("/dashboard/status.json"), c.get("/dashboard/nope"),
                     c.get("/dashboard", headers={"Origin": "http://localhost:5173"}), login(c, "wrong"),
                     login(c)]
        api = c.get("/status", headers=AUTH)
    for r in responses:
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp, r.request.url
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert not any(h.lower().startswith("access-control-") for h in r.headers)
        assert "set-cookie" not in r.headers
    assert responses[1].headers["content-type"].startswith("text/javascript")
    assert responses[4].status_code == 404
    assert "content-security-policy" not in api.headers


def test_lookalike_paths_still_need_the_bearer_token():
    with running(make_proxy()) as c:
        r = c.get("/dashboardx")
        r2 = c.get("/Dashboard")
    assert r.status_code == 401 and r2.status_code == 401


def test_static_names_are_a_fixed_list():
    with running(make_proxy()) as c:
        trav = c.get("/dashboard/%2e%2e%2fconfig.py")
        py = c.get("/dashboard/dashboard.py")
    assert trav.status_code == 404 and py.status_code == 404


# ---- sign-ins in flight (second review, finding 2) --------------------------------

def _asgi_login(p, receive):
    """POST /dashboard/login through the whole app, with a caller-controlled body."""
    from quota_router.app import create_app
    sent: list[dict] = []

    async def scenario():
        app = create_app(p)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/dashboard/login", "raw_path": b"/dashboard/login",
                 "query_string": b"", "root_path": "", "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 4000),
                 "headers": [(b"host", b"localhost:4000"), (b"origin", LOCAL.encode()),
                             (b"content-type", b"application/json")]}
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), 5)

    asyncio.run(scenario())
    return sent


def test_lockout_reached_while_a_body_is_arriving_still_refuses_it():
    p = make_proxy()
    msgs = [{"type": "http.request", "body": b'{"tok', "more_body": True},
            {"type": "http.request", "body": f'en": "{TOKEN}"}}'.encode(), "more_body": False}]

    async def receive():
        if len(msgs) == 1:  # between the two chunks, other sign-ins fail and start a lockout
            for _ in range(LOGIN_FAILURES_PER_MINUTE):
                p.dashboard_throttle.failed()
        return msgs.pop(0)

    sent = _asgi_login(p, receive)
    assert sent[0]["status"] == 429  # the right token, but the lockout began before it was checked
    assert b"session" not in b"".join(m.get("body", b"") for m in sent)


def test_slow_sign_in_upload_times_out(monkeypatch):
    import quota_router.dashboard as d
    monkeypatch.setattr(d, "LOGIN_UPLOAD_TIMEOUT_S", 0.2)
    p = make_proxy()
    msgs = [{"type": "http.request", "body": b'{"token": "', "more_body": True}]

    async def receive():
        if msgs:
            return msgs.pop(0)
        await asyncio.sleep(30)  # the rest never arrives
        return {"type": "http.disconnect"}

    sent = _asgi_login(p, receive)
    assert sent[0]["status"] == 408
    assert p.auth_failures == 0


def test_lone_surrogate_token_is_a_counted_401_not_a_500():
    """Third review, finding 1: json.loads accepts "\\ud800", which strict UTF-8 can't encode."""
    p = make_proxy()
    with running(p) as c:
        r = c.post("/dashboard/login", content=b'{"token": "\\ud800"}',
                   headers={**SAME, "Content-Type": "application/json"})
    assert r.status_code == 401 and p.auth_failures == 1
    assert "content-security-policy" in r.headers
