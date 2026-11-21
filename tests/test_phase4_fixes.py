"""Regression tests for the fixes after the Phase 4 deep review (DESIGN §5 step 4, §5 notes, §8)."""

from __future__ import annotations

import httpx
import pytest
import respx

from apphelpers import AUTH, URL, Chunks, Upstream, chat_body, chunk, error_body, make_proxy, mocked, running, sse, streamed
from helpers import T0, Env, FailingConn, ScriptedUpstream, chat, make_config, model_dict, pt
from quota_router.classify import Classification
from quota_router.clock import FakeClock
from quota_router.router import RouteServed, Unavailable


def post(client, body):
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


def respx_mock(side_effect):
    mock = respx.mock(assert_all_called=False)
    mock.post(URL).mock(side_effect=side_effect)
    return mock


# ---- 1. a request that never left the machine refunds its daily slot ----------

@pytest.mark.parametrize("exc", [httpx.ConnectError("no route"), httpx.ConnectTimeout("slow dns"),
                                 httpx.PoolTimeout("pool full")])
def test_not_sent_error_refunds_rpd_but_still_cools_down(exc):
    up = Upstream({"model-a": [exc]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body())
        snap = p.store.snapshot()
        blocked = p.store.check(p.config.models["a"], 10)
    assert r.headers["x-served-model"] == "model-b"
    assert snap["model-a"]["rpd_used"] == 0  # Google never saw it
    assert snap["model-a"]["rpm_used"] == 1  # the RPM slot is kept, as for a 429
    assert blocked is not None and blocked.reason == "unavailable"  # the 30 s cooldown still applies


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow model"), httpx.ReadError("reset"),
                                 httpx.WriteError("broken pipe"), httpx.RemoteProtocolError("bad")])
def test_error_after_sending_keeps_rpd(exc):
    up = Upstream({"model-a": [exc]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        snap = p.store.snapshot()
    assert snap["model-a"]["rpd_used"] == 1  # the request may have used quota


def test_failure_before_sending_refunds_rpd():
    p = make_proxy()

    def broken(*a, **kw):
        raise RuntimeError("could not build the request")

    with mocked(Upstream()), running(p) as c:
        p.upstream._client.build_request = broken
        r = post(c, chat_body())
        snap = p.store.snapshot()
    assert r.status_code == 503
    assert snap["model-a"]["rpd_used"] == 0 and snap["model-b"]["rpd_used"] == 0


def test_offline_retries_do_not_use_up_a_scarce_model():
    # 20 RPD: with the old behaviour, 20 connect errors blocked the model for the rest of the day.
    clock = FakeClock(T0)
    up = Upstream({"model-a": [httpx.ConnectError("offline") for _ in range(25)]})
    p = make_proxy(models={"a": model_dict("model-a", rpd=20)}, clock=clock)
    with mocked(up), running(p) as c:
        for _ in range(25):
            post(c, chat_body())
            clock.advance(31)  # past the cooldown
        r = post(c, chat_body())  # back online
    assert r.status_code == 200 and r.headers["x-served-model"] == "model-a"
    assert p.store.snapshot()["model-a"]["rpd_used"] == 1


def test_not_sent_network_timeout_cools_down_even_when_the_attempt_was_cut_short():
    # A connect timeout at the very end of a shortened attempt is still an early network timeout.
    e = Env(make_config({"a": model_dict("model-a")}))
    up = ScriptedUpstream({"model-a": Unavailable("timeout", not_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up, started_at=T0 - 170)
    blocked = e.store.check(e.model("a"), 10)
    assert blocked is not None and blocked.reason == "unavailable"
    assert e.store.snapshot()["model-a"]["rpd_used"] == 0


def test_attempt_timeout_inside_send_keeps_rpd():
    # The attempt's own deadline fires while waiting for Google: the request may have arrived.
    import asyncio

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    p = make_proxy(models={"a": model_dict("model-a")}, request_deadline_s=0.2)
    with respx_mock(slow), running(p) as c:
        r = post(c, chat_body())
        snap = p.store.snapshot()
    assert r.status_code == 503
    assert snap["model-a"]["rpd_used"] == 1


# ---- 2. a first attempt cut short by a slow upload gets no cooldown ----------

def test_first_attempt_cut_short_by_the_upload_gets_no_cooldown():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))

    def runs_out(model, timeout_s):
        e.clock.advance(timeout_s)  # the whole (shortened) attempt timeout passes

    up = ScriptedUpstream({"model-a": Unavailable("timeout")}, on_call=runs_out)
    # The upload took 170 s of the 180 s deadline: the first attempt gets 10 s, not 120 s.
    out = e.route(chat(), up, started_at=T0 - 170)
    assert up.calls == [("model-a", 10.0)]
    assert out.candidates[0].detail == "timeout (cut short by the request deadline)"
    assert e.store.check(e.model("a"), 10) is None  # no cooldown for every other client


def test_first_attempt_with_full_time_still_cools_down():
    e = Env(make_config({"a": model_dict("model-a")}))
    up = ScriptedUpstream({"model-a": Unavailable("timeout")},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up)
    blocked = e.store.check(e.model("a"), 10)
    assert blocked is not None and blocked.reason == "unavailable"


# ---- 3. an error event after streaming started updates the model's state ----

RPM_DETAILS = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
               {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"}]


def test_mid_stream_429_sets_the_cooldown_and_keeps_rpd():
    err = error_body(429, "RESOURCE_EXHAUSTED", "quota exceeded", RPM_DETAILS)
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), err, done=False))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        snap = p.store.snapshot()
        r2 = post(c, chat_body())
    assert snap["model-a"]["cooldown_until"] == T0 + 40
    assert snap["model-a"]["rpd_used"] == 1  # served in part: the slot is kept
    assert r2.headers["x-served-model"] == "model-b"  # model-a is not tried again at once


