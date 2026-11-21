"""Tool-turn handoffs with placeholder signatures (DESIGN §16.8 1b)."""

from __future__ import annotations

import copy

import httpx
import pytest

from apphelpers import AUTH, Upstream, chat_body, error_body, make_proxy, mocked, ok, running, unavailable
from helpers import Env, ScriptedUpstream, chat, make_config, model_dict
from quota_router.classify import Classification
from quota_router.config import ConfigError
from quota_router.router import (PLACEHOLDER_SIGNATURE, Rejected, RouteFailed, RoutePassthrough,
                                 RouteServed, Unavailable, with_placeholder_signatures)

def call(i: str, sig: str | None = "REAL-SIG", **extra) -> dict:
    tc = {"id": i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    if sig is not None:
        tc["extra_content"] = {"google": {"thought_signature": sig}, **extra}
    return tc


def turn(*steps: list[dict], prefix: list[dict] | None = None) -> list[dict]:
    msgs = list(prefix or []) + [{"role": "user", "content": "do it"}]
    for calls in steps:
        msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
        msgs += [{"role": "tool", "tool_call_id": c["id"], "content": "ok"} for c in calls]
        msgs.append({"role": "user", "content": "<environment_details>"})  # Zoo Code's shape
    return msgs


def sigs(body: dict) -> dict[str, str | None]:
    out = {}
    for m in body["messages"]:
        for tc in m.get("tool_calls") or []:
            out[tc["id"]] = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
    return out


def env3(targets=("b", "c"), **top) -> Env:
    models = {"a": model_dict("model-a"), "b": model_dict("model-b"), "c": model_dict("model-c")}
    return Env(make_config(models, {"fast": ["a", "b", "c"]}, handoff_targets=list(targets), **top))


def exhaust(env: Env, key: str) -> None:
    env.store.on_rate_limited(env.model(key), Classification("rpd", None), sent_at=env.clock.now())


# ---- when to hand off -------------------------------------------------------

def test_exhausted_owner_hands_off_with_placeholder_on_the_owners_calls():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    req = chat(messages=turn([call("s1")]))
    before = copy.deepcopy(req)
    up = ScriptedUpstream()
    seen = []
    env.router.on_handoff = lambda *a: seen.append(a)
    out = env.route(req, up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert out.affinity == "handoff" and out.handoff_from == "model-a"
    assert up.called == ["model-b"]  # the owner was skipped without a call
    assert sigs(up.bodies[0]) == {"s1": PLACEHOLDER_SIGNATURE}
    assert req == before  # the client's request is never changed
    assert seen == [("model-a", "model-b", "served")]


def test_owner_5xx_on_this_request_hands_off():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    up = ScriptedUpstream({"model-a": Unavailable("503")})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert out.model.id == "model-b" and out.path == ["model-a", "model-b"]
    assert up.bodies[0] is None  # the owner got the client's body unchanged
    assert sigs(up.bodies[1]) == {"s1": PLACEHOLDER_SIGNATURE}


def test_owner_5xx_that_also_fills_its_rpm_window_still_hands_off():
    # Review finding: a 503 that used a 5-RPM owner's last slot was merged into "quota, ready
    # in 40 s" (DESIGN §5.2), so the gate returned the owner's 429 instead of handing off.
    models = {"a": model_dict("model-a", rpm=5), "b": model_dict("model-b")}
    env = Env(make_config(models, {"fast": ["a", "b"]}, handoff_targets=["b"]))
    env.affinity.record(["s1"], "model-a")
    for _ in range(4):
        env.store.try_reserve(env.model("a"), 10)
    env.clock.advance(20)
    up = ScriptedUpstream({"model-a": Unavailable("http 503")})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert up.called == ["model-a", "model-b"]


def test_owner_skipped_in_a_5xx_cooldown_with_a_full_rpm_window_hands_off():
    models = {"a": model_dict("model-a", rpm=1), "b": model_dict("model-b")}
    env = Env(make_config(models, {"fast": ["a", "b"]}, handoff_targets=["b"]))
    env.affinity.record(["s1"], "model-a")
    env.store.try_reserve(env.model("a"), 10)
    env.store.on_unavailable(env.model("a"))
    env.clock.advance(5)
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert out.model.id == "model-b" and up.called == ["model-b"]


def test_fresh_60_s_cooldown_after_a_429_reaches_the_default_threshold():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    env.store.on_rate_limited(env.model("a"), Classification("unknown", None), sent_at=env.clock.now())
    out = env.route(chat(messages=turn([call("s1")])), ScriptedUpstream())
    assert out.model.id == "model-b"


class TickingClock:
    """Moves forward a little on every read, as a real clock does between steps."""

    def __init__(self, start: float, step: float = 0.005) -> None:
        self.t, self.step = start, step

    def now(self) -> float:
        self.t += self.step
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_fresh_60_s_cooldown_hands_off_on_a_moving_clock():
    # Review finding: the cooldown was set at T1 and the gate read the clock at T2 > T1, so the
    # wait looked like 59.99 s and the client got a 429. A frozen clock hid it.
    from helpers import T0
    from quota_router.router import RateLimited
    models = {"a": model_dict("model-a"), "b": model_dict("model-b")}
    env = Env(make_config(models, {"fast": ["a", "b"]}, handoff_targets=["b"]), clock=TickingClock(T0))
    env.affinity.record(["s1"], "model-a")
    unknown_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "slow down"}}
    up = ScriptedUpstream({"model-a": RateLimited(unknown_429)})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert up.called == ["model-a", "model-b"]


