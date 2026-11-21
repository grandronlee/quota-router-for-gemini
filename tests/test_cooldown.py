"""Growing 5xx cooldown for overloaded models (DESIGN §22)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from apphelpers import (AUTH, BASE, Upstream, captured_logs, chat_body, chunk, error_body, make_proxy, mocked,
                        ok, rate_limited, running, sse, streamed)
from helpers import Env, ScriptedUpstream, chat, make_config, model_dict
from quota_router.quota import UNAVAILABLE_COOLDOWN_S, unavailable_cooldown_s
from quota_router.router import (RateLimited, Rejected, RouteFailed, RouteServed, Served, Unavailable)
from quota_router.scrub import Scrubber
from quota_router.upstream import GeminiUpstream, HandleSet, make_client
from test_handoff import call, env3, turn

FAIL = Unavailable("http 503", counts=True, status=503)


def env1(**top) -> Env:
    return Env(make_config({"a": model_dict("model-a")}, {"fast": ["a"]}, **top))


def env2(**top) -> Env:
    return Env(make_config(None, None, **top))


def streak(e, key: str = "a") -> int:
    return e.store.snapshot()[e.model(key).id]["unavailable_streak"]


def fail_times(e: Env, n: int, key: str = "a") -> None:
    """`n` counted failures in a row, each after the previous hard 30 s."""
    m = e.model(key)
    for _ in range(n):
        res = e.store.try_reserve(m, 10, last_resort=True)
        assert e.store.on_failure(m, res)
        e.store.release_slot(res)
        e.clock.advance(UNAVAILABLE_COOLDOWN_S)


def post(client, body):
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


def request_lines(logs) -> list[dict]:
    out = []
    for line in logs.getvalue().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event") == "request":
            out.append(obj)
    return out


# ---- cooldown sequence ---------------------------------------------------------

def test_cooldown_grows_by_four_from_30s_and_caps_at_15_min():
    assert [unavailable_cooldown_s(n) for n in range(1, 7)] == [30, 120, 480, 900, 900, 900]
    assert unavailable_cooldown_s(10_000) == 900  # no overflow


def test_store_sequence_hard_30s_then_soft_until_the_cooldown_ends():
    e = env1()
    m = e.model("a")
    t0 = e.clock.now()
    for n, length in enumerate([30, 120, 480, 900, 900], start=1):
        res = e.store.try_reserve(m, 10, last_resort=True)
        assert e.store.on_failure(m, res)
        e.store.release_slot(res)
        assert streak(e) == n
        hard = e.store.check(m, 10)
        assert hard.reason == "unavailable" and not hard.deferred  # the first 30 s block it
        assert e.store.try_reserve(m, 10, last_resort=True).__class__.__name__ == "Blocked"
        e.clock.advance(30)
        if length > 30:
            soft = e.store.check(m, 10)
            assert soft.deferred and soft.cooling_down  # only moved to the back
            assert e.store.check(m, 10, last_resort=True) is None
            snap = e.store.snapshot()[m.id]
            assert snap["cooldown_until"] == pytest.approx(e.clock.now() - 30 + length)
        else:
            assert e.store.check(m, 10) is None
    assert e.clock.now() == t0 + 5 * 30


# ---- what counts ---------------------------------------------------------------

def test_5xx_counts_and_a_later_success_recovers():
    e = env2()
    up = ScriptedUpstream({"model-a": [FAIL]})
    assert e.route(chat(), up).model.id == "model-b"
    assert streak(e) == 1
    e.clock.advance(31)  # hard part over; 2 min soft would follow a second failure
    out = e.route(chat(), up)
    assert out.model.id == "model-a"  # no soft part after the first failure: tried normally
    assert up.called == ["model-a", "model-b", "model-a"]
    assert streak(e) == 0  # it served: recovered


def test_full_budget_timeout_after_sending_counts():
    e = env1()
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    assert isinstance(e.route(chat(), up), RouteFailed)
    assert streak(e) == 1


@pytest.mark.parametrize("result", [
    Unavailable("timeout", body_sent=False),            # send phase unknown, or a write timeout
    Unavailable("timeout", not_sent=True),              # connect or pool timeout
    Unavailable("error: ConnectError", not_sent=True),  # network down
    Unavailable("error: ReadError", body_sent=True),    # not in the counting list
])
def test_failures_that_dont_count_keep_todays_30s(result):
    e = env1()
    up = ScriptedUpstream({"model-a": result}, on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up)
    assert streak(e) == 0
    assert e.store.check(e.model("a"), 10).reason == "unavailable"  # 30 s, as today
    e.clock.advance(30)
    assert e.store.check(e.model("a"), 10) is None  # and no soft part


def test_early_timeout_after_sending_doesnt_count():
    e = env1()
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t / 2))
    e.route(chat(), up)
    assert streak(e) == 0


def test_deadline_shortened_timeout_doesnt_count_or_cool_down():
    e = env1()
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up, started_at=e.clock.now() - 170)  # 10 s left
    assert streak(e) == 0
    assert e.store.check(e.model("a"), 10) is None


def test_client_cancellation_doesnt_count_and_frees_the_slot():
    e = env1()
    fail_times(e, 1)  # streak 1, hard part over: the next attempt takes the recovery slot
    st = e.store._state["model-a"]

    async def hang(model, timeout_s, *, body=None):
        await asyncio.sleep(3600)

    async def main():
        attempts = []
        task = asyncio.ensure_future(e.router.route(chat(), hang, attempts=attempts))
        await asyncio.sleep(0.01)
        assert st.slot is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return attempts

    attempts = asyncio.run(main())
    assert st.slot is None and streak(e) == 1
    assert [a.log()["outcome"] for a in attempts] == ["client_cancelled"]


# ---- recovery ------------------------------------------------------------------

@pytest.mark.parametrize("result", [
    Served({"ok": 1}),
    RateLimited(b'{"error": {"code": 429}}'),
    Rejected({"bad": 1}, status=400),
    Rejected({"bad": 1}, status=422),
])
def test_an_answer_resets_the_streak(result):
    e = env1()
    fail_times(e, 2)  # 2 min cooldown; 30 s of it used
    e.route(chat(), ScriptedUpstream({"model-a": result}))  # tried as the last resort
    assert streak(e) == 0
    if not isinstance(result, RateLimited):
        assert e.store.check(e.model("a"), 10) is None  # the 5xx cooldown ended


def test_other_rejections_are_neutral():
    e = env1()
    fail_times(e, 1)
    e.route(chat(), ScriptedUpstream({"model-a": Rejected({"x": 1}, status=413)}))
    assert streak(e) == 1


def test_streams_recover_at_done_not_at_the_first_event():
    p = make_proxy()
    fail_times_store(p, "a", 1)
    seen = []

    def before(n, part):
        if part == b"data: [DONE]\n\n":  # the first events are already through
            seen.append(p.store.snapshot()["model-a"]["unavailable_streak"])

    up = Upstream({"model-a": [streamed(sse(chunk("hi"), chunk(" there")), before=before)]})
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
    assert r.headers["x-served-model"] == "model-a"
    assert seen == [1]
    assert p.store.snapshot()["model-a"]["unavailable_streak"] == 0


def test_mid_stream_5xx_counts():
    p = make_proxy()
    err = error_body(503, "UNAVAILABLE", "overloaded")
    up = Upstream({"model-a": [streamed(sse(chunk("hi"), err, done=False))]})
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
    assert p.store.snapshot()["model-a"]["unavailable_streak"] == 1


def test_stream_stopping_without_done_is_neutral():
    p = make_proxy()
    fail_times_store(p, "a", 1)
    up = Upstream({"model-a": [streamed(sse(chunk("hi"), done=False))]})
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
    assert p.store.snapshot()["model-a"]["unavailable_streak"] == 1


def test_mid_stream_429_resets_the_streak():
    p = make_proxy()
    fail_times_store(p, "a", 1)
    err = error_body(429, "RESOURCE_EXHAUSTED", "quota")
    up = Upstream({"model-a": [streamed(sse(chunk("hi"), err, done=False))]})
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
    assert p.store.snapshot()["model-a"]["unavailable_streak"] == 0


def fail_times_store(p, key: str, n: int) -> None:
    m = p.config.models[key]
    for _ in range(n):
        res = p.store.try_reserve(m, 10, last_resort=True)
        assert p.store.on_failure(m, res)
        p.store.release_slot(res)
        p.clock.advance(UNAVAILABLE_COOLDOWN_S)


# ---- stale outcomes --------------------------------------------------------------

def test_two_parallel_failures_count_once():
    e = env1()

    async def slow_fail(model, timeout_s, *, body=None):
        await asyncio.sleep(0.01)
        return FAIL

    async def main():
        return await asyncio.gather(*(e.router.route(chat(), slow_fail) for _ in range(2)))

    outs = asyncio.run(main())
    assert all(isinstance(o, RouteFailed) for o in outs)
    assert streak(e) == 1
    assert e.store.snapshot()["model-a"]["cooldown_until"] == pytest.approx(e.clock.now() + 30)


def test_old_in_flight_success_doesnt_reset_and_late_failure_doesnt_cool_down():
    e = env1()
    m = e.model("a")
    old_ok = e.store.try_reserve(m, 10)
    fails = e.store.try_reserve(m, 10)
    assert e.store.on_failure(m, fails)
    assert not e.store.on_recovered(m, old_ok)  # from before the failure: stale
    assert streak(e) == 1
    e.clock.advance(30)
    probe = e.store.try_reserve(m, 10)
    assert e.store.try_reserve(m, 10).deferred  # the slot is held
    assert e.store.on_recovered(m, probe)
    e.store.release_slot(probe)
    assert streak(e) == 0
    assert not e.store.on_failure(m, old_ok)  # from before the recovery: stale
    assert e.store.check(m, 10) is None  # no cooldown


def test_recovery_at_streak_0_keeps_the_epoch():
    """A busy healthy model still gets today's cooldown from a 503 after other 200s."""
    e = env1()
    m = e.model("a")
    first, second = e.store.try_reserve(m, 10), e.store.try_reserve(m, 10)
    assert not e.store.on_recovered(m, first)  # nothing to change
    assert e.store.on_failure(m, second)
    assert streak(e) == 1


