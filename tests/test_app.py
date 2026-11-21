"""HTTP layer (PLAN Phase 2): fallback, streaming, auth, guard, headers, observability."""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from apphelpers import (AUTH, KEY, TOKEN, Chunks, Upstream, captured_logs, chat_body, chunk, completion,
                        error_body, make_proxy, mocked, ok, rate_limited, running, sse, streamed, unavailable)
from helpers import model_dict
from quota_router.metrics import OUTCOMES, REASONS, STATES, STATUSES
from quota_router.router import RouteServed


def post(client, body, headers=None, **kw):
    return client.post("/v1/chat/completions", json=body, headers={**AUTH, **(headers or {})}, **kw)


def sse_events(text: str) -> list[str]:
    return [blk[len("data: "):] for blk in text.split("\n\n") if blk.startswith("data: ")]


# ---- fallback (non-stream) -------------------------------------------------

def test_429_falls_back_to_next_model():
    up = Upstream({"model-a": [rate_limited("minute")]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "from model-b"
    assert r.headers["x-served-model"] == "model-b"
    assert r.headers["x-fallback-path"] == "model-a,model-b"
    assert up.models == ["model-a", "model-b"]


def test_400_is_returned_unchanged_without_fallback():
    body = error_body(400, "INVALID_ARGUMENT", "Function call is missing a thought_signature")
    up = Upstream({"model-a": [httpx.Response(400, json=body)]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body())
    assert r.status_code == 400
    assert r.json() == body
    assert r.headers["x-served-model"] == "model-a"
    assert up.models == ["model-a"]


def test_503_falls_back_and_cools_down():
    up = Upstream({"model-a": [unavailable()]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body())
        assert r.headers["x-served-model"] == "model-b"
        blocked = p.store.check(p.config.models["a"], 10)
        assert blocked is not None and blocked.reason == "unavailable"
        r2 = post(c, chat_body())  # model-a is skipped while it cools down
    assert r2.headers["x-fallback-path"] == "model-b"
    assert up.models == ["model-a", "model-b", "model-b"]


def test_connect_error_and_404_model_fall_back():
    nf = httpx.Response(404, json=error_body(404, "NOT_FOUND", "models/model-a is not found"))
    up = Upstream({"model-a": [httpx.ConnectError("boom")], "model-b": [nf]})
    p = make_proxy(models={"a": model_dict("model-a"), "b": model_dict("model-b"), "c": model_dict("model-c")})
    with mocked(up), running(p) as c:
        r = post(c, chat_body())
        assert r.headers["x-served-model"] == "model-c"
        assert p.store.is_disabled(p.config.models["b"])
        ids = [m["id"] for m in c.get("/v1/models", headers=AUTH).json()["data"]]
    assert "model-b" not in ids and "model-c" in ids and "fast" in ids


def test_all_models_out_gives_429_with_retry_after():
    up = Upstream({"model-a": [rate_limited("minute", "20s")], "model-b": [rate_limited("minute", "45s")]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body())
    assert r.status_code == 429
    assert r.headers["retry-after"] == "20"
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert r.headers["x-fallback-path"] == "model-a,model-b"


def test_daily_429_retry_after_points_at_pt_midnight():
    up = Upstream({"model-a": [rate_limited("day", None)]})
    p = make_proxy(models={"a": model_dict("model-a")})
    with mocked(up), running(p) as c:
        r = post(c, chat_body())
    assert r.status_code == 429
    assert r.headers["retry-after"] == str(12 * 3600)  # T0 is 12:00 PT


def test_unknown_route_is_404_and_too_large_is_400():
    p = make_proxy(models={"a": model_dict("model-a", tpm_input=10)})
    up = Upstream()
    with mocked(up), running(p) as c:
        r404 = post(c, chat_body(model="nope"))
        r400 = post(c, chat_body(content="x" * 400))
    assert r404.status_code == 404 and r404.json()["error"]["code"] == "model_not_found"
    assert r400.status_code == 400 and r400.json()["error"]["code"] == "context_length_exceeded"
    assert up.requests == []


def test_upstream_request_rewrites_model_only_and_uses_the_api_key():
    up = Upstream()
    body = chat_body(temperature=0.3, tools=[{"type": "function", "function": {"name": "f"}}])
    with mocked(up), running(make_proxy()) as c:
        post(c, body)
        post(c, chat_body(stream=True, stream_options={"include_usage": False}))
    sent = up.body(0)
    assert sent == {**body, "model": "model-a"}
    assert up.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert TOKEN not in str(up.requests[0].headers)
    assert up.body(1)["stream_options"] == {"include_usage": True}


def test_timeout_before_first_byte_returns_503():
    async def hang(request):
        await asyncio.sleep(5)
        return ok()

    p = make_proxy(models={"a": model_dict("model-a")}, request_deadline_s=0.3)
    with mocked(Upstream({"model-a": [hang]})), running(p) as c:
        r = post(c, chat_body())
        metrics = c.get("/metrics", headers=AUTH).text
    assert r.status_code == 503
    assert int(r.headers["retry-after"]) >= 1
    assert 'qr_upstream_attempts_total{model="model-a",outcome="timeout"} 1' in metrics


# ---- tool-call affinity through HTTP -----------------------------------------

def test_non_stream_tool_call_records_affinity_before_responding():
    up = Upstream({"model-a": [ok(tool_ids=["call-1"])]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        assert p.affinity.lookup(["call-1"]) == {"call-1": "model-a"}
        follow = chat_body()
        follow["messages"] += [{"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]},
                               {"role": "tool", "tool_call_id": "call-1", "content": "done"}]
        r = post(c, follow)
    assert r.headers["x-turn-affinity"] == "pinned"
    assert r.headers["x-served-model"] == "model-a"


def test_reused_tool_call_id_is_counted():
    up = Upstream({"model-a": [ok(tool_ids=["dup"]), ok(tool_ids=["dup"])]})
    p = make_proxy(models={"a": model_dict("model-a")})
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body())
        post(c, chat_body())
        metrics = c.get("/metrics", headers=AUTH).text
    assert "qr_tool_call_id_reused_total 1" in metrics
    assert "already recorded" in logs.getvalue()
    assert "dup" not in logs.getvalue()


# ---- streaming -------------------------------------------------------------

def test_stream_passes_chunks_and_settles_usage():
    usage = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 30}
    up = Upstream({"model-a": [streamed(sse(chunk("Hel"), chunk("lo", usage=usage, finish="stop")))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
        status = c.get("/status", headers=AUTH).json()
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-served-model"] == "model-a"
    events = sse_events(r.text)
    assert events[-1] == "[DONE]"
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "Hel"
    assert status["models"]["model-a"]["tpm_input_used"] == 7  # reported prompt tokens replaced the estimate
    assert status["requests"]["by_reason"] == {"served": 1}


def test_stream_records_affinity_before_the_final_chunk():
    seen: dict = {}
    p = make_proxy()

    def before(n, part):
        if part.startswith(b"data: [DONE]"):  # the proxy asks for [DONE] only after forwarding usage
            seen["at_done"] = p.affinity.lookup(["call-9"])

    usage = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    parts = sse(chunk(tool_ids=["call-9"]), chunk(usage=usage, finish="stop"))
    up = Upstream({"model-a": [httpx.Response(200, headers={"content-type": "text/event-stream"},
                                              stream=Chunks(parts, before=before))]})
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
    assert seen["at_done"] == {"call-9": "model-a"}


def test_stream_429_before_first_byte_falls_back():
    up = Upstream({"model-a": [rate_limited("minute")],
                   "model-b": [streamed(sse(chunk("ok", finish="stop")))]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body(stream=True))
    assert r.status_code == 200
    assert r.headers["x-served-model"] == "model-b"
    assert sse_events(r.text)[-1] == "[DONE]"


def test_stream_error_as_first_event_falls_back():
    first = error_body(503, "UNAVAILABLE", "high demand")
    up = Upstream({"model-a": [streamed(sse(first, done=False))],
                   "model-b": [streamed(sse(chunk("ok", finish="stop")))]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body(stream=True))
    assert r.headers["x-served-model"] == "model-b"
    assert up.models == ["model-a", "model-b"]


def test_mid_stream_error_sends_sse_error_without_fallback_or_done():
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), done=False), error=httpx.ReadError("cut"))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
        status = c.get("/status", headers=AUTH).json()
    events = sse_events(r.text)
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "partial"
    assert json.loads(events[-1])["error"]["code"] == "stream_error"
    assert "[DONE]" not in events
    assert up.models == ["model-a"]
    assert status["requests"]["by_reason"] == {"stream_error": 1}


def test_upstream_error_event_mid_stream_is_forwarded_scrubbed():
    err = error_body(500, "INTERNAL", f"failed for key {KEY}")
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), err, done=False))]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body(stream=True))
        status = c.get("/status", headers=AUTH).json()
    events = sse_events(r.text)
    assert json.loads(events[-1])["error"]["code"] == 500
    assert status["requests"]["by_reason"] == {"stream_error": 1}
    assert KEY not in r.text
    assert "[DONE]" not in events


def test_stream_ending_without_done_is_an_error():
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), done=False))]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body(stream=True))
    assert json.loads(sse_events(r.text)[-1])["error"]["code"] == "stream_error"