def test_mid_stream_per_day_429_after_midnight_cools_down_instead_of_exhausting():
    # Reserved at 23:59:50 PT; the error event arrives at 00:30, past the 10-minute grace, so only
    # the reservation's send time keeps the new day (DESIGN §5 notes, per-day 429 and the PT date).
    clock = FakeClock(pt(2026, 11, 20, 23, 59, 50))
    err = error_body(429, "RESOURCE_EXHAUSTED", "quota exceeded", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}])
    parts = sse(chunk("partial"), err, done=False)

    def tick(n, part):
        if n == 1:
            clock.set(pt(2026, 11, 21, 0, 30))

    resp = httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=Chunks(parts, before=tick))
    up = Upstream({"model-a": [resp]})
    p = make_proxy(models={"a": model_dict("model-a", rpm=5, rpd=20)}, clock=clock)
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        snap = p.store.snapshot()["model-a"]
    assert snap["exhausted_until"] is None
    assert snap["cooldown_until"] == pt(2026, 11, 21, 0, 31)


def test_mid_stream_5xx_sets_the_unavailable_cooldown():
    err = error_body(503, "UNAVAILABLE", "high demand")
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), err, done=False))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        blocked = p.store.check(p.config.models["a"], 10)
    assert blocked is not None and blocked.reason == "unavailable"


def test_mid_stream_client_error_changes_no_state():
    err = error_body(400, "INVALID_ARGUMENT", "bad part")
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), err, done=False))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        blocked = p.store.check(p.config.models["a"], 10)
    assert blocked is None


# ---- 4. a failed DB read at PT midnight can't drop blocking state -------------

def test_blocking_state_is_set_even_when_the_new_days_read_fails():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}),
            start=pt(2026, 11, 20, 23, 59, 59))
    real = e.store._conn
    e.clock.set(pt(2026, 11, 21, 0, 0, 1))  # first call of the new PT day reloads its count
    e.store._conn = FailingConn(real, fail_on="SELECT count FROM rpd")
    # Through `_safely`, as the router calls them: a SQLite error is logged, not raised.
    e.router._safely(e.store.on_unavailable, e.model("a"))
    e.router._safely(e.store.on_rate_limited, e.model("b"), Classification("rpm", 30.0))
    e.store._conn = real
    a, b = e.store.check(e.model("a"), 10), e.store.check(e.model("b"), 10)
    assert a is not None and a.reason == "unavailable"
    assert b is not None and b.reason == "quota" and b.ready_at == e.clock.now() + 30


# ---- follow-up: the midnight grace honours a longer delay from Google ---------

def test_grace_cooldown_is_never_shorter_than_googles_delay():
    e = Env(make_config({"a": model_dict("model-a")}), start=pt(2026, 11, 21, 0, 2))
    e.store.on_rate_limited(e.model("a"), Classification("rpd", 300.0), sent_at=e.clock.now())
    snap = e.store.snapshot()["model-a"]
    assert snap["exhausted_until"] is None and snap["cooldown_until"] == e.clock.now() + 300


# ---- follow-up: the deadline is measured again after reserving ----------------

class SlowWrites:
    """Wraps the connection; the RPD write takes `delay` seconds on the fake clock."""

    def __init__(self, conn, clock, delay: float) -> None:
        self._conn, self._clock, self._delay = conn, clock, delay

    def execute(self, sql, *args):
        if "INSERT INTO rpd" in sql:
            self._clock.advance(self._delay)
        return self._conn.execute(sql, *args)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def test_reservation_that_crosses_the_deadline_is_released_and_not_called():
    e = Env(make_config({"a": model_dict("model-a")}))
    e.store._conn = SlowWrites(e.conn, e.clock, 1.0)
    up = ScriptedUpstream()
    out = e.route(chat(), up, started_at=T0 - 179.5)  # 0.5 s left before the write
    e.store._conn = e.conn
    assert up.calls == []
    assert out.status == 503 and out.candidates[0].reason == "not_tried"
    snap = e.store.snapshot()["model-a"]
    assert snap["rpd_used"] == 0 and snap["rpm_used"] == 0 and snap["tpm_input_used"] == 0


