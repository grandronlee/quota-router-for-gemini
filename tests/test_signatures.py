"""Restoring thought signatures that clients drop (DESIGN §23)."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import stat

import httpx
import pytest

from apphelpers import (AUTH, LOCAL, TOKEN, Upstream, captured_logs, chat_body, chunk, error_body,
                        make_proxy, mocked, ok, running, sse, streamed)
from helpers import T0, Env, FailingConn, ScriptedUpstream, chat, make_config, model_dict
from quota_router.affinity import (MAX_SIGNATURE_CHARS, AffinityRecord, AffinityStore, Recorded,
                                   signature_state)
from quota_router.clock import FakeClock
from quota_router.dashboard import SESSION_HEADER
from quota_router.db import MIGRATIONS, SCHEMA, SCHEMA_VERSION, connect
from quota_router.router import (PLACEHOLDER_SIGNATURE, RouteFailed,
                                 RouteServed, estimate_input_tokens, with_signatures)

SIG_A = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB" + "a1b2c3d4" * 20  # base64-shaped, never in any log
SIG_B = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJC" + "z9y8x7w6" * 20
SIG_C = "Q0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0ND" + "m5n4o3p2" * 20


# ---- shapes ---------------------------------------------------------------------------

def signed_completion(calls: list[tuple[str, str | None]], text: str | None = None) -> dict:
    """A completion with tool calls; each (id, signature), None for no signature."""
    tcs = []
    for i, sig in calls:
        tc = {"id": i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        if sig is not None:
            tc["extra_content"] = {"google": {"thought_signature": sig}}
        tcs.append(tc)
    msg = {"role": "assistant", "content": text}
    if tcs:
        msg["tool_calls"] = tcs
    return {"id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 20}}


def signed_ok(calls: list[tuple[str, str | None]]) -> httpx.Response:
    return httpx.Response(200, json=signed_completion(calls))


def delta(calls: list[tuple[int, str | None, str | None]], usage: dict | None = None) -> dict:
    """A stream chunk with tool-call deltas: each (tool_call index, id or None, signature or None)."""
    tcs = []
    for n, i, sig in calls:
        tc: dict = {"index": n}
        if i is not None:
            tc.update({"id": i, "type": "function", "function": {"name": "f", "arguments": "{}"}})
        if sig is not None:
            tc["extra_content"] = {"google": {"thought_signature": sig}}
        tcs.append(tc)
    out: dict = {"object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
    if usage:
        out["usage"] = usage
    return out


USAGE = {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 20}


def copilot_follow_up(ids: list[str], model: str = "fast") -> dict:
    """Copilot Chat's shape: the assistant tool calls come back without `extra_content`."""
    body = chat_body(model)
    body["messages"] += [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for i in ids]},
        *({"role": "tool", "tool_call_id": i, "content": "file text"} for i in ids)]
    return body


def tc(i: str | None, sig: str | None = None) -> dict:
    out: dict = {"type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    if i is not None:
        out["id"] = i
    if sig is not None:
        out["extra_content"] = {"google": {"thought_signature": sig}}
    return out


def turn(*steps: list[dict], prefix: list[dict] | None = None) -> list[dict]:
    msgs = list(prefix or []) + [{"role": "user", "content": "do it"}]
    for calls in steps:
        msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
        msgs += [{"role": "tool", "tool_call_id": c.get("id", "x"), "content": "ok"} for c in calls]
    return msgs


def sent_signature(body: dict, call_id: str):
    for m in body["messages"]:
        for t in m.get("tool_calls") or []:
            if t.get("id") == call_id:
                return ((t.get("extra_content") or {}).get("google") or {}).get("thought_signature")
    raise KeyError(call_id)


def stored(p_or_env, call_id: str) -> AffinityRecord | None:
    return p_or_env.affinity.lookup_records([call_id]).get(call_id)


def post(c, body):
    return c.post("/v1/chat/completions", json=body, headers=AUTH)


def request_lines(logs) -> list[dict]:
    return [json.loads(ln) for ln in logs.getvalue().splitlines() if '"event": "request"' in ln]


def sig_metric(c, model: str, action: str) -> float:
    for line in c.get("/metrics", headers=AUTH).text.splitlines():
        if line.startswith(f'qr_thought_signatures_total{{action="{action}",model="{model}"}}'):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def env1(**top) -> Env:
    return Env(make_config({"a": model_dict("model-a")}, {"fast": ["a"]}, **top))


def env3(targets=("b", "c")) -> Env:
    models = {"a": model_dict("model-a"), "b": model_dict("model-b"), "c": model_dict("model-c")}
    return Env(make_config(models, {"fast": ["a", "b", "c"]}, handoff_targets=list(targets)))


def body_sent(up: ScriptedUpstream, req: dict, n: int = 0) -> dict:
    return req if up.bodies[n] is None else up.bodies[n]


# ---- the regression (DESIGN §23.7) --------------------------------------------------

def test_copilot_follow_up_on_a_pinned_turn_sends_the_stored_signature_upstream():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]),
                               httpx.Response(200, json=signed_completion([], text="done"))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        assert post(c, chat_body()).status_code == 200
        r = post(c, copilot_follow_up(["call-1"]))
    assert r.status_code == 200
    assert r.headers["x-turn-affinity"] == "pinned"
    assert up.models == ["model-a", "model-a"]
    assert sent_signature(up.body(1), "call-1") == SIG_A