def test_stream_session_finishes_when_client_never_reads():
    """Client gone before the first chunk: usage keeps the estimate, the stream is closed,
    affinity is still recorded for IDs seen, and the request is logged."""
    from quota_router.app import StreamSession
    from quota_router.upstream import SSEEvent, Stream

    p = make_proxy()

    class FakeResponse:
        closed = False

        async def aclose(self):
            FakeResponse.closed = True

    async def events():
        yield SSEEvent(b"data: x\n\n", "x")

    first_obj = chunk(tool_ids=["call-z"])
    first = SSEEvent(b"", json.dumps(first_obj))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None)
    stream = Stream(FakeResponse(), events(), first)  # type: ignore[arg-type]
    session = StreamSession(p, served, stream, request_id="r", route="fast", started=0.0, input_estimate=10)

    async def run():
        gen = session.body()
        await gen.__anext__()  # first chunk processed, then the client disconnects
        await gen.aclose()
        await session.finish()
        await session.finish()  # idempotent

    with captured_logs() as logs:
        asyncio.run(run())
    assert FakeResponse.closed and stream.closed
    assert session.reason == "client_disconnected"
    assert p.affinity.lookup(["call-z"]) == {"call-z": "model-a"}
    assert logs.getvalue().count('"event": "request"') == 1