def test_slow_429_with_a_short_retry_delay_is_not_a_handoff():
    # Review finding: the wait was measured from before the call, so a 429 that took 45 s to
    # arrive with a 20 s retry delay looked like a 65 s wait and spent a target's quota.
    import json
    from apphelpers import rate_limited
    from quota_router.router import RateLimited
    env = env3()
    env.affinity.record(["s1"], "model-a")
    body = json.loads(rate_limited("minute", delay="20s").content)
    up = ScriptedUpstream({"model-a": RateLimited(body)}, on_call=lambda m, t: env.clock.advance(45))
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 429
    assert up.called == ["model-a"]
    assert out.retry_after_s == 20


def test_target_size_is_estimated_from_the_body_it_receives():
    # Review finding: the estimate came from the client's body with its long real signatures,
    # so a target that fits the placeholder version was skipped as too large.
    big_sig = "S" * 40_000  # ~12K estimated tokens; the placeholder is 32 characters
    models = {"a": model_dict("model-a"), "b": model_dict("model-b", tpm_input=5_000)}
    env = Env(make_config(models, {"fast": ["a", "b"]}, handoff_targets=["b"]))
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1", big_sig)])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert out.reservation.tokens.tokens < 5_000


def test_short_429_with_an_unreadable_state_is_not_a_handoff(monkeypatch):
    import sqlite3
    from quota_router.router import RateLimited
    env = env3()
    env.affinity.record(["s1"], "model-a")
    rpm_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "20s"}]}}
    real_reserve = env.store.try_reserve

    def reserve_then_break(model, tokens):
        res = real_reserve(model, tokens)
        monkeypatch.setattr(env.store, "check", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
        return res

    monkeypatch.setattr(env.store, "try_reserve", reserve_then_break)
    up = ScriptedUpstream({"model-a": RateLimited(rpm_429)})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 429 and up.called == ["model-a"]


def test_failed_handoff_still_names_the_owner():
    env = env3(targets=("b",))
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    out = env.route(chat(messages=turn([call("s1")])), ScriptedUpstream({"model-b": Unavailable("503")}))
    assert isinstance(out, RouteFailed) and out.handoff_from == "model-a"
    no_try = env3(targets=())
    no_try.affinity.record(["s1"], "model-a")
    exhaust(no_try, "a")
    assert no_try.route(chat(messages=turn([call("s1")])), ScriptedUpstream()).handoff_from is None


def test_400_on_a_rewritten_mixed_turn_is_marked_rewritten():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    env.affinity.record(["s2"], "model-b")
    out = env.route(chat(messages=turn([call("s1")], [call("s2")])),
                    ScriptedUpstream({"model-b": Rejected({"error": {"code": 400}})}))
    assert isinstance(out, RoutePassthrough) and out.rewritten and out.handoff_from is None
    own = env3()
    own.affinity.record(["s1"], "model-a")
    out2 = own.route(chat(messages=turn([call("s1")])),
                     ScriptedUpstream({"model-a": Rejected({"error": {"code": 400}})}))
    assert not out2.rewritten


def test_short_rpm_wait_returns_the_owners_429_without_handoff():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    env.store.on_rate_limited(env.model("a"), Classification("rpm", 20), sent_at=env.clock.now())
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 429
    assert 1 <= out.retry_after_s <= 60
    assert up.called == [] and [c.model.id for c in out.candidates] == ["model-a"]
    assert out.affinity == "pinned"


def test_wait_longer_than_handoff_after_s_hands_off():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    env.store.on_rate_limited(env.model("a"), Classification("rpm", 90), sent_at=env.clock.now())
    out = env.route(chat(messages=turn([call("s1")])), ScriptedUpstream())
    assert out.model.id == "model-b" and out.affinity == "handoff"


def test_handoff_after_s_zero_hands_off_on_any_wait():
    env = env3(handoff_after_s=0)
    env.affinity.record(["s1"], "model-a")
    env.store.on_rate_limited(env.model("a"), Classification("rpm", 5), sent_at=env.clock.now())
    out = env.route(chat(messages=turn([call("s1")])), ScriptedUpstream())
    assert out.model.id == "model-b"


def test_decision_is_made_once_even_if_a_slow_target_eats_the_margin():
    # The owner is ready in 70 s (> 60): hand off. The first target takes 15 s and fails;
    # the second target is still tried although the owner is now only 55 s away.
    env = env3()
    env.affinity.record(["s1"], "model-a")
    env.store.on_rate_limited(env.model("a"), Classification("rpm", 70), sent_at=env.clock.now())
    up = ScriptedUpstream({"model-b": Unavailable("503")},
                          on_call=lambda m, t: env.clock.advance(15) if m.id == "model-b" else None)
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-c"
    assert up.called == ["model-b", "model-c"]


def test_only_listed_targets_take_over():
    env = env3(targets=("c",))
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert out.model.id == "model-c" and up.called == ["model-c"]


def test_no_targets_keeps_the_v1_behaviour():
    env = env3(targets=())
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 429 and up.called == []


def test_direct_model_id_gets_no_handoff():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    up = ScriptedUpstream()
    out = env.route(chat(model="model-a", messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 429 and up.called == []


def test_unpinned_requests_are_unaffected():
    env = env3()
    exhaust(env, "a")
    up = ScriptedUpstream()
    out = env.route(chat(), up)
    assert out.model.id == "model-b" and out.affinity is None and out.handoff_from is None
    assert up.bodies == [None]


def test_all_targets_failing_gives_the_usual_final_status():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    seen = []
    env.router.on_handoff = lambda *a: seen.append(a)
    up = ScriptedUpstream({"model-b": Unavailable("503"), "model-c": Unavailable("503")})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RouteFailed) and out.status == 503
    assert [c.model.id for c in out.candidates] == ["model-a", "model-b", "model-c"]
    assert seen == [("model-a", "model-b", "failed"), ("model-a", "model-c", "failed")]


def test_400_after_handoff_is_returned_unchanged_and_not_retried():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    seen = []
    env.router.on_handoff = lambda *a: seen.append(a)
    up = ScriptedUpstream({"model-b": Rejected({"error": {"code": 400}})})
    out = env.route(chat(messages=turn([call("s1")])), up)
    assert isinstance(out, RoutePassthrough) and out.handoff_from == "model-a"
    assert up.called == ["model-b"] and seen == [("model-a", "model-b", "rejected")]


def test_a_failing_hook_never_breaks_routing():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    env.router.on_handoff = lambda *a: 1 / 0
    assert env.route(chat(messages=turn([call("s1")])), ScriptedUpstream()).model.id == "model-b"


# ---- after the handoff --------------------------------------------------------

def test_target_owns_the_turn_afterwards_and_the_old_owners_calls_stay_placeholdered():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    out = env.route(chat(messages=turn([call("s1")])), ScriptedUpstream())
    env.router.complete(out, 10, ["s2"])  # model-b made call s2
    up = ScriptedUpstream()
    out2 = env.route(chat(messages=turn([call("s1")], [call("s2", "B-SIG")])), up)
    assert out2.model.id == "model-b" and out2.affinity == "mixed" and out2.handoff_from is None
    assert sigs(up.bodies[0]) == {"s1": PLACEHOLDER_SIGNATURE, "s2": "B-SIG"}


def test_owner_serving_its_own_turn_sends_unknown_ids_unchanged():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    up = ScriptedUpstream()
    out = env.route(chat(messages=turn([call("s1")], [call("lost")])), up)
    assert out.model.id == "model-a" and out.affinity == "pinned" and up.bodies == [None]


def test_unknown_ids_get_the_placeholder_when_another_model_serves():
    env = env3()
    env.affinity.record(["s1"], "model-a")
    exhaust(env, "a")
    up = ScriptedUpstream()
    env.route(chat(messages=turn([call("s1")], [call("lost")])), up)
    assert sigs(up.bodies[0]) == {"s1": PLACEHOLDER_SIGNATURE, "lost": PLACEHOLDER_SIGNATURE}


# ---- the rewrite itself ---------------------------------------------------------

def test_rewrite_touches_only_the_active_turn_and_keeps_other_fields():
    old_turn = [{"role": "user", "content": "earlier"},
                {"role": "assistant", "tool_calls": [call("old")]},
                {"role": "tool", "tool_call_id": "old", "content": "x"},
                {"role": "assistant", "content": "done"}]
    req = {"model": "fast", "messages": turn([call("s1", other=1), call("s2", None), call("b1", "B")],
                                             prefix=old_turn)}
    before = copy.deepcopy(req)
    out = with_placeholder_signatures(req, "model-b", {"s1": "model-a", "b1": "model-b"})
    assert req == before
    assert sigs(out) == {"old": "REAL-SIG", "s1": PLACEHOLDER_SIGNATURE, "s2": PLACEHOLDER_SIGNATURE, "b1": "B"}
    s1 = next(tc for m in out["messages"] for tc in m.get("tool_calls") or [] if tc["id"] == "s1")
    assert s1["extra_content"]["other"] == 1 and s1["function"] == call("s1")["function"]
    assert out["messages"][1] is req["messages"][1]  # untouched messages are shared, not copied


def test_rewrite_returns_the_same_object_when_nothing_changes():
    req = {"model": "fast", "messages": turn([call("s1", PLACEHOLDER_SIGNATURE)])}
    assert with_placeholder_signatures(req, "model-b", {"s1": "model-a"}) is req
    req2 = {"model": "fast", "messages": turn([call("b1", "B")])}
    assert with_placeholder_signatures(req2, "model-b", {"b1": "model-b"}) is req2


def test_rewrite_tolerates_odd_shapes():
    odd = {"model": "fast", "messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "tool_calls": ["not-a-dict", {"id": 5}, {"type": "function"}]},
        {"role": "assistant", "tool_calls": [{"id": "s1", "extra_content": "junk"}]},
        "garbage"]}
    out = with_placeholder_signatures(odd, "model-b", {})
    calls = [tc for m in out["messages"] if isinstance(m, dict) for tc in m.get("tool_calls") or []]
    assert calls[0] == "not-a-dict"
    assert all(tc["extra_content"]["google"]["thought_signature"] == PLACEHOLDER_SIGNATURE
               for tc in calls[1:])
    assert with_placeholder_signatures({"messages": "nope"}, "m", {}) == {"messages": "nope"}


# ---- config ------------------------------------------------------------------------

@pytest.mark.parametrize("top, message", [
    ({"handoff_targets": ["zzz"]}, "unknown model"),
    ({"handoff_targets": ["a", "a"]}, "twice"),
    ({"handoff_after_s": -1}, "handoff_after_s"),
    ({"handoffs": [{"from": "a", "to": "b"}]}, "replaced by handoff_targets"),
])
def test_config_rejects_bad_handoff_settings(top, message):
    with pytest.raises(ConfigError, match=message):
        make_config(**top)


def test_config_defaults():
    cfg = make_config()
    assert cfg.handoff_targets == [] and cfg.handoff_after_s == 60


# ---- through HTTP ----------------------------------------------------------------------

def test_http_handoff_sends_placeholder_upstream_and_sets_headers_and_metric():
    up = Upstream({"model-a": [ok(tool_ids=["call-1"]), unavailable()],
                   "model-b": [ok("from b", tool_ids=["call-2"])]})
    p = make_proxy(handoff_targets=["b"])
    with mocked(up), running(p) as c:
        post = lambda body: c.post("/v1/chat/completions", json=body, headers=AUTH)  # noqa: E731
        post(chat_body())
        follow = chat_body()
        follow["messages"] += [{"role": "assistant", "tool_calls": [{
                                    "id": "call-1", "type": "function",
                                    "function": {"name": "f", "arguments": "{}"},
                                    "extra_content": {"google": {"thought_signature": "A-SIG"}}}]},
                               {"role": "tool", "tool_call_id": "call-1", "content": "done"}]
        r = post(follow)
        metrics = c.get("/metrics", headers=AUTH).text
        assert p.affinity.lookup(["call-2"]) == {"call-2": "model-b"}
    assert r.status_code == 200
    assert r.headers["x-served-model"] == "model-b"
    assert r.headers["x-turn-affinity"] == "handoff"
    assert r.headers["x-handoff-from"] == "model-a"
    assert r.headers["x-fallback-path"] == "model-a,model-b"
    assert up.body(1)["messages"][-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "A-SIG"
    sent_b = up.body(2)
    assert sent_b["model"] == "model-b"
    assert sent_b["messages"][-2]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": PLACEHOLDER_SIGNATURE}}
    assert 'qr_handoff_total{from="model-a",result="served",to="model-b"} 1' in metrics


def test_http_400_after_handoff_is_logged_with_both_models():
    from apphelpers import captured_logs
    body400 = error_body(400, "INVALID_ARGUMENT", "Function call is missing a thought_signature")
    up = Upstream({"model-a": [ok(tool_ids=["call-1"]), unavailable()],
                   "model-b": [httpx.Response(400, json=body400)]})
    p = make_proxy(handoff_targets=["b"])
    with mocked(up), running(p) as c, captured_logs() as logs:
        c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        follow = chat_body()
        follow["messages"] += [{"role": "assistant", "tool_calls": [{
                                    "id": "call-1", "type": "function",
                                    "function": {"name": "f", "arguments": "{}"}}]},
                               {"role": "tool", "tool_call_id": "call-1", "content": "done"}]
        r = c.post("/v1/chat/completions", json=follow, headers=AUTH)
    assert r.status_code == 400 and r.json() == body400
    assert r.headers["x-handoff-from"] == "model-a"
    assert "handed off from model-a" in logs.getvalue() and "remove b from handoff_targets" in logs.getvalue()