# ---- signature_state ------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (None, ("", "none")), ("", ("", "none")), (5, ("", "none")), ({"x": 1}, ("", "none")),
    (SIG_A, (SIG_A, "stored")), ("a-b_c+d/e=", ("a-b_c+d/e=", "stored")),
    ("S" * MAX_SIGNATURE_CHARS, ("S" * MAX_SIGNATURE_CHARS, "stored")),
    ("S" * (MAX_SIGNATURE_CHARS + 1), (None, "too_large")),
    ("abc def", (None, "invalid")), ("abc\n", (None, "invalid")), ("sig$", (None, "invalid")),
    ("é" * 10, (None, "invalid")),
])
def test_signature_state(value, expected):
    assert signature_state(value) == expected


# ---- capture, non-streamed ------------------------------------------------------------

def test_non_streamed_capture_one_call():
    up = Upstream({"model-a": [signed_ok([("c1", SIG_A)])]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        assert sig_metric(c, "model-a", "stored") == 1
        rec = stored(p, "c1")
    assert rec == AffinityRecord("model-a", rec.turn_id, SIG_A)


def test_non_streamed_parallel_calls_first_signed_second_none():
    up = Upstream({"model-a": [signed_ok([("c1", SIG_A), ("c2", None)])]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        assert sig_metric(c, "model-a", "stored") == 1 and sig_metric(c, "model-a", "none") == 1
        assert stored(p, "c1").signature == SIG_A
        assert stored(p, "c2").signature == ""


def test_non_streamed_too_large_and_invalid_are_stored_as_unknown():
    big = "S" * (MAX_SIGNATURE_CHARS + 1)
    up = Upstream({"model-a": [signed_ok([("c1", big), ("c2", "not base64!"), ("c3", 7)])]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        assert post(c, chat_body()).status_code == 200
        assert sig_metric(c, "model-a", "too_large") == 1
        assert sig_metric(c, "model-a", "invalid") == 1
        assert sig_metric(c, "model-a", "none") == 1  # a non-string is no signature
        assert stored(p, "c1").signature is None and stored(p, "c2").signature is None
        assert stored(p, "c3").signature == ""


def test_non_streamed_one_id_twice_with_two_values_is_a_conflict():
    up = Upstream({"model-a": [signed_ok([("c1", SIG_A), ("c1", SIG_B)])]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        assert sig_metric(c, "model-a", "conflict") == 1
        assert stored(p, "c1").signature is None


# ---- capture, streamed ------------------------------------------------------------------

class Snapshot:
    """The affinity records as they were before the proxy closed its database."""

    def __init__(self, records: dict[str, AffinityRecord]) -> None:
        self.records = records

    @property
    def affinity(self):
        return self

    def lookup_records(self, ids):
        return {i: self.records[i] for i in ids if i in self.records}


def snapshot(p) -> Snapshot:
    rows = p.conn.execute("SELECT tool_call_id FROM affinity").fetchall()
    return Snapshot(p.affinity.lookup_records([r[0] for r in rows]))


def run_stream(parts, **kw):
    up = Upstream({"model-a": [streamed(parts, **kw)]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        r = post(c, chat_body(stream=True))
        metrics = {a: sig_metric(c, "model-a", a) for a in ("stored", "none", "conflict", "invalid")}
        snap = snapshot(p)
    return snap, r, metrics


def test_streamed_signature_in_the_ids_delta():
    p, r, m = run_stream(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE)))
    assert r.status_code == 200 and stored(p, "c1").signature == SIG_A
    assert m["stored"] == 1 and m["none"] == 0


def test_streamed_signature_in_a_later_delta_is_matched_by_index_and_settled_before_done():
    seen = {}
    p_box = {}

    def before(n, part):
        if part == b"data: [DONE]\n\n" and "p" in p_box:
            seen["at_done"] = stored(p_box["p"], "c1")

    parts = sse(delta([(0, "c1", None), (1, "c2", None)]), chunk("", usage=USAGE),
                delta([(1, None, SIG_B)]), delta([(0, None, SIG_A)]))
    up = Upstream({"model-a": [streamed(parts, before=before)]})
    p = p_box["p"] = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        assert stored(p, "c1").signature == SIG_A and stored(p, "c2").signature == SIG_B
    # Recorded at the usage chunk as unknown; settled after upstream's [DONE] was read (the
    # order towards the client is checked in the next test).
    assert seen["at_done"].signature is None


def test_streamed_settling_write_runs_before_the_client_gets_done():
    from quota_router.app import StreamSession
    from quota_router.upstream import SSEEvent, Stream

    p = make_proxy()

    class FakeResponse:
        async def aclose(self):
            pass

    async def events():
        yield SSEEvent(b"data: late\n\n", json.dumps(delta([(0, None, SIG_A)])))
        yield SSEEvent(b"data: [DONE]\n\n", "[DONE]")

    first = SSEEvent(b"data: first\n\n", json.dumps(delta([(0, "c1", None)], usage=USAGE)))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None)
    session = StreamSession(p, served, Stream(FakeResponse(), events(), first),  # type: ignore[arg-type]
                            request_id="r", route="fast", started=0.0, input_estimate=10)
    seen = []

    async def run():
        async for out in session.body():
            seen.append((out, stored(p, "c1").signature))
        await session.finish()

    asyncio.run(run())
    assert seen[0] == (b"data: first\n\n", None)  # recorded at the usage chunk, unknown yet
    assert seen[-1] == (b"data: [DONE]\n\n", SIG_A)  # settled before [DONE] reached the client


def test_streamed_no_signature_is_stored_as_none_at_done():
    p, _, m = run_stream(sse(delta([(0, "c1", None)]), chunk("", usage=USAGE)))
    assert stored(p, "c1").signature == ""
    assert m["none"] == 1 and m["stored"] == 0


def test_streamed_two_values_are_a_conflict_before_and_after_the_record():
    p, _, m = run_stream(sse(delta([(0, "c1", SIG_A)]), delta([(0, None, SIG_B)]), chunk("", usage=USAGE)))
    assert stored(p, "c1").signature is None and m["conflict"] == 1
    # Recorded with SIG_A at the usage chunk, contradicted later: cleared by the settling write.
    p2, _, m2 = run_stream(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE), delta([(0, None, SIG_B)])))
    assert stored(p2, "c1").signature is None and m2["conflict"] == 1
    # The same value twice is fine.
    p3, _, m3 = run_stream(sse(delta([(0, "c1", SIG_A)]), delta([(0, None, SIG_A)]), chunk("", usage=USAGE)))
    assert stored(p3, "c1").signature == SIG_A and m3["conflict"] == 0


def test_streamed_invalid_signature_stays_unknown():
    p, _, m = run_stream(sse(delta([(0, "c1", "bad sig!")]), chunk("", usage=USAGE)))
    assert stored(p, "c1").signature is None and m["invalid"] == 1


def test_streamed_cut_off_leaves_its_calls_unknown():
    # DESIGN §23.8 amendment 1: only [DONE] shows a call carries no signature.
    p, r, m = run_stream(sse(delta([(0, "c1", None), (1, "c2", None)]), chunk("", usage=USAGE),
                             delta([(0, None, SIG_A)]), done=False))
    assert b"stream_error" in r.content
    assert stored(p, "c1").signature is None and stored(p, "c2").signature is None
    assert m["stored"] == 0 and m["none"] == 0


def test_streamed_error_event_leaves_its_calls_unknown():
    err = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}
    p, r, _ = run_stream(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE), err, done=False))
    assert b"overloaded" in r.content
    assert stored(p, "c1").signature is None  # recorded as unknown, never settled


def disconnecting_session(p, first_obj, later_objs=()):
    from quota_router.app import StreamSession
    from quota_router.upstream import SSEEvent, Stream

    class FakeResponse:
        async def aclose(self):
            pass

    async def events():
        for o in later_objs:
            yield SSEEvent(b"data: x\n\n", json.dumps(o))

    first = SSEEvent(b"data: x\n\n", json.dumps(first_obj))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None)
    session = StreamSession(p, served, Stream(FakeResponse(), events(), first),  # type: ignore[arg-type]
                            request_id="r", route="fast", started=0.0, input_estimate=10)

    async def run():
        gen = session.body()
        await gen.__anext__()
        await gen.__anext__()  # the late signature is read, then the client goes away
        await gen.aclose()
        await session.finish()
        await session.finish()  # idempotent

    return session, run


def test_client_disconnect_leaves_its_calls_unknown():
    p = make_proxy()
    session, run = disconnecting_session(p, delta([(0, "c1", None), (1, "c2", None)], usage=USAGE),
                                         [delta([(0, None, SIG_A)]), chunk("more")])
    asyncio.run(run())
    assert session.reason == "client_disconnected"
    assert stored(p, "c1") is not None  # the affinity record stays
    assert stored(p, "c1").signature is None and stored(p, "c2").signature is None


def test_failed_settling_write_leaves_unknown_and_never_fails_the_stream():
    up = Upstream({"model-a": [streamed(sse(delta([(0, "c1", None)]), chunk("", usage=USAGE),
                                            delta([(0, None, SIG_A)])))]})
    p = make_proxy()
    p.affinity._conn = FailingConn(p.conn, "UPDATE affinity SET signature")
    with mocked(up), running(p) as c, captured_logs() as logs:
        r = post(c, chat_body(stream=True))
        rec = stored(p, "c1")
    assert r.status_code == 200 and r.content.endswith(b"data: [DONE]\n\n")
    assert rec == AffinityRecord("model-a", rec.turn_id, None)  # the record stays
    assert p.router.state_errors == 1
    assert "could not settle the thought signatures" in logs.getvalue()
    assert SIG_A not in logs.getvalue()


# ---- restore: §23.4's table ---------------------------------------------------------------

def test_client_signature_is_kept_over_a_stored_one():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1", SIG_B)])), up)
    assert isinstance(out, RouteServed) and up.bodies == [None]  # sent as received


def test_own_stored_signature_is_restored_and_counted():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    seen = []
    env.router.on_signature = lambda *a: seen.append(a)
    req = chat(messages=turn([tc("s1")]))
    before = copy.deepcopy(req)
    up = ScriptedUpstream()
    attempts = []
    env.route(req, up, attempts=attempts)
    assert sent_signature(up.bodies[0], "s1") == SIG_A
    assert req == before  # the client's request is never changed
    assert seen == [("model-a", "restored", 1)]
    assert attempts[0].log()["signatures"] == {"restored": 1, "placeholder": 0}


def test_own_call_stored_as_none_is_sent_as_received():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": ""})
    up = ScriptedUpstream()
    attempts = []
    env.route(chat(messages=turn([tc("s1")])), up, attempts=attempts)
    assert up.bodies == [None]
    assert "signatures" not in attempts[0].log()


def test_own_call_with_unknown_state_gets_the_placeholder():
    env = env1()
    env.affinity.record(["s1"], "model-a")  # NULL: unknown
    up = ScriptedUpstream()
    seen = []
    env.router.on_signature = lambda *a: seen.append(a)
    env.route(chat(messages=turn([tc("s1")])), up)
    assert sent_signature(up.bodies[0], "s1") == PLACEHOLDER_SIGNATURE
    assert seen == [("model-a", "placeholder", 1)]


def test_unknown_and_missing_ids_without_signature_get_the_placeholder_on_a_pinned_turn():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    up = ScriptedUpstream()
    attempts = []
    out = env.route(chat(messages=turn([tc("s1"), tc("lost"), tc(None)])), up, attempts=attempts)
    assert out.affinity == "pinned"
    body = up.bodies[0]
    calls = body["messages"][-4]["tool_calls"]
    assert [((t.get("extra_content") or {}).get("google") or {}).get("thought_signature") for t in calls] == \
        [SIG_A, PLACEHOLDER_SIGNATURE, PLACEHOLDER_SIGNATURE]
    assert attempts[0].log()["signatures"] == {"restored": 1, "placeholder": 2}


def test_unpinned_turn_gets_the_placeholder_on_missing_signatures_only():
    env = env1()
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("lost-1"), tc("lost-2", SIG_C)])), up)
    assert out.affinity == "unknown"
    assert sent_signature(up.bodies[0], "lost-1") == PLACEHOLDER_SIGNATURE
    assert sent_signature(up.bodies[0], "lost-2") == SIG_C  # the client's own, untouched