def test_attempt_timeout_uses_the_time_left_after_reserving():
    e = Env(make_config({"a": model_dict("model-a")}))
    e.store._conn = SlowWrites(e.conn, e.clock, 1.0)
    up = ScriptedUpstream()
    e.route(chat(), up, started_at=T0 - 170)  # 10 s left before the write, 9 s after it
    assert up.calls == [("model-a", 9.0)]


# ---- follow-up: Zoo Code's tool turns are pinned (DESIGN §5.1) ---------------

def zoo_turn(*, extra_user: int = 1) -> list[dict]:
    """The shape Zoo Code 3.98.0 sends: tool results, then its environment details as a user message."""
    msgs = [{"role": "system", "content": "You are Zoo."},
            {"role": "user", "content": "Read README.md"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1111111111111111111", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1111111111111111111", "content": "# README"}]
    msgs += [{"role": "user", "content": "<environment_details>...</environment_details>"}] * extra_user
    return msgs


@pytest.mark.parametrize("extra_user", [0, 1, 2])
def test_zoo_code_tool_turn_is_pinned(extra_user):
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    e.affinity.record(["call_1111111111111111111"], "model-b")
    up = ScriptedUpstream()
    out = e.route(chat(messages=zoo_turn(extra_user=extra_user)), up)
    assert up.called == ["model-b"] and out.affinity == "pinned"


def test_a_user_message_after_an_assistant_reply_starts_a_new_turn():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    e.affinity.record(["call_1111111111111111111"], "model-b")
    msgs = zoo_turn() + [{"role": "assistant", "content": "Done."},
                         {"role": "user", "content": "Thanks, now something else"}]
    up = ScriptedUpstream()
    out = e.route(chat(messages=msgs), up)
    assert up.called == ["model-a"] and out.affinity is None


# ---- follow-up: request bodies share a memory budget (DESIGN §20.3) ----------

def small_budget_proxy(**kw):
    return make_proxy(max_body_bytes=1000, max_inflight_body_bytes=2000, **kw)


def test_request_over_the_shared_budget_gets_503_busy_before_buffering():
    up = Upstream()
    p = small_budget_proxy()
    with mocked(up), running(p) as c:
        p.body_budget.used = 1500  # other requests in progress hold most of it
        r = post(c, chat_body(content="x" * 500))
        held = p.body_budget.used
        status = c.get("/status", headers=AUTH).json()
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "server_busy"
    assert up.requests == [] and held == 1500  # nothing called, nothing kept
    assert status["requests"]["by_reason"] == {"busy": 1}


def test_chunked_body_over_the_budget_gets_503_busy():
    p = small_budget_proxy()

    def chunks():
        yield b'{"model": "fast", "messages": [{"role": "user", "content": "'
        yield b"x" * 600
        yield b'"}]}'

    with mocked(Upstream()), running(p) as c:
        p.body_budget.used = 1500
        r = c.post("/v1/chat/completions", content=chunks(),
                   headers={**AUTH, "content-type": "application/json"})
        held = p.body_budget.used
    assert r.status_code == 503 and held == 1500


@pytest.mark.parametrize("script", [
    [None],                                                       # non-stream served
    [streamed(sse(chunk("ok", finish="stop")))],                  # stream to [DONE]
    [streamed(sse(chunk("partial"), done=False), error=httpx.ReadError("cut"))],  # stream error
    [httpx.Response(400, json=error_body(400, "INVALID_ARGUMENT", "bad"))],       # passthrough
])
def test_budget_is_released_after_every_ending(script):
    up = Upstream({"model-a": [s for s in script if s is not None]})
    p = small_budget_proxy()
    stream = script[0] is not None and script[0].headers.get("content-type") == "text/event-stream"
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=stream))
        post(c, chat_body(content="x" * 2000))  # 413: over max_body_bytes
        assert p.body_budget.used == 0


def test_budget_below_max_body_bytes_is_a_config_error():
    from quota_router.config import ConfigError
    with pytest.raises((ConfigError, ValueError), match="max_inflight_body_bytes"):
        make_config(max_body_bytes=1000, max_inflight_body_bytes=999)


def test_release_removes_its_own_token_entry_not_an_equal_one():
    e = Env(make_config({"a": model_dict("model-a")}))
    m = e.model("a")
    first = e.store.try_reserve(m, 100)
    second = e.store.try_reserve(m, 100)  # same clock reading, same estimate
    e.store.release(second)
    e.store.settle(first, 40)  # the kept reservation's correction must still count
    assert e.store.snapshot()["model-a"]["tpm_input_used"] == 40


def test_later_attempt_released_when_the_write_leaves_under_5_seconds():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    e.store._conn = SlowWrites(e.conn, e.clock, 1.0)
    up = ScriptedUpstream({"model-a": Unavailable("http 503")})
    out = e.route(chat(), up, started_at=T0 - 173.5)  # 6.5 s; 5.5 when model-b reserves, 4.5 after its write
    e.store._conn = e.conn
    assert up.called == ["model-a"]
    assert out.candidates[1].reason == "not_tried"
    snap = e.store.snapshot()["model-b"]
    assert snap["rpd_used"] == 0 and snap["rpm_used"] == 0 and snap["tpm_input_used"] == 0