# ---- one recovery attempt at a time ------------------------------------------------

def test_concurrent_requests_dont_both_take_the_slot():
    e = env2()
    fail_times(e, 1)  # model-a: streak 1, hard part over, no soft part
    calls = []

    async def up(model, timeout_s, *, body=None):
        calls.append(model.id)
        await asyncio.sleep(0.01)
        return Served({"ok": model.id})

    async def main():
        return await asyncio.gather(*(e.router.route(chat(), up) for _ in range(2)))

    outs = asyncio.run(main())
    assert sorted(o.model.id for o in outs) == ["model-a", "model-b"]
    assert calls.count("model-a") == 1  # the other request skipped it as recovering
    assert e.store._state["model-a"].slot is None
    assert streak(e) == 0


def test_slot_is_freed_after_a_failure():
    e = env1()
    fail_times(e, 1)
    e.route(chat(), ScriptedUpstream({"model-a": FAIL}))
    assert e.store._state["model-a"].slot is None and streak(e) == 2


def test_recovering_request_returns_503_when_nothing_else_can_serve():
    e = env1()
    fail_times(e, 1)
    gate = {}

    async def up(model, timeout_s, *, body=None):
        await gate["ev"].wait()
        return Served({"ok": 1})

    async def main():
        gate["ev"] = asyncio.Event()
        first = asyncio.ensure_future(e.router.route(chat(), up))
        await asyncio.sleep(0.01)
        second = await e.router.route(chat(), up)
        gate["ev"].set()
        return await first, second

    first, second = asyncio.run(main())
    assert isinstance(first, RouteServed)
    assert isinstance(second, RouteFailed) and second.status == 503
    assert "recovery attempt in flight" in second.message
    assert second.retry_after_s == 5  # not 1: the attempt in flight can take up to 120 s