def test_failed_lookup_routes_unknown_with_placeholders():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    env.affinity._conn = FailingConn(env.conn, "SELECT tool_call_id, model_id")
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1")])), up)
    assert isinstance(out, RouteServed) and out.affinity == "unknown"
    assert sent_signature(up.bodies[0], "s1") == PLACEHOLDER_SIGNATURE
    assert env.router.state_errors == 1


def test_owner_no_longer_configured_gets_no_stored_signature():
    env = env1()
    env.affinity.record(["s1"], "model-gone", None, {"s1": SIG_A})
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1")])), up)
    assert out.affinity == "unknown"
    assert sent_signature(up.bodies[0], "s1") == PLACEHOLDER_SIGNATURE


def test_handoff_target_never_gets_the_owners_stored_signature():
    from quota_router.classify import Classification
    env = env3()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    env.store.on_rate_limited(env.model("a"), Classification("rpd", None), sent_at=env.clock.now())
    seen = []
    env.router.on_signature = lambda *a: seen.append(a)
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1")])), up)
    assert out.model.id == "model-b" and out.affinity == "handoff"
    assert sent_signature(up.bodies[0], "s1") == PLACEHOLDER_SIGNATURE
    assert SIG_A not in json.dumps(up.bodies[0])
    assert seen == []  # §16.8's placeholder: counted in qr_handoff_total, not here


