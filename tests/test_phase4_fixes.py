"""Regression tests for the fixes after the Phase 4 deep review (DESIGN §5 step 4, §5 notes, §8)."""

from __future__ import annotations

import json

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
    return make_proxy(max_body_bytes=1000, max_inflight_parsed_bytes=12_000, **kw)


def test_request_over_the_shared_budget_gets_503_busy_before_buffering():
    up = Upstream()
    p = small_budget_proxy()
    with mocked(up), running(p) as c:
        p.body_budget.used = 11_000  # other requests in progress hold most of it
        r = post(c, chat_body(content="x" * 500))
        held = p.body_budget.used
        status = c.get("/status", headers=AUTH).json()
    assert r.status_code == 503 and r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "server_busy"
    assert up.requests == [] and held == 11_000  # nothing called, nothing kept
    assert status["requests"]["by_reason"] == {"busy": 1}


def test_body_is_parsed_only_once_its_estimate_is_held(monkeypatch):
    # The lease holds the whole estimate when the body is parsed, and a request that can't
    # get it is busy before the parse (DESIGN §20.3). That the join also comes after the
    # estimate is held is pinned by test_reading_a_body_uses_no_more_memory_than_its_lease_holds.
    import quota_router.app as appmod
    from quota_router.app import parsed_memory_estimate
    real = appmod.json.loads
    seen: list[tuple[bytes, int]] = []

    def spy(s, *a, **kw):
        if isinstance(s, bytes) and s.startswith(b'{"model"'):
            seen.append((s, p.body_budget.used))
        return real(s, *a, **kw)

    monkeypatch.setattr(appmod.json, "loads", spy)
    p = small_budget_proxy()
    body = chat_body(content="x" * 500)
    with mocked(Upstream()), running(p) as c:
        assert post(c, body).status_code == 200
        raw, held = seen[0]  # the request body's parse (the first; later ones are upstream's)
        assert held == parsed_memory_estimate(raw)
        seen.clear()
        p.body_budget.used = 12_000 - 3 * len(raw)  # room for 2× the raw body, not the estimate
        r = post(c, body)
        held = p.body_budget.used
    assert r.status_code == 503 and r.json()["error"]["code"] == "server_busy"
    assert seen == [] and held == 12_000 - 3 * len(raw)  # refused before the parse, nothing kept


def test_chunked_body_over_the_budget_gets_503_busy():
    p = small_budget_proxy()

    def chunks():
        yield b'{"model": "fast", "messages": [{"role": "user", "content": "'
        yield b"x" * 600
        yield b'"}]}'

    with mocked(Upstream()), running(p) as c:
        p.body_budget.used = 11_000
        r = c.post("/v1/chat/completions", content=chunks(),
                   headers={**AUTH, "content-type": "application/json"})
        held = p.body_budget.used
    assert r.status_code == 503 and held == 11_000


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
    with pytest.raises((ConfigError, ValueError), match="max_inflight_parsed_bytes"):
        make_config(max_body_bytes=1000, max_inflight_parsed_bytes=11_119)


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


# ---- request bodies are charged their parsed-memory estimate (DESIGN §20.3) ----

def test_estimate_covers_measured_shapes_and_ignores_structure_inside_strings():
    import json as _json
    from quota_router.app import parsed_memory_estimate
    from quota_router.config import min_parsed_estimate
    text = _json.dumps({"messages": [{"role": "user", "content": "a, b: {c}, [d]" * 10_000}]}).encode()
    tiny = _json.dumps({"messages": [[0] for _ in range(10_000)]}).encode()
    escaped = _json.dumps({"messages": [{"role": "user", "content": 'q"{,}\\' * 10_000}]}).encode()
    # a long string costs the base factor only, however many commas or braces it holds
    from quota_router.config import PARSED_BASE_FACTOR
    assert parsed_memory_estimate(text) < PARSED_BASE_FACTOR * len(text) + 3_000
    assert parsed_memory_estimate(escaped) < PARSED_BASE_FACTOR * len(escaped) + 3_000
    # tiny values: measured at ~21× their size once parsed (tracemalloc, 2026-11-21)
    assert parsed_memory_estimate(tiny) > 21 * len(tiny)
    # the startup minimum allows for a chat body's own keys, so it is never below a plain one
    assert min_parsed_estimate(len(text)) >= PARSED_BASE_FACTOR * len(text)


def test_body_of_tiny_values_is_charged_more_than_its_bytes_and_gets_503_busy():
    # Regression (review): the budget counted upload bytes, so a body of tiny values within it
    # could take ~20× that in parsed memory.
    p = make_proxy(max_body_bytes=40_000, max_inflight_parsed_bytes=4_000_000)
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "x": [[0]] * 6_000}
    raw_len = len(json.dumps(body))
    with mocked(Upstream()), running(p) as c:
        p.body_budget.used = 4_000_000 - 10 * raw_len  # room for 10× the raw size, not ~20×
        r = post(c, body)
        held = p.body_budget.used
    assert r.status_code == 503 and r.json()["error"]["code"] == "server_busy"
    assert held == 4_000_000 - 10 * raw_len  # nothing kept