# ---- the last-resort pass ----------------------------------------------------------

def test_soft_model_is_skipped_while_another_serves():
    e = env2()
    fail_times(e, 2)
    up = ScriptedUpstream()
    out = e.route(chat(), up)
    assert out.model.id == "model-b" and up.called == ["model-b"]


def test_soft_model_is_tried_last_when_nothing_else_serves():
    e = env2()
    fail_times(e, 2)
    up = ScriptedUpstream({"model-b": FAIL})
    out = e.route(chat(), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-a"
    assert up.called == ["model-b", "model-a"] and out.path == ["model-b", "model-a"]


def test_last_resort_respects_max_attempts():
    models = {k: model_dict(f"model-{k}") for k in "abc"}
    e = Env(make_config(models, {"fast": ["a", "b", "c"]}, max_attempts=2))
    fail_times(e, 2)
    up = ScriptedUpstream({"model-b": FAIL, "model-c": FAIL})
    out = e.route(chat(), up)
    assert isinstance(out, RouteFailed) and up.called == ["model-b", "model-c"]
    a = out.candidates[0]
    assert a.reason == "unavailable" and a.detail == "5xx cooldown, tried last"


def test_last_resort_respects_the_deadline():
    e = env2()
    fail_times(e, 2)
    up = ScriptedUpstream({"model-b": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up)  # model-b uses the 120 s, leaving 60 s for the last resort
    assert up.called == ["model-b", "model-a"]
    e2 = env2()
    fail_times(e2, 2)
    up2 = ScriptedUpstream({"model-b": FAIL}, on_call=lambda m, t: e2.clock.advance(t - 2))
    out = e2.route(chat(), up2, started_at=e2.clock.now() - 60)  # 120 s left; 2 s after model-b
    assert isinstance(out, RouteFailed) and up2.called == ["model-b"]


def test_hard_blocked_model_is_never_tried():
    e = env2()
    m = e.model("a")
    res = e.store.try_reserve(m, 10)
    e.store.on_failure(m, res)  # just failed: in its hard 30 s
    up = ScriptedUpstream({"model-b": FAIL})
    out = e.route(chat(), up)
    assert isinstance(out, RouteFailed) and up.called == ["model-b"]


def test_pinned_owner_in_soft_cooldown_is_tried_after_its_targets_fail():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    fail_times(env, 2)
    up = ScriptedUpstream({"model-b": FAIL, "model-c": FAIL})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-a"
    assert up.called == ["model-b", "model-c", "model-a"]
    assert out.handoff_from is None and up.bodies[2] is None  # its own turn, body unchanged


# ---- visibility ------------------------------------------------------------------

def test_attempts_in_the_log_line_and_streak_in_status():
    p = make_proxy()
    secret = "internal detail that must not be logged"
    up = Upstream({"model-a": [httpx.Response(503, json=error_body(503, "UNAVAILABLE", secret))],
                   "model-b": [ok("fine")]})
    with mocked(up), running(p) as c, captured_logs() as logs:
        assert post(c, chat_body()).status_code == 200
        status = c.get("/status", headers=AUTH).json()
    line = request_lines(logs)[-1]
    assert [(a["model"], a["outcome"], a["status"]) for a in line["attempts"]] == [
        ("model-a", "unavailable", 503), ("model-b", "ok", 200)]
    assert all(set(a) == {"model", "outcome", "status", "ms"} and isinstance(a["ms"], int)
               for a in line["attempts"])
    assert secret not in logs.getvalue()
    assert status["models"]["model-a"]["unavailable_streak"] == 1
    assert status["models"]["model-b"]["unavailable_streak"] == 0


def test_stream_attempt_outcomes_in_the_log_line():
    p = make_proxy()
    err = error_body(503, "UNAVAILABLE", "overloaded")
    up = Upstream({"model-a": [rate_limited(), streamed(sse(chunk("hi"))), streamed(sse(chunk("x"), err, done=False)),
                               streamed(sse(chunk("y"), done=False))]})
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body(model="model-a", stream=True))
        p.clock.advance(60)
        post(c, chat_body(model="model-a", stream=True))
        post(c, chat_body(model="model-a", stream=True))  # a counted failure: hard 30 s
        p.clock.advance(31)
        post(c, chat_body(model="model-a", stream=True))
    lines = request_lines(logs)
    assert [a["outcome"] for a in lines[0]["attempts"]] == ["rate_limited"]
    assert [(a["outcome"], a["status"]) for a in lines[1]["attempts"]] == [("stream_complete", 200)]
    assert [(a["outcome"], a["status"]) for a in lines[2]["attempts"]] == [("stream_error", 503)]
    assert [a["outcome"] for a in lines[3]["attempts"]] == ["stream_incomplete"]


# ---- send phase (httpx trace) ------------------------------------------------------

class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, *, trace: bool, exc: Exception | None = None) -> None:
        self.trace = trace
        self.exc = exc

    async def handle_async_request(self, request):
        cb = request.extensions.get("trace")
        if self.trace and cb:
            await cb("http11.send_request_body.complete", {})
        if self.exc:
            raise self.exc
        await asyncio.sleep(3600)