def test_mixed_turn_latest_owner_gets_its_own_signatures_and_placeholders_for_the_rest():
    env = env3()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    env.affinity.record(["s2"], "model-b", None, {"s2": SIG_B})
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1")], [tc("s2")])), up)
    assert out.model.id == "model-b" and out.affinity == "mixed"
    assert sent_signature(up.bodies[0], "s1") == PLACEHOLDER_SIGNATURE
    assert sent_signature(up.bodies[0], "s2") == SIG_B
    assert SIG_A not in json.dumps(up.bodies[0])


def test_handoff_target_gets_its_own_stored_signatures_back():
    from quota_router.classify import Classification
    env = env3(targets=("a", "c"))
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    env.affinity.record(["s2"], "model-b", None, {"s2": SIG_B})
    env.store.on_rate_limited(env.model("b"), Classification("rpd", None), sent_at=env.clock.now())
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([tc("s1")], [tc("s2")])), up)  # owner b, out: hand off
    assert out.affinity == "handoff" and out.model.id == "model-a"
    assert sent_signature(up.bodies[0], "s1") == SIG_A  # model-a's own call
    assert sent_signature(up.bodies[0], "s2") == PLACEHOLDER_SIGNATURE


def test_earlier_turns_are_sent_as_received():
    env = env1()
    env.affinity.record(["old", "s1"], "model-a", None, {"old": SIG_B, "s1": SIG_A})
    earlier = [{"role": "user", "content": "before"},
               {"role": "assistant", "content": None, "tool_calls": [tc("old")]},
               {"role": "tool", "tool_call_id": "old", "content": "x"},
               {"role": "assistant", "content": "done"}]
    req = chat(messages=turn([tc("s1")], prefix=earlier))
    up = ScriptedUpstream()
    env.route(req, up)
    assert sent_signature(up.bodies[0], "old") is None
    assert sent_signature(up.bodies[0], "s1") == SIG_A
    assert up.bodies[0]["messages"][1] is req["messages"][1]  # shared, not copied


def test_estimate_and_reservation_come_from_the_body_sent():
    big = "S" * MAX_SIGNATURE_CHARS
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": big})
    req = chat(messages=turn([tc("s1")]))
    up = ScriptedUpstream()
    out = env.route(req, up)
    assert out.reservation.tokens.tokens == estimate_input_tokens(up.bodies[0])
    assert out.reservation.tokens.tokens > estimate_input_tokens(req) + 4000


def test_restored_body_too_large_for_the_model_is_skipped_as_too_large():
    big = "S" * MAX_SIGNATURE_CHARS
    env = Env(make_config({"a": model_dict("model-a", tpm_input=3000)}, {"fast": ["a"]}))
    env.affinity.record(["s1"], "model-a", None, {"s1": big})
    out = env.route(chat(messages=turn([tc("s1")])), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and out.code == "context_length_exceeded"


def test_with_signatures_returns_the_request_itself_when_nothing_changes():
    req = {"model": "fast", "messages": turn([tc("s1", SIG_A), tc("s2")])}
    recs = {"s1": AffinityRecord("model-a", None, SIG_B), "s2": AffinityRecord("model-a", None, "")}
    out = with_signatures(req, "model-a", recs)
    assert out.body is req and (out.restored, out.placeholder, out.foreign) == (0, 0, 0)
    assert with_signatures({"messages": "nope"}, "m", {}).body == {"messages": "nope"}


def test_with_signatures_keeps_other_fields_of_the_call():
    req = {"model": "fast", "messages": turn([{**tc("s1"), "extra_content": {"other": 1, "google": {"x": 2}}}])}
    before = copy.deepcopy(req)
    out = with_signatures(req, "model-a", {"s1": AffinityRecord("model-a", None, SIG_A)}).body
    assert req == before
    got = out["messages"][-2]["tool_calls"][0]["extra_content"]
    assert got == {"other": 1, "google": {"x": 2, "thought_signature": SIG_A}}


def test_400_after_restoring_is_returned_unchanged_and_logged_with_counts_only():
    body400 = error_body(400, "INVALID_ARGUMENT", "Function call is missing a thought_signature")
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), httpx.Response(400, json=body400)],
                   "model-b": [ok()]})
    p = make_proxy(handoff_targets=["b"])
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body())
        r = post(c, copilot_follow_up(["call-1"]))
    assert r.status_code == 400 and r.json() == body400
    assert up.models == ["model-a", "model-a"]  # never retried elsewhere
    text = logs.getvalue()
    assert "restored 1, placeholder 0" in text
    assert "handoff_targets" not in text  # no other model's placeholders in that body
    assert SIG_A not in text