# ---- auth and the browser guard -------------------------------------------

def test_missing_or_wrong_token_is_401():
    with mocked(Upstream()), running(make_proxy()) as c:
        r1 = c.post("/v1/chat/completions", json=chat_body())
        r2 = c.post("/v1/chat/completions", json=chat_body(), headers={"Authorization": "Bearer nope"})
        r3 = c.get("/status")
        r4 = c.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"})
        ok_ = c.get("/healthz")
        m = c.get("/metrics", headers=AUTH).text
    assert [r.status_code for r in (r1, r2, r3, r4)] == [401, 401, 401, 401]
    assert r1.json()["error"]["code"] == "invalid_api_key"
    assert ok_.status_code == 200
    assert "qr_auth_failures_total 4" in m


def test_foreign_host_or_origin_is_403_everywhere():
    with mocked(Upstream()), running(make_proxy()) as c:
        bad_host = c.get("/healthz", headers={"Host": "evil.example"})
        rebinding = c.post("/v1/chat/completions", json=chat_body(), headers={**AUTH, "Host": "attacker.test:4000"})
        bad_origin = c.post("/v1/chat/completions", json=chat_body(),
                            headers={**AUTH, "Origin": "https://evil.example"})
        null_origin = c.get("/healthz", headers={"Origin": "null"})
        local_origin = c.get("/healthz", headers={"Origin": "http://localhost:5173"})
        ipv4 = c.get("/healthz", headers={"Host": "127.0.0.1:9999"})
        ipv6 = c.get("/healthz", headers={"Host": "[::1]:4000"})
    for r in (bad_host, rebinding, bad_origin, null_origin):
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_host"
    for r in (local_origin, ipv4, ipv6):
        assert r.status_code == 200
    for r in (bad_host, local_origin, ipv4):
        assert not any(h.lower().startswith("access-control-") for h in r.headers)