def _call(transport, timeout_s: float = 0.05):
    async def main():
        client = make_client(transport)
        try:
            up = GeminiUpstream(client, BASE, "k", Scrubber(["k"]))
            return await up.bind(chat(), False, HandleSet())(model_dict_cfg(), timeout_s)
        finally:
            await client.aclose()
    return asyncio.run(main())


def model_dict_cfg():
    return make_config().models["a"]


def test_timeout_after_the_body_was_written_is_marked_sent():
    r = _call(_Transport(trace=True))
    assert r == Unavailable("timeout", body_sent=True)


def test_timeout_without_trace_is_not_marked_sent():
    assert _call(_Transport(trace=False)).body_sent is False


def test_write_timeout_is_not_marked_sent():
    r = _call(_Transport(trace=True, exc=httpx.WriteTimeout("slow upload")))
    assert r.detail == "timeout" and not r.body_sent


def test_real_httpcore_reports_the_body_written():
    """The trace event name is httpcore's: checked against a server that never answers."""
    async def main():
        async def handler(reader, writer):
            await reader.read(65536)
            await asyncio.sleep(3600)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = make_client(trust_env=False)
        try:
            up = GeminiUpstream(client, f"http://127.0.0.1:{port}/v1", "k", Scrubber(["k"]))
            return await up.bind(chat(), False, HandleSet())(model_dict_cfg(), 0.3)
        finally:
            await client.aclose()
            server.close()

    assert asyncio.run(main()) == Unavailable("timeout", body_sent=True)