# ---- storage ------------------------------------------------------------------------------

def version_2_file(path) -> None:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for statement in MIGRATIONS[1]:
        c.execute(statement)
    c.execute("PRAGMA user_version = 2")
    c.execute("INSERT INTO affinity VALUES ('v2-call', 'model-a', ?, 'ab')", (T0 - 60,))
    c.commit()
    c.close()


def test_migration_2_to_3_keeps_rows_as_unknown(tmp_path):
    path = tmp_path / "v2.sqlite3"
    version_2_file(path)
    conn = connect(path)
    assert SCHEMA_VERSION == 4 and conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store = AffinityStore(conn, FakeClock(T0))
    assert store.lookup_records(["v2-call"]) == {"v2-call": AffinityRecord("model-a", "ab", None)}
    conn.close()


def test_version_2_record_gets_the_placeholder_on_a_copilot_follow_up(tmp_path):
    path = tmp_path / "v2.sqlite3"
    version_2_file(path)
    up = Upstream()
    p = make_proxy(db=str(path))
    with mocked(up), running(p) as c:
        r = post(c, copilot_follow_up(["v2-call"]))
    assert r.headers["x-turn-affinity"] == "pinned"
    assert sent_signature(up.body(0), "v2-call") == PLACEHOLDER_SIGNATURE


def test_migration_1_to_3_and_a_new_file(tmp_path):
    v1 = tmp_path / "v1.sqlite3"
    c = sqlite3.connect(v1)
    c.executescript(SCHEMA)
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO affinity VALUES ('v1-call', 'model-a', ?)", (T0 - 60,))
    c.commit()
    c.close()
    for path in (v1, tmp_path / "new.sqlite3"):
        conn = connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = [r[1] for r in conn.execute("PRAGMA table_info(affinity)")]
        assert cols == ["tool_call_id", "model_id", "created_at", "turn_id", "signature"]
        conn.close()
    conn = connect(v1)
    assert AffinityStore(conn, FakeClock(T0)).lookup_records(["v1-call"]) == {
        "v1-call": AffinityRecord("model-a", None, None)}
    conn.close()


def test_an_id_recorded_twice_overwrites_the_signature():
    env = env1()
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    assert env.affinity.record(["s1"], "model-b", None, {"s1": ""}) == Recorded(1, 0, T0)
    assert stored(env, "s1") == AffinityRecord("model-b", None, "")
    env.affinity.record(["s1"], "model-a", None, {"s1": SIG_A})
    env.affinity.record(["s1"], "model-a")  # no signatures given: unknown
    assert stored(env, "s1").signature is None


def test_settling_write_never_touches_another_models_or_a_newer_record():
    env = env1()
    at = env.affinity.record(["s1", "s2"], "model-a").at
    env.affinity.record(["s1"], "model-b")  # a later response reused the ID
    env.affinity.record(["s2"], "model-a", None, {"s2": ""})  # a later response of the same model
    written, _ = env.affinity.settle_signatures("model-a", None, [("s1", SIG_A, None, at), ("s2", SIG_A, None, at)])
    assert written == []
    assert stored(env, "s1") == AffinityRecord("model-b", None, None)
    assert stored(env, "s2").signature == ""
    # Clearing a contradicted signature is guarded on the value recorded.
    at3 = env.affinity.record(["s3"], "model-a", None, {"s3": SIG_B}).at
    assert env.affinity.settle_signatures("model-a", None, [("s3", None, SIG_A, at3)])[0] == []
    assert stored(env, "s3").signature == SIG_B
    assert env.affinity.settle_signatures("model-a", None, [("s3", None, SIG_B, at3)])[0] == [None]
    assert stored(env, "s3").signature is None


def test_settling_write_skips_a_newer_unknown_record_of_the_same_model():
    # Review finding: two streams from one model reused an ID and both recorded it as unknown;
    # the older stream's settle landed on the newer record and blocked the newer one's settle.
    env = env1()
    old_at = env.affinity.record(["s1"], "model-a", "turn-1").at
    env.clock.advance(1)
    new_at = env.affinity.record(["s1"], "model-a", "turn-2").at
    assert env.affinity.settle_signatures("model-a", "turn-1", [("s1", SIG_A, None, old_at)])[0] == []
    assert stored(env, "s1").signature is None
    # Same created_at but another turn is still not this stream's record.
    assert env.affinity.settle_signatures("model-a", "turn-1", [("s1", SIG_A, None, new_at)])[0] == []
    assert env.affinity.settle_signatures("model-a", "turn-2", [("s1", SIG_B, None, new_at)])[0] == [SIG_B]
    assert stored(env, "s1").signature == SIG_B