def test_allowed_hosts_extend_the_guard():
    p = make_proxy(allowed_hosts=frozenset({"localhost", "127.0.0.1", "[::1]", "box.tail1234.ts.net"}))
    with mocked(Upstream()), running(p) as c:
        r = c.get("/healthz", headers={"Host": "box.tail1234.ts.net"})
    assert r.status_code == 200


def test_no_cors_preflight_support():
    with mocked(Upstream()), running(make_proxy()) as c:
        r = c.options("/v1/chat/completions", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 403
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


def test_body_limits():
    p = make_proxy(max_body_bytes=200)
    with mocked(Upstream()), running(p) as c:
        big = post(c, chat_body(content="x" * 500))
        bad = c.post("/v1/chat/completions", content=b"{not json", headers={**AUTH, "content-type": "application/json"})
        arr = c.post("/v1/chat/completions", content=b"[1]", headers=AUTH)
    assert big.status_code == 413
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_json"
    assert arr.status_code == 400


# ---- secrets and observability ---------------------------------------------

def test_key_never_appears_in_responses_or_logs():
    leak = error_body(400, "INVALID_ARGUMENT", f"API key not valid: {KEY} (proxy {TOKEN})")
    up = Upstream({"model-a": [httpx.Response(400, json=leak)]})
    with mocked(up), running(make_proxy()) as c, captured_logs() as logs:
        r = post(c, chat_body())
        c.post("/v1/chat/completions", json=chat_body(), headers={"Authorization": f"Bearer {KEY}"})
        logging_text = logs.getvalue()
    assert r.status_code == 400
    assert KEY not in r.text and TOKEN not in r.text
    assert "<redacted>" in r.text
    assert KEY not in logging_text and TOKEN not in logging_text


def test_logs_and_metrics_hold_no_content_and_bounded_labels():
    prompt = "SECRET-PROMPT-TEXT"
    up = Upstream({"model-a": [ok("COMPLETION-TEXT", tool_ids=["call-SECRET-ID"])],
                   "model-b": [streamed(sse(chunk("STREAM-TEXT", tool_ids=["call-STREAM-ID"],
                                                  usage={"prompt_tokens": 5, "total_tokens": 9}),))]})
    p = make_proxy(models={"a": model_dict("model-a"), "b": model_dict("model-b")},
                   routes={"fast": ["a", "b"], "only-b": ["b"]})
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body(content=prompt))
        post(c, chat_body(model="only-b", content=prompt, stream=True))
        post(c, chat_body(model="made-up-route-name", content=prompt))
        c.get("/status")  # 401
        metrics = c.get("/metrics", headers=AUTH).text
        status = c.get("/status", headers=AUTH).text
        text = logs.getvalue()
    for secret in (prompt, "COMPLETION-TEXT", "STREAM-TEXT", "call-SECRET-ID", "call-STREAM-ID"):
        assert secret not in text and secret not in metrics and secret not in status
    lines = [json.loads(ln) for ln in text.splitlines()]
    requests = [ln for ln in lines if ln.get("event") == "request"]
    assert len(requests) == 4
    assert {ln["route"] for ln in requests} == {"fast", "only-b", "unknown"}
    served = next(ln for ln in requests if ln["route"] == "fast")
    assert served["prompt_tokens"] == 8 and served["output_tokens"] == 12  # total − prompt
    allowed = {"route": {"fast", "only-b", "unknown", "model-a", "model-b"},
               "model": {"model-a", "model-b"}, "status": set(STATUSES) | {"other"},
               "reason": set(REASONS), "outcome": set(OUTCOMES), "state": set(STATES),
               "kind": {"input", "output"}, "le": None}
    for name, value in re.findall(r'(\w+)="([^"]*)"', metrics):
        assert name in allowed, name
        if allowed[name] is not None:
            assert value in allowed[name], (name, value)
    assert "made-up-route-name" not in metrics