# ---- review fixes ------------------------------------------------------------------

def test_busy_recovery_slot_still_hands_off_when_the_owner_is_also_quota_blocked():
    """A short RPM wait alone would keep the turn on its owner; a failing owner hands off."""
    models = {"a": model_dict("model-a", rpm=1), "b": model_dict("model-b"), "c": model_dict("model-c")}
    env = Env(make_config(models, {"fast": ["a", "b", "c"]}, handoff_targets=["b", "c"]))
    env.affinity.record(["s1"], "model-a")
    fail_times(env, 1)
    env.clock.advance(30)  # the failed call has left the minute window
    holder = env.store.try_reserve(env.model("a"), 10)
    assert holder.slot  # a recovery attempt in flight, filling the RPM window
    env.clock.advance(10)  # RPM frees in 50 s, under handoff_after_s
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b" and up.called == ["model-b"]


def test_timeout_cut_by_the_deadline_by_under_5s_doesnt_count():
    e = env1()
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up, started_at=e.clock.now() - 63)  # 117 s left: under the 120 s budget
    assert up.calls[0][1] == pytest.approx(117)
    assert streak(e) == 0
    assert e.store.check(e.model("a"), 10).reason == "unavailable"  # today's 30 s still applies


def test_stale_uncounted_failure_doesnt_block_a_recovered_model():
    e = env1()
    m = e.model("a")

    async def main():
        ev = asyncio.Event()

        async def up(model, timeout_s, *, body=None):
            await ev.wait()
            return Unavailable("error: ReadError", body_sent=True)

        task = asyncio.ensure_future(e.router.route(chat(), up))
        await asyncio.sleep(0.01)  # reserved at the old epoch
        fail = e.store.try_reserve(m, 10)
        assert e.store.on_failure(m, fail)
        e.clock.advance(30)
        probe = e.store.try_reserve(m, 10)
        assert e.store.on_recovered(m, probe)
        e.store.release_slot(probe)
        ev.set()
        return await task

    assert isinstance(asyncio.run(main()), RouteFailed)
    assert streak(e) == 0
    assert e.store.check(m, 10) is None  # the delayed failure set no cooldown


class _StalledBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""


class _HeadersThenStall(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        return httpx.Response(200, stream=_StalledBody())


def test_timeout_after_headers_keeps_the_http_status():
    assert _call(_HeadersThenStall()).status == 200
    assert _call(_Transport(trace=True)).status is None  # before headers: none


def test_busy_slot_retry_after_reaches_the_client_and_quota_keeps_its_own():
    p = make_proxy(routes={"solo": ["a"]})
    fail_times_store(p, "a", 1)
    holder = p.store.try_reserve(p.config.models["a"], 10)  # a recovery attempt in flight
    assert holder.slot
    with mocked(Upstream()), running(p) as c:
        r = post(c, chat_body(model="solo"))
    assert r.status_code == 503 and r.headers["retry-after"] == "5"

    q = make_proxy(routes={"solo": ["a"]})
    up = Upstream({"model-a": [rate_limited(delay="20s")]})
    with mocked(up), running(q) as c:
        r = post(c, chat_body(model="solo"))
    assert r.status_code == 429 and r.headers["retry-after"] == "20"