def test_body_whose_estimate_exceeds_the_whole_budget_gets_413():
    p = make_proxy(max_body_bytes=40_000, max_inflight_parsed_bytes=300_000)
    body = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "x": [{}] * 6_000}
    up = Upstream()
    with mocked(up), running(p) as c:
        r = post(c, body)
        assert p.body_budget.used == 0
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"
    assert up.requests == []


def test_plain_body_of_the_largest_allowed_size_fits_the_minimum_budget():
    from quota_router.config import min_parsed_estimate
    p = make_proxy(max_body_bytes=20_000, max_inflight_parsed_bytes=min_parsed_estimate(20_000) + 2_000)
    with mocked(Upstream()), running(p) as c:
        r = post(c, chat_body(content="x" * 19_800))
        assert p.body_budget.used == 0
    assert r.status_code == 200


def test_default_budget_is_128_mib():
    assert make_config().max_inflight_parsed_bytes == 128 * 1024 * 1024


def test_config_from_the_old_example_still_starts_and_warns(caplog):
    # Review finding: the pre-2026-11-21 example set max_inflight_body_bytes: 67108864 (raw
    # bytes) with 20 MB bodies. Read as parsed memory that is below the minimum, so the proxy
    # refused to start. The old key is now ignored with a warning naming the new one.
    import logging
    with caplog.at_level(logging.WARNING, logger="quota_router"):
        cfg = make_config(max_body_bytes=20971520, max_inflight_body_bytes=67108864)
    assert cfg.max_inflight_parsed_bytes == 128 * 1024 * 1024
    assert "max_inflight_parsed_bytes" in caplog.text and "ignored" in caplog.text
    p = make_proxy(max_body_bytes=20971520, max_inflight_body_bytes=67108864)
    assert p.body_budget.limit == 128 * 1024 * 1024


def test_unterminated_string_full_of_escaped_quotes_is_estimated_in_linear_time():
    # Regression (review): the string pattern could fail at an unclosed quote and was retried
    # from every later quote, so this took about 1.9 s at 16K pairs and hours at 1 MB,
    # blocking the event loop. A truncated tool-call `arguments` string has this shape.
    import time
    from quota_router.app import parsed_memory_estimate
    for body in (b'"' + b'\\"' * 200_000, b'{"a": "' + b"x" * 1000 + b"\\", b'"\n\\\n'):
        t = time.perf_counter()
        parsed_memory_estimate(body)
        assert time.perf_counter() - t < 0.5


def test_one_wide_character_raises_the_whole_bodys_estimate():
    # Regression (review): Python stores a string at its widest character's width, and
    # json.loads decodes the whole document first: ASCII + one emoji peaks at ~14× its size.
    from quota_router.app import parsed_memory_estimate
    ascii_body = json.dumps({"messages": [{"role": "user", "content": "x" * 100_000}]}).encode()
    # as UTF-8 bytes, or as the \u escapes json.dumps writes by default
    for wide, factor in ((b"\xf0\x9f\x98\x80", 15), (rb"\u0100", 9), ("Ā".encode(), 9),
                         (rb"\ud83d\ude00", 15), ("é".encode(), 6), (rb"\u00e9", 6)):
        body = ascii_body.replace(b'xxxx"', wide + b'"')
        assert parsed_memory_estimate(body) >= factor * len(body), wide
    assert parsed_memory_estimate(ascii_body) < 7 * len(ascii_body)


def test_body_of_exactly_max_body_bytes_fits_a_budget_of_exactly_the_minimum():
    # Review nit: the startup minimum promised this, but a minimal chat body's own keys cost
    # more than the allowance, so it got 413.
    from quota_router.config import min_parsed_estimate
    limit = 20_000
    p = make_proxy(max_body_bytes=limit, max_inflight_parsed_bytes=min_parsed_estimate(limit))
    shell = {"model": "fast", "stream": True, "stream_options": {"include_usage": True},
             "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": ""}]}
    pad = limit - len(json.dumps(shell, separators=(",", ":")).encode())
    shell["messages"][1]["content"] = "x" * pad
    raw = json.dumps(shell, separators=(",", ":")).encode()
    assert len(raw) == limit
    with mocked(Upstream({"model-a": [streamed(sse(chunk("ok", finish="stop")))]})), running(p) as c:
        r = c.post("/v1/chat/completions", content=raw, headers={**AUTH, "content-type": "application/json"})
    assert r.status_code == 200


# ---- the estimate is streamed in bounded memory (DESIGN §20.3, 2026-11-21) ----