def test_status_and_metrics_content():
    up = Upstream({"model-a": [rate_limited("day", None)]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        s = c.get("/status", headers=AUTH).json()
        m = c.get("/metrics", headers=AUTH).text
    a = s["models"]["model-a"]
    assert a["state"] == "exhausted" and a["exhausted_until"] is not None and a["rpd_used"] == 0
    assert s["models"]["model-b"]["state"] == "available" and s["models"]["model-b"]["rpd_used"] == 1
    assert s["next_pt_reset_in_s"] == 12 * 3600
    assert s["requests"]["by_status"] == {"200": 1}
    assert s["recent_errors"] == []
    assert 'qr_model_state{model="model-a",state="exhausted"} 1' in m
    assert 'qr_upstream_attempts_total{model="model-a",outcome="429_rpd"} 1' in m
    assert 'qr_requests_total{reason="served",route="fast",status="200"} 1' in m
    assert 'qr_rpd_limit{model="model-a"} 500' in m
    assert 'qr_first_byte_seconds_count{route="fast"} 1' in m


def test_empty_limit_has_no_limit_sample():
    p = make_proxy(models={"a": model_dict("model-a", rpd=None)})
    with mocked(Upstream()), running(p) as c:
        m = c.get("/metrics", headers=AUTH).text
        s = c.get("/status", headers=AUTH).json()
    assert "qr_rpd_limit{" not in m
    assert s["models"]["model-a"]["rpd_limit"] is None


# ---- streams that are opened but not served are closed ------------------------

def _opened_stream_then(outcome_factory):
    """Patch the router so it opens a real upstream stream, then ends some other way."""
    from quota_router.upstream import HandleSet  # noqa: F401  (documented contract)
    parts = sse(chunk("x", finish="stop"))
    body = Chunks(parts)
    up = Upstream({"model-a": [httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)]})
    p = make_proxy()

    async def route(request, upstream, *, started_at=None):
        await upstream(p.config.models["a"], 5.0)  # returns Served(Stream), which the router drops
        return outcome_factory()

    p.router.route = route  # type: ignore[method-assign]
    return p, up, body


def test_unserved_stream_is_closed_when_route_fails():
    from quota_router.router import RouteFailed
    p, up, body = _opened_stream_then(lambda: RouteFailed(503, "upstream_unavailable", "x", 1))
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
    assert r.status_code == 503
    assert body.closed


def test_unserved_stream_is_closed_when_routing_raises():
    def boom():
        raise RuntimeError("bug")
    p, up, body = _opened_stream_then(boom)
    with mocked(up), running(p) as c, captured_logs() as logs:
        r = post(c, chat_body(stream=True))
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert body.closed
    assert '"reason": "internal_error"' in logs.getvalue()


# ---- review fixes (Phase 2 deep review) -------------------------------------

def test_lone_surrogate_is_sent_escaped_and_never_blames_the_model():
    """Review finding 1: a body json.loads accepts but UTF-8 can't encode used to cool down
    every model and spend their RPD without calling Google."""
    up = Upstream()
    p = make_proxy()
    raw = b'{"model":"fast","messages":[{"role":"user","content":"trunc \\ud83d"}]}'
    with mocked(up), running(p) as c:
        r = c.post("/v1/chat/completions", content=raw, headers={**AUTH, "content-type": "application/json"})
        r2 = post(c, chat_body())
        s = c.get("/status", headers=AUTH).json()
    assert r.status_code == 200 and r.headers["x-served-model"] == "model-a"
    assert b"\\ud83d" in up.requests[0].content
    assert r2.status_code == 200
    assert s["models"]["model-a"]["state"] == "available" and s["models"]["model-a"]["rpd_used"] == 2
    assert s["models"]["model-b"]["rpd_used"] == 0


def test_client_disconnect_before_response_stops_routing():
    """Review finding 2 (DESIGN §8): no fallback for a client that has gone."""
    from quota_router.app import create_app

    async def hang(request):
        await asyncio.sleep(10)
        return ok()

    up = Upstream({"model-a": [hang]})
    p = make_proxy()
    body = json.dumps(chat_body()).encode()
    sent: list[dict] = []

    async def scenario():
        app = create_app(p)
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            await asyncio.sleep(0.2)  # the client gives up while model-a thinks
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "root_path": "", "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 4000),
                 "headers": [(b"host", b"localhost:4000"), (b"content-type", b"application/json"),
                             (b"authorization", AUTH["Authorization"].encode())]}
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), 5)

    with mocked(up), captured_logs() as logs:
        asyncio.run(scenario())
    assert up.models == ["model-a"]  # model-b was never tried
    assert sent[0]["status"] == 499
    assert '"reason": "client_disconnected"' in logs.getvalue()