def test_total_cap_clears_the_oldest_signatures_and_keeps_the_rows():
    conn = connect(":memory:")
    clock = FakeClock(T0)
    store = AffinityStore(conn, clock, max_signature_total=100)
    store.record(["s1"], "model-a", None, {"s1": "A" * 40})
    clock.advance(1)
    store.record(["s2"], "model-a", None, {"s2": "B" * 40})
    clock.advance(1)
    assert store.record(["s3", "s4"], "model-a", None, {"s3": "C" * 40, "s4": ""}).evicted == 1
    recs = store.lookup_records(["s1", "s2", "s3", "s4"])
    assert [recs[i].signature for i in ("s1", "s2", "s3", "s4")] == [None, "B" * 40, "C" * 40, ""]
    # A settling write that adds a signature keeps the cap too.
    at5 = store.record(["s5"], "model-a").at
    written, evicted = store.settle_signatures("model-a", None, [("s5", "D" * 40, None, at5)])
    assert written == ["D" * 40] and evicted == 1
    assert store.lookup_records(["s2"])["s2"].signature is None
    # The prune deletes the signature with its row.
    clock.advance(25 * 3600)
    store.prune()
    assert conn.execute("SELECT COUNT(*) FROM affinity").fetchone()[0] == 0


def test_eviction_is_counted_through_http():
    up = Upstream({"model-a": [signed_ok([("c1", SIG_A)]), signed_ok([("c2", SIG_B)])]})
    p = make_proxy()
    p.affinity._max_total = len(SIG_A) + 10
    with mocked(up), running(p) as c:
        post(c, chat_body())
        post(c, chat_body())
        assert sig_metric(c, "model-a", "evicted") == 1
        assert c.get("/status", headers=AUTH).json()["signatures"]["evicted"] == 1
        assert stored(p, "c1").signature is None and stored(p, "c2").signature == SIG_B


def test_record_refuses_a_value_outside_the_rules_whatever_the_caller_checked():
    env = env1()
    env.affinity.record(["s1", "s2"], "model-a", None,
                        {"s1": "S" * (MAX_SIGNATURE_CHARS + 1), "s2": "has space"})
    assert stored(env, "s1").signature is None and stored(env, "s2").signature is None


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_database_file_is_owner_only_whatever_the_umask(tmp_path):
    old = os.umask(0o022)
    try:
        folder = tmp_path / "made-by-connect"
        path = folder / "q.sqlite3"
        conn = connect(path)
        conn.execute("INSERT INTO rpd VALUES ('m', 'd', 1)")
        assert mode(folder) == 0o700 and mode(path) == 0o600
        assert mode(str(path) + "-wal") == 0o600
        conn.close()
        os.chmod(path, 0o644)  # an older file, made before this version
        os.chmod(str(path) + "-wal", 0o644) if os.path.exists(str(path) + "-wal") else None
        connect(path).close()
        assert mode(path) == 0o600
    finally:
        os.umask(old)


def test_default_data_folder_is_tightened_but_a_custom_existing_one_is_not(tmp_path, monkeypatch):
    # DESIGN §23.8 amendment 3: a custom db_path may sit in the working or a shared directory.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(mode=0o755)
    os.chmod(tmp_path / "data", 0o755)
    connect("data/quota.sqlite3").close()
    assert mode(tmp_path / "data") == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    with captured_logs() as logs:
        connect(shared / "q.sqlite3").close()
    assert mode(shared) == 0o755 and mode(shared / "q.sqlite3") == 0o600
    assert "readable by others" in logs.getvalue()


def test_database_file_that_cant_be_made_owner_only_refuses_to_start(tmp_path, monkeypatch):
    from quota_router import db as dbmod
    from quota_router.db import StateDBError
    path = tmp_path / "q.sqlite3"
    real = os.chmod

    def no_chmod(p, m, *a, **kw):
        if str(p) == str(path):
            raise PermissionError(1, "Operation not permitted")
        return real(p, m, *a, **kw)

    monkeypatch.setattr(dbmod.os, "chmod", no_chmod)
    with pytest.raises(StateDBError, match="owner-only"):
        connect(path)
    real(path, 0o644)
    monkeypatch.setattr(dbmod.os, "chmod", lambda p, m, *a, **kw: None)  # a filesystem that ignores it
    with pytest.raises(StateDBError, match="readable by others"):
        connect(path)


def test_wal_file_that_cant_be_made_owner_only_closes_the_connection(tmp_path, monkeypatch):
    # The -wal and -shm files appear only once _open() switches to WAL, so their check runs
    # after the connection exists; refusing to start there must not leave it open.
    import sqlite3
    from quota_router import db as dbmod
    from quota_router.db import StateDBError
    path = tmp_path / "q.sqlite3"
    opened = []
    real_connect, real_chmod = sqlite3.connect, os.chmod

    def recording_connect(*a, **kw):
        opened.append(real_connect(*a, **kw))
        return opened[-1]

    def no_chmod_on_wal_or_shm(p, m, *a, **kw):
        if str(p).endswith(("-wal", "-shm")):
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(p, m, *a, **kw)

    monkeypatch.setattr(dbmod.sqlite3, "connect", recording_connect)
    monkeypatch.setattr(dbmod.os, "chmod", no_chmod_on_wal_or_shm)
    with pytest.raises(StateDBError, match=r"-(wal|shm) owner-only"):
        connect(path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


# ---- failures never fail the request ----------------------------------------------------------

def test_failed_record_leads_to_unknown_and_the_placeholder():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), ok()]})
    p = make_proxy()
    p.affinity._conn = FailingConn(p.conn, "INSERT INTO affinity", times=1)
    with mocked(up), running(p) as c, captured_logs() as logs:
        assert post(c, chat_body()).status_code == 200
        r = post(c, copilot_follow_up(["call-1"]))
    assert r.status_code == 200 and r.headers["x-turn-affinity"] == "unknown"
    assert sent_signature(up.body(1), "call-1") == PLACEHOLDER_SIGNATURE
    assert p.router.state_errors == 1
    assert "could not record tool-call affinity" in logs.getvalue()
    assert SIG_A not in logs.getvalue()


