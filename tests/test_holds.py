"""Short-wait holds: hold a request for the proxy's own RPM or TPM window (DESIGN §25)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from apphelpers import (AUTH, LOCAL, Upstream, captured_logs, chat_body, chunk, make_proxy, mocked,
                        ok, running, sse, streamed)
from helpers import Env, ScriptedUpstream, T0, chat, make_config, model_dict, pt
from quota_router.classify import Classification
from quota_router.config import ConfigError
from quota_router.router import (HOLD_PAD_S, Hold, RateLimited, RouteFailed, RouteServed,
                                 Unavailable, with_signatures)

TPM = 1000  # small, so a test can fill the window with one reservation
W32 = 32 + HOLD_PAD_S  # a 32 s wait as slept: the wake time plus the pad
MS32 = round(W32 * 1000)


def fake_sleep(clock, calls: list | None = None, on_sleep=None, extra: float = 0.0):
    """A hold's sleep on the fake clock: yields, then moves it to the wake time (never back),
    plus `extra`, then yields again so a task woken by `on_sleep` runs first."""
    async def sleep(s: float) -> None:
        if calls is not None:
            calls.append(s)
        target = clock.now() + s + extra
        await asyncio.sleep(0)
        clock.set(max(clock.now(), target))
        if on_sleep is not None:
            on_sleep()
        await asyncio.sleep(0)
    return sleep


def env_(models: dict | None = None, routes: dict | None = None, hold_max_s: float = 60, **top) -> Env:
    models = models or {"a": model_dict("model-a", tpm_input=TPM), "b": model_dict("model-b", tpm_input=TPM)}
    e = Env(make_config(models, routes or {"fast": list(models)}, hold_max_s=hold_max_s, **top))
    e.sleeps = []  # type: ignore[attr-defined]
    e.router.sleep = fake_sleep(e.clock, e.sleeps)  # type: ignore[attr-defined]
    return e


def fill_tpm(e: Env, key: str, wait: float = 32.0, tokens: int = TPM - 5) -> None:
    """Fill the model's input-TPM window so it frees `wait` seconds from now."""
    res = e.store.try_reserve(e.model(key), tokens)
    assert not hasattr(res, "reason"), res
    e.clock.advance(60 - wait)


def fill_rpm(e: Env, key: str, wait: float = 32.0) -> None:
    m = e.model(key)
    for _ in range(m.rpm):
        assert not hasattr(e.store.try_reserve(m, 1), "reason")
    e.clock.advance(60 - wait)