def test_stream_error_ending_records_affinity_before_the_error_event():
    """Review finding 3: complete() must run before the final bytes of every ending."""
    from quota_router.app import StreamSession
    from quota_router.upstream import SSEEvent, Stream

    p = make_proxy()

    class FakeResponse:
        async def aclose(self):
            pass

    async def events():
        raise httpx.ReadError("cut")
        yield  # pragma: no cover

    first = SSEEvent(b"data: x\n\n", json.dumps(chunk(tool_ids=["call-e"])))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None)
    session = StreamSession(p, served, Stream(FakeResponse(), events(), first),  # type: ignore[arg-type]
                            request_id="r", route="fast", started=0.0, input_estimate=10)
    seen = {}

    async def run():
        async for out in session.body():
            if b"stream_error" in out:
                seen["at_error"] = p.affinity.lookup(["call-e"])

    asyncio.run(run())
    assert seen["at_error"] == {"call-e": "model-a"}


def test_attempt_outcomes_are_labelled_precisely():
    """Review finding 4."""
    from quota_router.app import attempt_outcome
    from quota_router.router import Unavailable
    assert attempt_outcome(Unavailable("http 503")) == "5xx"
    assert attempt_outcome(Unavailable("stream error event 503")) == "5xx"
    assert attempt_outcome(Unavailable("stream error event None")) == "error"
    assert attempt_outcome(Unavailable("stream error event 200")) == "error"
    assert attempt_outcome(Unavailable("empty stream")) == "error"
    assert attempt_outcome(Unavailable("error: ConnectError")) == "error"
    assert attempt_outcome(Unavailable("timeout")) == "timeout"


def test_first_event_404_model_not_found_disables_and_falls_back():
    """Review finding 7."""
    nf = error_body(404, "NOT_FOUND", "models/model-a is not found")
    up = Upstream({"model-a": [streamed(sse(nf, done=False))],
                   "model-b": [streamed(sse(chunk("ok", finish="stop")))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
    assert r.headers["x-served-model"] == "model-b"
    assert p.store.is_disabled(p.config.models["a"])
    assert p.store.snapshot()["model-a"]["rpd_used"] == 0


def test_housekeeping_survives_unexpected_errors_and_shutdown_closes_db(monkeypatch):
    """Review finding 6."""
    import sqlite3 as _sqlite3
    import time as _time
    import quota_router.app as appmod
    monkeypatch.setattr(appmod, "PRUNE_INTERVAL_S", 0.01)
    p = make_proxy()
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("unexpected")

    p.prune_once = flaky  # type: ignore[method-assign]
    with mocked(Upstream()), running(p) as c:
        _time.sleep(0.1)
        assert c.get("/healthz").status_code == 200
    assert len(calls) >= 2  # the task kept running after the first failure
    try:
        p.conn.execute("SELECT 1")
        closed = False
    except _sqlite3.ProgrammingError:
        closed = True
    assert closed


def _asgi_post(p, receive, body_headers=None):
    from quota_router.app import create_app
    sent: list[dict] = []

    async def scenario():
        app = create_app(p)

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "root_path": "", "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 4000),
                 "headers": [(b"host", b"localhost:4000"), (b"content-type", b"application/json"),
                             (b"authorization", AUTH["Authorization"].encode())]}
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), 5)

    asyncio.run(scenario())
    return sent