# ---- visibility and privacy ---------------------------------------------------------------------

def test_log_line_counts_and_a_zoo_code_request_shows_no_signatures_field():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), ok(), signed_ok([("call-2", SIG_B)]), ok()]})
    p = make_proxy()
    zoo = copilot_follow_up(["call-2"])
    zoo["messages"][-2]["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": SIG_B}}
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body())
        post(c, copilot_follow_up(["call-1"]))
        post(c, chat_body())
        post(c, zoo)
        assert sig_metric(c, "model-a", "restored") == 1
        assert sig_metric(c, "model-a", "stored") == 2
        status = c.get("/status", headers=AUTH).json()["signatures"]
    lines = request_lines(logs)
    assert lines[1]["attempts"][0]["signatures"] == {"restored": 1, "placeholder": 0}
    assert all("signatures" not in a for ln in (lines[0], lines[2], lines[3]) for a in ln["attempts"])
    assert sent_signature(up.body(3), "call-2") == SIG_B and up.requests[3] is not None
    assert status == {"stored": 2, "none": 0, "too_large": 0, "invalid": 0, "conflict": 0,
                      "evicted": 0, "restored": 1, "placeholder": 0}


def test_signature_values_never_leave_the_proxy_except_upstream():
    from quota_router.classify import Classification
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]),
                               streamed(sse(delta([(0, "call-2", SIG_B)]), chunk("", usage=USAGE))),
                               ok(), httpx.Response(400, json=error_body(400, "INVALID_ARGUMENT", "bad")),
                               signed_ok([("call-3", SIG_C)])]})
    p = make_proxy()
    with mocked(up), running(p) as c, captured_logs() as logs:
        post(c, chat_body())
        post(c, chat_body(stream=True))
        post(c, copilot_follow_up(["call-1"]))  # restored
        post(c, copilot_follow_up(["call-2"]))  # restored, then a 400
        # An exception path: a failed record with a value in flight (a failed settle: above).
        p.affinity._conn = FailingConn(p.conn, "INSERT INTO affinity", times=1)
        post(c, chat_body())
        p.affinity._conn = p.conn
        # A router error body on a pinned turn whose stored signature would be restored.
        p.store.on_rate_limited(p.config.models["a"], Classification("rpd", None), sent_at=p.clock.now())
        failed = post(c, copilot_follow_up(["call-1"]))
        status = c.get("/status", headers=AUTH).text
        metrics = c.get("/metrics", headers=AUTH).text
        sid = c.post("/dashboard/login", json={"token": TOKEN}, headers={"Origin": LOCAL}).json()["session"]
        dash = c.get("/dashboard/status.json", headers={SESSION_HEADER: sid}).text
    assert failed.status_code == 429
    assert sent_signature(up.body(2), "call-1") == SIG_A and sent_signature(up.body(3), "call-2") == SIG_B
    for where, text in (("logs", logs.getvalue()), ("status", status), ("metrics", metrics),
                        ("dashboard", dash), ("error body", failed.text)):
        for sig in (SIG_A, SIG_B, SIG_C):
            assert sig not in text, where
            assert sig[:40] not in text, where


# ---- review follow-ups (2026-11-22) -----------------------------------------------------------

def test_owner_no_longer_configured_counts_a_placeholder_and_gives_no_handoff_advice():
    # Review finding: the placeholder was counted as another model's call (§16.8), so it showed
    # nowhere, and a 400 advised removing the serving model from handoff_targets.
    body400 = error_body(400, "INVALID_ARGUMENT", "Function call is missing a thought_signature")
    up = Upstream({"model-a": [httpx.Response(400, json=body400)]})
    p = make_proxy()
    p.affinity.record(["gone-1"], "model-removed", None, {"gone-1": SIG_A})
    with mocked(up), running(p) as c, captured_logs() as logs:
        r = post(c, copilot_follow_up(["gone-1"]))
        assert sig_metric(c, "model-a", "placeholder") == 1
    assert r.status_code == 400 and r.headers["x-turn-affinity"] == "unknown"
    assert sent_signature(up.body(0), "gone-1") == PLACEHOLDER_SIGNATURE
    text = logs.getvalue()
    assert "handoff_targets" not in text and "restored 0, placeholder 1" in text
    assert request_lines(logs)[0]["attempts"][0]["signatures"] == {"restored": 0, "placeholder": 1}


def test_id_less_calls_get_the_placeholder_when_no_call_has_an_id():
    # Review finding: with no ID in the turn the body was sent unchanged.
    env = env1()
    up = ScriptedUpstream()
    req = chat(messages=turn([tc(None)]))
    env.route(req, up)
    sent = up.bodies[0]["messages"][-2]["tool_calls"][0]
    assert sent["extra_content"]["google"]["thought_signature"] == PLACEHOLDER_SIGNATURE


def test_streamed_conflict_after_the_record_is_not_also_counted_as_stored():
    # Review finding: recorded with SIG_A at usage, contradicted later: stored 1 and conflict 1.
    snap, _, m = run_stream(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE), delta([(0, None, SIG_B)])))
    assert snap.affinity.lookup_records(["c1"])["c1"].signature is None
    assert m["conflict"] == 1 and m["stored"] == 0
    snap2, _, m2 = run_stream(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE)))
    assert snap2.affinity.lookup_records(["c1"])["c1"].signature == SIG_A and m2["stored"] == 1