def _whole_body_estimate(raw: bytes) -> int:
    """The estimate as it was before streaming: strip every string of the joined body at once.
    Kept as the reference the streamed estimate must equal."""
    import re
    from quota_router.app import _char_width
    from quota_router.config import PARSED_BASE_FACTOR, PARSED_OVERHEAD, PARSED_WIDTH_FACTOR
    s = re.sub(rb'"[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z)', b'""', raw, flags=re.S)
    values = s.count(b",") + s.count(b"[") + s.count(b"{") + 1
    factor = PARSED_BASE_FACTOR + PARSED_WIDTH_FACTOR * (_char_width(raw) - 1)
    return (factor * len(raw) + PARSED_OVERHEAD
            + 80 * values + 160 * s.count(b"{") + 64 * s.count(b":"))


def test_streamed_estimate_equals_the_whole_body_estimate_however_it_is_split(monkeypatch):
    # Strings, escapes, raw newlines and \u escapes cut at window and chunk edges, with tiny
    # windows and empty chunks.
    import random
    import quota_router.app as appmod
    rng = random.Random(7)
    parts = [b'"', b'\\', b',', b'[', b'{', b':', b'}', b']', b'a', b'0', b' ', b'\n', b'\\u', b'D83D',
             b'00e9', b'\xc4\x80', b'\xf0\x9f\x98\x80', b'\\"', b'\\\\']
    for _ in range(4000):
        raw = b"".join(rng.choice(parts) for _ in range(rng.randint(0, 60)))
        monkeypatch.setattr(appmod, "ESTIMATE_WINDOW", rng.randint(1, 8))
        est = appmod.BodyEstimate()
        pos = 0
        while pos < len(raw):
            k = rng.randint(0, 10)  # empty chunks included
            est.feed(raw[pos:pos + k])
            pos += k
        assert est.value == _whole_body_estimate(raw), raw


def _big_bodies(size: int) -> dict[str, bytes]:
    code = "def f(a, b):\n    return {'x': [a, b], 'y': \"q\\\"z\"}  # x, y: z\n" * (size // 60)
    return {
        "tiny strings": b'{"x": [' + b'"",0,' * (size // 5) + b'0]}',
        "code": json.dumps({"model": "m", "messages": [{"role": "user", "content": code}]}).encode(),
    }


@pytest.mark.parametrize("shape", ["tiny strings", "code"])
def test_estimate_scratch_memory_is_bounded_whatever_the_body_size(shape):
    # Regression (second review of the parsed-size fix): stripping the whole body with one
    # re.sub took ~44× its size for tiny strings and ~12× for code (the regex engine keeps
    # state per escape inside one string), above the estimate it then charged.
    import tracemalloc
    from quota_router.app import parsed_memory_estimate
    raw = _big_bodies(2_000_000)[shape]
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        value = parsed_memory_estimate(raw)
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024, peak  # ~370 KB at most for the worst shapes measured
    assert value == _whole_body_estimate(raw)  # same accuracy as before


def _read_body_measured(raw: bytes, chunk_size: int, budget: int = 1 << 30):
    """Feeds `raw` to _read_json in chunks made as they are received (as a server would) and
    returns (result, lease, tracemalloc peak during the read)."""
    import asyncio
    import tracemalloc
    from starlette.requests import Request
    from quota_router.app import BodyBudget, _read_json
    pos = 0

    async def receive():
        nonlocal pos
        piece = raw[pos:pos + chunk_size]
        pos += chunk_size
        return {"type": "http.request", "body": piece, "more_body": pos < len(raw)}

    lease = BodyBudget(budget).lease()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        result = asyncio.run(_read_json(request, 2_000_000, lease))
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    return result, lease, peak


@pytest.mark.parametrize("shape,size,chunk_size", [
    ("tiny strings", 1_000_000, 65536), ("code", 1_000_000, 65536),
])
def test_reading_a_body_uses_no_more_memory_than_its_lease_holds(shape, size, chunk_size):
    # Regression: the peak while the body is received, estimated, joined and parsed stays
    # within what the request holds, plus the estimate's fixed scratch (DESIGN §20.3).
    body, lease, peak = _read_body_measured(_big_bodies(size)[shape], chunk_size)
    assert isinstance(body, dict)
    assert peak <= lease.held + 512 * 1024, (peak, lease.held)


def test_body_that_can_never_fit_gets_413_even_under_load():
    # Review nit: the 2× join charge ran before the estimate, so under load a body that could
    # never fit got 503 (and a client retrying it got 503 again) instead of its 413.
    p = make_proxy(max_body_bytes=40_000, max_inflight_parsed_bytes=300_000)
    up = Upstream()

    def chunks():  # no Content-Length, so nothing is charged before the body is read
        yield b'{"model": "fast", "messages": [{"role": "user", "content": "hi"}], "x": ['
        for _ in range(6):
            yield b"{}, " * 1_000
        yield b"{}]}"

    raw_len = sum(len(c) for c in chunks())
    with mocked(up), running(p) as c:
        p.body_budget.used = 300_000 - int(1.5 * raw_len)  # room for the bytes, not for 2× of them
        r = c.post("/v1/chat/completions", content=chunks(),
                   headers={**AUTH, "content-type": "application/json"})
        held = p.body_budget.used
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"
    assert held == 300_000 - int(1.5 * raw_len) and up.requests == []