def test_disconnect_log_names_the_model_that_kept_its_reservation():
    """Review nit 1: the 499 line says which model was being called."""
    async def hang(request):
        await asyncio.sleep(10)
        return ok()

    p = make_proxy()
    msgs = [{"type": "http.request", "body": json.dumps(chat_body()).encode(), "more_body": False}]

    async def receive():
        if msgs:
            return msgs.pop(0)
        await asyncio.sleep(0.2)
        return {"type": "http.disconnect"}

    with mocked(Upstream({"model-a": [hang]})), captured_logs() as logs:
        _asgi_post(p, receive)
    line = next(json.loads(ln) for ln in logs.getvalue().splitlines() if '"event": "request"' in ln)
    assert line["status"] == 499 and line["fallback_path"] == ["model-a"]
    assert p.store.snapshot()["model-a"]["rpd_used"] == 1


def test_disconnect_during_upload_is_logged_not_raised():
    """Review nit 3: ClientDisconnect while reading the body."""
    up = Upstream()
    p = make_proxy()
    msgs = [{"type": "http.request", "body": b'{"model": "fa', "more_body": True},
            {"type": "http.disconnect"}]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    with mocked(up), captured_logs() as logs:
        sent = _asgi_post(p, receive)
    assert sent[0]["status"] == 499
    assert '"reason": "client_disconnected"' in logs.getvalue()
    assert "Traceback" not in logs.getvalue()
    assert up.requests == []


# ---- pasted review findings (2026-11-21) -------------------------------------

def test_passthrough_hides_a_unicode_escaped_key():
    """Finding 2 through HTTP: the client decodes \\u0041 back into the key."""
    escaped = KEY.replace("A", "\\u0041", 1).encode()
    raw = b'{"error": {"code": 400, "message": "API key not valid: ' + escaped + b'", "status": "INVALID_ARGUMENT"}}'
    up = Upstream({"model-a": [httpx.Response(400, content=raw, headers={"content-type": "application/json"})]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body())
    assert r.status_code == 400
    assert KEY not in r.json()["error"]["message"]


def test_heartbeat_before_an_error_event_still_falls_back():
    """Finding 4: a comment must not commit the response to a model."""
    err = json.dumps(error_body(429, "RESOURCE_EXHAUSTED", "quota exceeded")).encode()
    parts = [b": keepalive\n\n", b"data: " + err + b"\n\n"]
    up = Upstream({"model-a": [streamed(parts)], "model-b": [streamed(sse(chunk("ok", finish="stop")))]})
    with mocked(up), running(make_proxy()) as c:
        r = post(c, chat_body(stream=True))
    assert r.headers["x-served-model"] == "model-b"
    assert sse_events(r.text)[-1] == "[DONE]"


def test_slow_upload_hits_the_request_deadline():
    """Finding 3: an incomplete upload can't hold the request open past the deadline."""
    up = Upstream()
    p = make_proxy(request_deadline_s=0.3)
    msgs = [{"type": "http.request", "body": b'{"model": "fa', "more_body": True}]

    async def receive():
        if msgs:
            return msgs.pop(0)
        await asyncio.sleep(30)  # the rest never arrives
        return {"type": "http.disconnect"}

    with mocked(up), captured_logs() as logs:
        sent = _asgi_post(p, receive)
    assert sent[0]["status"] == 408
    assert up.requests == []
    assert '"status": 408' in logs.getvalue()