def test_streamed_same_id_at_two_indexes_is_one_call():
    # Review finding: the second index's signature was ignored and the call settled as ''.
    snap, _, m = run_stream(sse(delta([(0, "c1", None)]), delta([(1, "c1", SIG_A)]), chunk("", usage=USAGE)))
    assert snap.affinity.lookup_records(["c1"])["c1"].signature == SIG_A
    snap2, _, m2 = run_stream(sse(delta([(0, "c1", SIG_A)]), delta([(1, "c1", SIG_B)]), chunk("", usage=USAGE)))
    assert snap2.affinity.lookup_records(["c1"])["c1"].signature is None and m2["conflict"] == 1


def test_streamed_signature_before_its_ids_delta():
    snap, _, _ = run_stream(sse(delta([(0, None, SIG_A)]), delta([(0, "c1", None)]), chunk("", usage=USAGE)))
    assert snap.affinity.lookup_records(["c1"])["c1"].signature == SIG_A


def test_streamed_second_id_at_the_same_index_is_a_new_call():
    snap, _, _ = run_stream(sse(delta([(0, "c1", SIG_A)]), delta([(0, "c2", None)]),
                                delta([(0, None, SIG_B)]), chunk("", usage=USAGE)))
    recs = snap.affinity.lookup_records(["c1", "c2"])
    assert recs["c1"].signature == SIG_A and recs["c2"].signature == SIG_B


def test_streamed_id_and_late_signature_after_usage_are_recorded_and_settled():
    snap, _, m = run_stream(sse(chunk("", usage=USAGE), delta([(0, "c1", None)]), delta([(0, None, SIG_A)])))
    assert snap.affinity.lookup_records(["c1"])["c1"].signature == SIG_A and m["stored"] == 1


def test_client_request_is_unchanged_through_http():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), ok()]})
    p = make_proxy()
    follow = copilot_follow_up(["call-1"])
    before = copy.deepcopy(follow)
    with mocked(up), running(p) as c:
        post(c, chat_body())
        post(c, follow)
    assert follow == before and sent_signature(up.body(1), "call-1") == SIG_A


# ---- amendment 2: request-scoped scrubbing (DESIGN §23.8) ----------------------------------------

def echo(sig: str, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json=error_body(status, "INVALID_ARGUMENT", f"bad signature {sig}"))


def test_restored_signature_echoed_in_a_4xx_is_scrubbed_but_the_clients_own_is_not():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), echo(SIG_A), echo(SIG_C)]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, copilot_follow_up(["call-1"]))
        own = copilot_follow_up(["call-9"])
        own["messages"][-2]["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": SIG_C}}
        r2 = post(c, own)
    assert r.status_code == 400 and SIG_A not in r.text and "<redacted>" in r.text
    assert SIG_C in r2.text  # nothing restored: passed through unchanged, as before


def test_restored_signature_echoed_in_a_429_capture_is_scrubbed(tmp_path):
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]), echo(SIG_A, 429)]})
    p = make_proxy(db=str(tmp_path / "q.sqlite3"))
    with mocked(up), running(p) as c:
        post(c, chat_body())
        post(c, copilot_follow_up(["call-1"]))
    files = list((tmp_path / "429").glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert SIG_A not in text and "<redacted>" in text


def test_restored_signature_echoed_in_a_stream_error_event_is_scrubbed(tmp_path):
    first_err = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": f"bad {SIG_A}"}}
    later_err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": f"slow {SIG_A}"}}
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A)]),
                               streamed(sse(first_err, done=False)),
                               streamed(sse(chunk("partial"), later_err, done=False))]})
    p = make_proxy(db=str(tmp_path / "q.sqlite3"))
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r1 = post(c, {**copilot_follow_up(["call-1"]), "stream": True})
        r2 = post(c, {**copilot_follow_up(["call-1"]), "stream": True})
    assert r1.status_code == 400 and SIG_A not in r1.text
    assert b"partial" in r2.content and SIG_A.encode() not in r2.content
    for f in (tmp_path / "429").glob("*.json"):
        assert SIG_A not in f.read_text()


def test_short_restored_value_is_scrubbed_too():
    # Review finding: values under 8 characters were dropped from the per-call scrubber.
    from quota_router.scrub import Scrubber
    assert Scrubber(["k" * 20]).with_extra(["abcd"]).text("x abcd y") == "x <redacted> y"


def test_only_one_of_several_restored_signatures_echoed_is_scrubbed():
    up = Upstream({"model-a": [signed_ok([("call-1", SIG_A), ("call-2", SIG_B)]), echo(SIG_B)]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, copilot_follow_up(["call-1", "call-2"]))
    assert sent_signature(up.body(1), "call-1") == SIG_A and sent_signature(up.body(1), "call-2") == SIG_B
    assert SIG_B not in r.text and "<redacted>" in r.text


def test_failed_settle_at_done_loses_the_signature_and_the_follow_up_gets_the_placeholder():
    # Amendment 1's trade-off: the usage-chunk record is NULL, so a failed settle keeps nothing.
    up = Upstream({"model-a": [streamed(sse(delta([(0, "c1", SIG_A)]), chunk("", usage=USAGE))), ok()]})
    p = make_proxy()
    p.affinity._conn = FailingConn(p.conn, "UPDATE affinity SET signature", times=1)
    with mocked(up), running(p) as c:
        assert post(c, chat_body(stream=True)).status_code == 200
        assert stored(p, "c1").signature is None
        r = post(c, copilot_follow_up(["c1"]))
    assert r.headers["x-turn-affinity"] == "pinned"
    assert sent_signature(up.body(1), "c1") == PLACEHOLDER_SIGNATURE