def pinned_to(e: Env, model_id: str, ids=("c1",), signatures=None) -> dict:
    e.affinity.record(list(ids), model_id, None, signatures)
    msgs: list = [{"role": "user", "content": "task"}]
    for i in ids:
        msgs += [{"role": "assistant", "tool_calls": [{"id": i, "type": "function",
                  "function": {"name": "f", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": i, "content": "done"}]
    return chat(messages=msgs)


def timed_upstream(e: Env, script=None) -> ScriptedUpstream:
    up = ScriptedUpstream(script)
    up.at = []  # type: ignore[attr-defined]
    up.on_call = lambda m, t: up.at.append(e.clock.now())  # type: ignore[attr-defined]
    return up


# ---- holding a short local wait ---------------------------------------------

@pytest.mark.parametrize("limit", ["tpm", "rpm"])
def test_pinned_owner_with_a_short_local_wait_is_held_then_served(limit):
    e = env_({"a": model_dict("model-a", tpm_input=TPM, rpm=2), "b": model_dict("model-b", tpm_input=TPM)},
             handoff_targets=["b"])
    req = pinned_to(e, "model-a")
    (fill_tpm if limit == "tpm" else fill_rpm)(e, "a")
    arrived = e.clock.now()
    up = timed_upstream(e)
    hold = Hold()
    out = e.route(req, up, hold=hold)
    assert isinstance(out, RouteServed) and out.model.id == "model-a" and out.affinity == "pinned"
    assert up.called == ["model-a"]  # one call: the owner; b free but the gate keeps the owner
    assert up.at == [arrived + W32]
    assert e.sleeps == [W32]
    assert (hold.model, hold.limits, hold.ms) == ("model-a", [limit], MS32)
    assert out.path == ["model-a"]
    assert e.router.holds_in_flight == 0


def test_a_wait_over_hold_max_s_is_answered_with_429():
    e = env_(hold_max_s=20)
    for key in ("a", "b"):
        e.store.try_reserve(e.model(key), TPM - 5)
    e.clock.advance(28)  # both free in 32 s
    hold = Hold()
    out = e.route(chat(), ScriptedUpstream(), hold=hold)
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 32
    assert e.sleeps == [] and not hold.held


@pytest.mark.parametrize("top", [{"hold_max_s": 0}, {}])
def test_hold_off_by_default_and_at_zero(top):
    models = {"a": model_dict("model-a", tpm_input=TPM)}
    e = Env(make_config(models, {"fast": ["a"]}, **top))
    assert e.config.hold_max_s == 0
    calls: list = []
    e.router.sleep = fake_sleep(e.clock, calls)
    fill_tpm(e, "a")
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 32
    assert calls == []


# ---- never held -------------------------------------------------------------

@pytest.mark.parametrize("result", [
    RateLimited({}), Unavailable("http 503", counts=True), Unavailable("timeout", body_sent=True)],
    ids=["429", "5xx", "timeout"])
def test_no_hold_after_an_upstream_call(result):
    # a's RPM is 1, so after its call it is a local rpm block too and the outcome is a 429.
    e = env_({"a": model_dict("model-a", rpm=1), "b": model_dict("model-b", rpm=1)})
    fill_rpm(e, "b", wait=10)
    up = ScriptedUpstream({"model-a": result})
    hold = Hold()
    out = e.route(chat(), up, hold=hold)
    assert isinstance(out, RouteFailed) and out.status == 429
    assert up.called == ["model-a"] and e.sleeps == [] and not hold.held


def _soft_cooldown(e: Env, key: str = "a") -> None:
    st = e.store._state[e.model(key).id]
    st.soft_until = e.clock.now() + 100  # the soft part of a 5xx cooldown


def _recovering(e: Env, key: str = "a") -> None:
    st = e.store._state[e.model(key).id]
    st.streak = 1
    st.slot = e.store.try_reserve(e.model("b"), 1)  # any reservation: the slot is taken


@pytest.mark.parametrize("setup,label", [
    (lambda e: e.store.on_rate_limited(e.model("a"), Classification("rpm", 10.0)), "cooldown_429"),
    (_soft_cooldown, "cooldown_5xx"),
    (_recovering, "recovering"),
])
def test_no_hold_for_blocks_that_are_not_the_proxys_own_count(setup, label):
    e = env_({"a": model_dict("model-a", rpm=1), "b": model_dict("model-b")}, routes={"fast": ["a"]})
    fill_rpm(e, "a", wait=10)  # also a short rpm block, so only the other limit stops the hold
    setup(e)
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 429
    assert label in out.candidates[0].limits
    assert e.sleeps == []


def test_no_hold_for_tpm_set_by_googles_tpm_429():
    e = env_(routes={"fast": ["a"]})
    e.store.on_rate_limited(e.model("a"), Classification("tpm", 5.0))
    e.clock.advance(6)  # the cooldown is over; the 60 s TPM window from Google's 429 is not
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 429
    assert out.candidates[0].limits == ["tpm"]
    assert e.sleeps == []


@pytest.mark.parametrize("kind", ["rpd", "exhausted"])
def test_no_hold_for_rpd_or_exhausted_seconds_before_pt_midnight(kind):
    models = {"a": model_dict("model-a", rpd=1)}
    e = Env(make_config(models, {"fast": ["a"]}, hold_max_s=60), start=pt(2026, 11, 20, 23, 59, 50))
    e.router.sleep = fake_sleep(e.clock, calls := [])
    if kind == "rpd":
        e.store.try_reserve(e.model("a"), 1)
    else:
        e.store.on_rate_limited(e.model("a"), Classification("rpd", None), sent_at=e.clock.now())
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 10
    assert out.candidates[0].limits == [kind]
    assert calls == []


def test_no_hold_for_a_503_outcome():
    e = env_()
    fill_tpm(e, "a")
    e.store.on_unavailable(e.model("b"))  # 30 s hard 5xx cooldown: reason unavailable
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 503
    assert e.sleeps == []


@pytest.mark.parametrize("ago", [4, 3])
def test_no_hold_when_the_deadline_would_leave_under_5_s_and_the_slack(ago):
    e = env_(routes={"fast": ["a"]}, request_deadline_s=40, hold_max_s=35)
    fill_tpm(e, "a")
    # Arrived `ago` s ago: waking at 32 s would leave 4 s or 5 s, under 5 s + 1 s of slack.
    out = e.route(chat(), ScriptedUpstream(), started_at=e.clock.now() - ago)
    assert isinstance(out, RouteFailed) and out.status == 429
    assert e.sleeps == []


def test_hold_leaving_5_s_and_the_slack_is_held():
    """Review finding 1: 6 s left at the wake time, so the pad and overshoot still leave 5 s."""
    e2 = env_(routes={"fast": ["a"]}, request_deadline_s=40, hold_max_s=35)
    fill_tpm(e2, "a")
    out = e2.route(chat(), ScriptedUpstream(), started_at=e2.clock.now() - 2)
    assert isinstance(out, RouteServed) and e2.sleeps == [W32]


# ---- the margin after waking ------------------------------------------------

def test_reservation_with_too_little_left_after_waking_is_released_with_503():
    e = env_(routes={"fast": ["a"]}, request_deadline_s=40, hold_max_s=35)
    fill_tpm(e, "a")
    before = e.store.snapshot()["model-a"]
    real = e.store.try_reserve

    def slow_reserve(model, tokens, **kw):
        res = real(model, tokens, **kw)
        if e.sleeps:  # after the hold: the reservation's write took 4 s (8 s were left)
            e.clock.advance(4)
        return res
    e.store.try_reserve = slow_reserve  # type: ignore[method-assign]
    up = ScriptedUpstream()
    out = e.route(chat(), up)
    assert isinstance(out, RouteFailed) and out.status == 503
    assert up.called == [] and e.sleeps == [W32]
    after = e.store.snapshot()["model-a"]
    assert after["rpd_used"] == before["rpd_used"]  # given back
    assert after["rpm_used"] == 0 and after["tpm_input_used"] == 0  # the old entry left the window too


def test_sleep_that_overruns_the_margin_gives_503_without_reserving():
    e = env_(routes={"fast": ["a"]}, request_deadline_s=40, hold_max_s=35)
    e.router.sleep = fake_sleep(e.clock, e.sleeps, extra=4)  # woke 4 s late: 4 s left
    fill_tpm(e, "a")
    rpd = e.store.snapshot()["model-a"]["rpd_used"]
    up = ScriptedUpstream()
    out = e.route(chat(), up)
    assert isinstance(out, RouteFailed) and out.status == 503 and out.code == "upstream_unavailable"
    assert up.called == [] and e.store.snapshot()["model-a"]["rpd_used"] == rpd


# ---- the handoff gate -------------------------------------------------------

def test_owner_blocked_60_s_or_more_still_hands_off_first():
    e = env_(handoff_targets=["b"])
    req = pinned_to(e, "model-a")
    e.store.on_rate_limited(e.model("a"), Classification("rpm", 60.0))
    out = e.route(req, ScriptedUpstream())
    assert isinstance(out, RouteServed) and out.model.id == "model-b" and out.handoff_from == "model-a"
    assert e.sleeps == []


def test_hold_waits_for_the_earliest_admitted_target():
    models = {"a": model_dict("model-a", tpm_input=TPM), "b": model_dict("model-b", tpm_input=TPM, rpm=None),
              "c": model_dict("model-c", tpm_input=TPM, rpm=1)}
    e = env_(models, handoff_targets=["b", "c"])
    req = pinned_to(e, "model-a")
    assert not hasattr(e.store.try_reserve(e.model("b"), TPM - 5), "reason")  # tpm, frees at +60
    e.clock.advance(30)
    assert not hasattr(e.store.try_reserve(e.model("c"), 1), "reason")  # rpm, frees at +90
    e.clock.advance(20)  # now +50: b frees in 10 s, c in 40 s
    e.store.on_rate_limited(e.model("a"), Classification("rpm", 60.0))  # owner: 60 s, hands off
    up = timed_upstream(e)
    hold = Hold()
    out = e.route(req, up, hold=hold)
    assert e.sleeps == [10 + HOLD_PAD_S] and (hold.model, hold.limits) == ("model-b", ["tpm"])
    assert isinstance(out, RouteServed) and out.model.id == "model-b" and out.handoff_from == "model-a"
    assert up.called == ["model-b"]


def test_targets_are_not_used_for_the_wait_when_the_gate_keeps_the_owner():
    # The owner frees in 40 s; b would free in 5 s but the gate never admits it.
    models = {k: model_dict(f"model-{k}", tpm_input=TPM) for k in "ab"}
    e = env_(models, handoff_targets=["b"])
    req = pinned_to(e, "model-a")
    e.store.try_reserve(e.model("a"), TPM - 5)
    e.clock.advance(15)
    e.store.try_reserve(e.model("b"), TPM - 5)
    e.clock.advance(5)  # a frees in 40 s; b is free in 55 s, but b is never walked
    hold = Hold()
    out = e.route(req, ScriptedUpstream(), hold=hold)
    assert e.sleeps == [40 + HOLD_PAD_S] and hold.model == "model-a"
    assert isinstance(out, RouteServed) and out.model.id == "model-a"


# ---- one routing context, one hold ------------------------------------------

AUTO_MODELS = {"s1": model_dict("smart-1", rpm=None, rpd=20, tpm_input=TPM),
               "s2": model_dict("smart-2", rpm=None, rpd=20, tpm_input=TPM),
               "f1": model_dict("fast-1", rpm=None, rpd=500, tpm_input=TPM)}
AUTO = {"name": "gemini-auto", "tiers": {"smart": ["s1", "s2"], "fast": ["f1"]},
        "default_tier": "fast", "pacing": {"tier": "smart", "reserve": 5}}


def test_routing_context_is_kept_across_the_hold():
    e = env_(AUTO_MODELS, {"gemini-smart": ["s1", "s2"]}, auto=AUTO)
    up = ScriptedUpstream()
    start = e.route(chat("gemini-auto", tools=[{"type": "function", "function": {"name": "f"}}]), up)
    assert isinstance(start, RouteServed) and start.model.key == "s1" and start.route_tier == "smart"
    e.router.complete(start, 10, ["c1"])
    msgs = [{"role": "user", "content": "task"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "done"}]
    fill_tpm(e, "s1", tokens=TPM - 50)  # the turn start's own tokens are in the window too

    def meanwhile():  # during the hold: c1 now names another model, and the pace has gone
        e.affinity.record(["c1"], "fast-1", "other-turn")
        for _ in range(20):
            e.store.try_reserve(e.model("s2"), 1)
    e.router.sleep = fake_sleep(e.clock, e.sleeps, on_sleep=meanwhile)
    out = e.route({"model": "gemini-auto", "messages": msgs}, up)
    assert e.sleeps == [W32]
    assert isinstance(out, RouteServed)
    assert (out.model.key, out.affinity, out.route_tier, out.route_reason) == ("s1", "pinned", "smart", "pinned")
    assert out.turn.turn_id == start.turn.turn_id


def test_two_held_requests_for_one_window_one_served_one_refused():
    models = {"a": model_dict("model-a", tpm_input=TPM)}
    e = env_(models, routes={"fast": ["a"]})
    big = chat(messages=[{"role": "user", "content": "x" * 2000}])  # ~600 tokens: one fits at a time
    e.store.try_reserve(e.model("a"), 500)
    e.clock.advance(28)

    async def both():
        h1, h2 = Hold(), Hold()
        outs = await asyncio.gather(e.router.route(big, ScriptedUpstream(), hold=h1),
                                    e.router.route(big, ScriptedUpstream(), hold=h2))
        return outs, (h1, h2)
    (o1, o2), (h1, h2) = asyncio.run(both())
    assert h1.held and h2.held and e.sleeps == [W32, W32]
    statuses = sorted(type(o).__name__ for o in (o1, o2))
    assert statuses == ["RouteFailed", "RouteServed"]
    failed = o1 if isinstance(o1, RouteFailed) else o2
    assert failed.status == 429 and failed.retry_after_s == 60  # fresh: the served one's entry


def test_gone_at_waking_raises_cancelled_before_any_send():
    e = env_(routes={"fast": ["a"]})
    fill_tpm(e, "a")
    rpd = e.store.snapshot()["model-a"]["rpd_used"]
    gone = {"v": False}
    e.router.sleep = fake_sleep(e.clock, e.sleeps, on_sleep=lambda: gone.update(v=True))
    up = ScriptedUpstream()
    with pytest.raises(asyncio.CancelledError):
        e.route(chat(), up, gone=lambda: gone["v"])
    assert up.called == [] and e.store.snapshot()["model-a"]["rpd_used"] == rpd
    assert e.router.holds_in_flight == 0


def test_mixed_turn_after_a_hold_sends_the_same_body_and_counts_restores_once():
    e = env_(handoff_targets=[])
    # c1 by model-b, c2 by model-a (the latest): a mixed turn owned by model-a. The client sent
    # no signatures, so c2 gets model-a's stored one back and c1 the placeholder.
    e.affinity.record(["c1"], "model-b", "t1", {"c1": "SIG-B"})
    e.affinity.record(["c2"], "model-a", "t1", {"c2": "SIG-A"})
    msgs: list = [{"role": "user", "content": "task"}]
    for i in ("c1", "c2"):
        msgs += [{"role": "assistant", "tool_calls": [{"id": i, "type": "function",
                  "function": {"name": "f", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": i, "content": "done"}]
    req = chat(messages=msgs)
    expected = with_signatures(req, "model-a", e.affinity.lookup_records(["c1", "c2"]), foreign=True)
    fill_tpm(e, "a")
    up = ScriptedUpstream()
    out = e.route(req, up)
    assert isinstance(out, RouteServed) and out.affinity == "mixed" and e.sleeps == [W32]
    assert up.bodies == [expected.body]
    assert e.router.signatures["restored"] == 1 and e.router.signatures["placeholder"] == 0


# ---- config -----------------------------------------------------------------

@pytest.mark.parametrize("top,ok_", [
    ({"hold_max_s": -1}, False),
    ({"hold_max_s": 60, "request_deadline_s": 64}, False),
    ({"hold_max_s": 60, "request_deadline_s": 65}, True),
    ({"hold_max_s": float("nan")}, False),
    ({"hold_max_s": float("inf")}, False),
    ({"hold_max_s": 0, "request_deadline_s": 3}, True),
    ({"request_deadline_s": 3}, True),
])
def test_hold_max_s_validation(top, ok_):
    if ok_:
        assert make_config(**top).hold_max_s == top.get("hold_max_s", 0)
    else:
        with pytest.raises(ConfigError, match="hold_max_s"):
            make_config(**top)


# ---- the HTTP layer ---------------------------------------------------------

def held_proxy(**top):
    models = {"a": model_dict("model-a", tpm_input=TPM), "b": model_dict("model-b", tpm_input=TPM)}
    p = make_proxy(models, hold_max_s=60, **top)
    for key in ("a", "b"):
        assert not hasattr(p.store.try_reserve(p.config.models[key], TPM - 5), "reason")
    p.clock.advance(28)  # both free in 32 s
    p.router.sleep = fake_sleep(p.clock)
    return p


def request_lines(logs) -> list[dict]:
    return [json.loads(ln) for ln in logs.getvalue().splitlines() if '"event": "request"' in ln]


def test_held_turn_start_is_served_logged_counted_and_has_one_turn_row():
    p = held_proxy()
    up = Upstream()
    with mocked(up), captured_logs() as logs, running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        status = c.get("/status", headers=AUTH).json()
        metrics = c.get("/metrics", headers=AUTH).text
        rows = p.conn.execute("SELECT steps, errors FROM turns").fetchall()
    assert r.status_code == 200 and r.headers["x-fallback-path"] == "model-a"
    line = request_lines(logs)[0]
    assert line["status"] == 200 and "blocked" not in line
    assert line["hold"] == {"model": "model-a", "limits": ["tpm"], "ms": MS32}
    assert status["holds"] == {"max_s": 60, "in_flight": 0, "served": 1, "passthrough": 0,
                               "refused": 0, "cancelled": 0, "error": 0}
    assert 'qr_holds_total{result="served"} 1' in metrics
    assert rows == [(1, 0)]


def test_held_request_whose_window_is_taken_is_refused_with_a_fresh_retry_after():
    p = held_proxy()

    def taken():  # another request takes the room while this one sleeps
        for key in ("a", "b"):
            p.store.try_reserve(p.config.models[key], TPM - 5)
    p.router.sleep = fake_sleep(p.clock, on_sleep=taken)
    with mocked(Upstream()), captured_logs() as logs, running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    line = request_lines(logs)[0]
    assert line["hold"]["ms"] == MS32 and line["retry_after_s"] == 60
    assert p.holds["refused"] == 1


def test_held_passthrough_is_counted():
    p = held_proxy()
    up = Upstream({"model-a": [httpx.Response(400, json={"error": {"message": "bad"}})]})
    with mocked(up), running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert r.status_code == 400 and p.holds["passthrough"] == 1


def test_held_stream_that_fails_after_the_first_byte_logs_hold_and_counts_served_once():
    p = held_proxy()
    up = Upstream({"model-a": [streamed(sse(chunk("partial"), done=False), error=httpx.ReadError("cut"))]})
    with mocked(up), captured_logs() as logs, running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body(stream=True), headers=AUTH)
    assert r.status_code == 200 and "upstream stream failed" in r.text
    lines = request_lines(logs)
    assert len(lines) == 1 and lines[0]["reason"] == "stream_error"
    assert lines[0]["hold"] == {"model": "model-a", "limits": ["tpm"], "ms": MS32}
    assert p.holds == {"served": 1, "passthrough": 0, "refused": 0, "cancelled": 0, "error": 0}


def test_not_held_line_has_no_hold_field():
    p = make_proxy(hold_max_s=60)
    with mocked(Upstream()), captured_logs() as logs, running(p) as c:
        c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert "hold" not in request_lines(logs)[0]
    assert sum(p.holds.values()) == 0


SCOPE = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
         "method": "POST", "scheme": "http", "path": "/v1/chat/completions",
         "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
         "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 4000),
         "headers": [(b"host", b"localhost:4000"), (b"content-type", b"application/json"),
                     (b"authorization", AUTH["Authorization"].encode())]}


def _held_disconnect(p, receive_after_body, *, cancel_handler: bool = False) -> list[dict]:
    from quota_router.app import create_app
    sent: list[dict] = []
    body = json.dumps(chat_body()).encode()

    async def scenario():
        app = create_app(p)
        msgs = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if msgs:
                return msgs.pop(0)
            return await receive_after_body()

        async def send(message):
            sent.append(message)

        async with app.router.lifespan_context(app):
            task = asyncio.ensure_future(app(SCOPE, receive, send))
            if cancel_handler:
                for _ in range(200):
                    if p.router.holds_in_flight:
                        break
                    await asyncio.sleep(0.005)
                assert p.router.holds_in_flight == 1
                p.clock.advance(7)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                for _ in range(5):
                    await asyncio.sleep(0)  # the routing task takes its cancellation
            else:
                await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    return sent


def _assert_cancelled_hold(p, up, logs, rpd_before, ms):
    assert up.requests == []
    assert p.store.snapshot()["model-a"]["rpd_used"] == rpd_before  # nothing reserved
    line = request_lines(logs)[0]
    assert line["status"] == 499 and line["reason"] == "client_disconnected"
    assert line["hold"] == {"model": "model-a", "limits": ["tpm"], "ms": ms}
    assert p.holds["cancelled"] == 1 and sum(p.holds.values()) == 1
    assert p.router.holds_in_flight == 0
    assert p.body_budget.used == 0


def test_disconnect_during_the_hold():
    p = held_proxy()
    rpd = p.store.snapshot()["model-a"]["rpd_used"]

    async def never_wakes(s):
        await asyncio.sleep(10)
    p.router.sleep = never_wakes

    async def leave():
        await asyncio.sleep(0.1)
        p.clock.advance(5)
        return {"type": "http.disconnect"}
    up = Upstream()
    with mocked(up), captured_logs() as logs:
        sent = _held_disconnect(p, leave)
    assert sent[0]["status"] == 499
    _assert_cancelled_hold(p, up, logs, rpd, 5000)


def test_disconnect_at_the_moment_of_waking():
    p = held_proxy()
    rpd = p.store.snapshot()["model-a"]["rpd_used"]
    woke = asyncio.Event()
    p.router.sleep = fake_sleep(p.clock, on_sleep=woke.set)

    async def leave_on_wake():
        await woke.wait()
        return {"type": "http.disconnect"}
    up = Upstream()
    with mocked(up), captured_logs() as logs:
        sent = _held_disconnect(p, leave_on_wake)
    assert sent[0]["status"] == 499
    _assert_cancelled_hold(p, up, logs, rpd, MS32)


def test_cancelled_handler_during_the_hold():
    p = held_proxy()
    rpd = p.store.snapshot()["model-a"]["rpd_used"]

    async def never_wakes(s):
        await asyncio.sleep(10)
    p.router.sleep = never_wakes

    async def stay():
        await asyncio.sleep(30)
        return {"type": "http.disconnect"}
    up = Upstream()
    with mocked(up), captured_logs() as logs:
        _held_disconnect(p, stay, cancel_handler=True)
    _assert_cancelled_hold(p, up, logs, rpd, 7000)


def test_routing_error_after_a_hold_is_counted_as_error():
    p = held_proxy()
    real = p.store.try_reserve
    calls = {"n": 0}

    def boom(model, tokens, **kw):
        calls["n"] += 1
        if calls["n"] > 2:  # the walk after the hold
            raise RuntimeError("bug")
        return real(model, tokens, **kw)
    p.store.try_reserve = boom  # type: ignore[method-assign]
    with mocked(Upstream()), captured_logs() as logs, running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert r.status_code == 500
    assert request_lines(logs)[0]["hold"]["ms"] == MS32
    assert p.holds["error"] == 1 and p.router.holds_in_flight == 0


# ---- Codex adversarial review, 2026-11-24 -----------------------------------

def _gone_soon(e: Env, gone: dict):
    """A sleep that moves the clock and makes the disconnect ready only on the loop's next
    round, without yielding: the watcher hasn't run when the router resumes."""
    async def sleep(s: float) -> None:
        e.sleeps.append(s)  # type: ignore[attr-defined]
        e.clock.advance(s)
        asyncio.get_running_loop().call_soon(gone.update, {"v": True})
    return sleep


def test_disconnect_ready_at_waking_but_not_yet_seen_is_never_sent():
    """Finding 1: a ready disconnect reaches the watcher before the held request is sent."""
    e = env_(routes={"fast": ["a"]})
    fill_tpm(e, "a")
    rpd = e.store.snapshot()["model-a"]["rpd_used"]
    gone = {"v": False}
    e.router.sleep = _gone_soon(e, gone)
    up = ScriptedUpstream()
    with pytest.raises(asyncio.CancelledError):
        e.route(chat(), up, gone=lambda: gone["v"])
    assert up.called == [] and e.store.snapshot()["model-a"]["rpd_used"] == rpd


def test_disconnect_ready_after_reserving_releases_the_reservation():
    """Finding 1: the checkpoint after reserving gives the reservation back."""
    e = env_(routes={"fast": ["a"]})
    fill_tpm(e, "a")
    rpd = e.store.snapshot()["model-a"]["rpd_used"]
    gone = {"v": False}
    real = e.store.try_reserve

    def reserve(model, tokens, **kw):
        res = real(model, tokens, **kw)
        if e.sleeps:  # the walk after the hold: the client leaves now
            asyncio.get_running_loop().call_soon(gone.update, {"v": True})
        return res
    e.store.try_reserve = reserve  # type: ignore[method-assign]
    up = ScriptedUpstream()
    with pytest.raises(asyncio.CancelledError):
        e.route(chat(), up, gone=lambda: gone["v"])
    snap = e.store.snapshot()["model-a"]
    assert up.called == [] and snap["rpd_used"] == rpd and snap["tpm_input_used"] == 0


def test_hold_does_not_open_the_gate_for_a_fresh_short_owner_block():
    """Finding 2: the first walk kept the owner; after the hold the owner's window was taken
    again (a fresh 59 s block). The fresh gate keeps it: no handoff to the scarce target."""
    models = {"a": model_dict("model-a", tpm_input=TPM), "b": model_dict("model-b", tpm_input=TPM, rpd=20)}
    e = env_(models, handoff_targets=["b"])
    req = pinned_to(e, "model-a")
    fill_tpm(e, "a")

    def taken():  # at the wake time, then the held request resumes 1 s later
        e.store.try_reserve(e.model("a"), TPM - 5)
        e.clock.advance(1)
    e.router.sleep = fake_sleep(e.clock, e.sleeps, on_sleep=taken)
    up = ScriptedUpstream()
    out = e.route(req, up)
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 59
    assert up.called == [] and out.handoff_from is None


def test_hold_sleeps_the_checked_wait_despite_a_clock_step():
    """Finding 3: a wall-clock step back after the wait was checked can't stretch the sleep."""
    e = env_(routes={"fast": ["a"]}, hold_max_s=33)
    fill_tpm(e, "a")
    real = e.router._hold_for

    def step_back(*a):
        found = real(*a)
        e.clock.advance(-3600)
        return found
    e.router._hold_for = step_back  # type: ignore[method-assign]
    e.route(chat(), ScriptedUpstream())
    assert e.sleeps == [W32]


def test_time_running_out_after_a_hold_is_503_not_429():
    """Finding 4: the second walk loses its margin while other requests hold the windows."""
    e = env_(request_deadline_s=40, hold_max_s=35)
    for key in ("a", "b"):
        e.store.try_reserve(e.model(key), TPM - 5)
    e.clock.advance(28)

    real = e.store.try_reserve
    woke = {"v": False}

    def taken():
        for key in ("a", "b"):
            real(e.model(key), TPM - 5)
        woke["v"] = True
    e.router.sleep = fake_sleep(e.clock, e.sleeps, on_sleep=taken)

    def slow(model, tokens, **kw):
        res = real(model, tokens, **kw)
        if woke["v"] and model.key == "a":
            e.clock.advance(4)  # 8 s were left at waking; 4 s now
        return res
    e.store.try_reserve = slow  # type: ignore[method-assign]
    out = e.route(chat(), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.status == 503
    assert [c.reason for c in out.candidates] == ["quota", "not_tried"]


# ---- code-reviewer-deep, 2026-11-24 -----------------------------------------

def test_window_that_freed_since_its_check_is_walked_again_at_once():
    """Finding 2: ready_at already passed when the hold is decided: a wait of 0, then served."""
    e = env_(routes={"fast": ["a"]})
    fill_tpm(e, "a", wait=0.001)
    real = e.store.try_reserve

    def late(model, tokens, **kw):
        res = real(model, tokens, **kw)
        if not e.sleeps:
            e.clock.advance(0.01)  # the window frees while the first walk finishes
        return res
    e.store.try_reserve = late  # type: ignore[method-assign]
    up = ScriptedUpstream()
    out = e.route(chat(), up)
    assert isinstance(out, RouteServed) and up.called == ["model-a"]
    assert e.sleeps == [HOLD_PAD_S]


def test_hold_sleeps_past_the_window_edge():
    """Finding 3: the sleep ends after the wake time, so the window has freed when it wakes."""
    e = env_(routes={"fast": ["a"]})
    fill_tpm(e, "a")
    e.route(chat(), ScriptedUpstream())
    assert e.sleeps[0] > 32


# ---- the timing invariant, over a grid (the area drew review findings twice) -------------

def test_hold_timing_invariant_over_a_grid():
    """For every combination of wait, arrival age, deadline, hold_max_s, sleep overshoot and a
    slow reservation, with or without another request taking the window at the wake time: a
    held request sleeps more than 0 and at most hold_max_s + pad; any call
    it makes starts with at least MIN_ATTEMPT_S of the deadline left; after a hold, a 429 comes
    only from quota checked while that margin remained; hold_max_s 0 never sleeps; and a request is
    either served or answered 429/503, never held twice."""
    import itertools
    from quota_router.config import MIN_ATTEMPT_S
    checked = 0
    for wait, ago, deadline, max_s, over, slow, taken in itertools.product(
            (0.5, 5, 31.9, 32, 59.9), (0, 2, 3, 10, 30), (40, 65, 180), (0, 20, 35, 60),
            (0, 0.5, 2), (0, 1), (False, True)):
        if max_s and max_s + MIN_ATTEMPT_S > deadline:
            continue  # rejected by config
        e = env_(routes={"fast": ["a"]}, request_deadline_s=deadline, hold_max_s=max_s)
        fill_tpm(e, "a", wait=wait)
        real = e.store.try_reserve
        e.router.sleep = fake_sleep(e.clock, e.sleeps, extra=over, on_sleep=(
            (lambda _r=real, _e=e: _r(_e.model("a"), TPM - 5)) if taken else None))

        def reserve(model, tokens, _real=real, _e=e, _slow=slow, **kw):
            res = _real(model, tokens, **kw)
            if _e.sleeps and _slow:
                _e.clock.advance(_slow)
            return res
        e.store.try_reserve = reserve  # type: ignore[method-assign]
        arrived = e.clock.now() - ago
        end = arrived + deadline
        up = timed_upstream(e)
        hold = Hold()
        out = e.route(chat(), up, hold=hold, started_at=arrived)
        case = (wait, ago, deadline, max_s, over, slow, taken)
        assert len(e.sleeps) <= 1, case
        if max_s == 0:
            assert e.sleeps == [] and not hold.held, case
        if e.sleeps:
            assert hold.held and 0 < e.sleeps[0] <= max_s + HOLD_PAD_S, case
            for t in up.at:
                assert end - t >= MIN_ATTEMPT_S, case
            if isinstance(out, RouteFailed) and out.status == 429:
                # A 429 after a hold only from quota checked while the margin remained (§25.4):
                # a candidate whose margin was gone is not_tried, which makes a 503.
                for c in out.candidates:
                    assert c.reason == "quota" and end - c.checked_at >= MIN_ATTEMPT_S, case
        assert isinstance(out, RouteServed) or out.status in (429, 503), case
        if wait <= max_s and end - (e.clock.now()) >= 0 and not e.sleeps:
            # not held although the wait fits: only for want of margin (5 s + slack)
            assert end - (arrived + ago + wait) < MIN_ATTEMPT_S + 1.0 or wait > max_s, case
        checked += 1
    assert checked > 2000
