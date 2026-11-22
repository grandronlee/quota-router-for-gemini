"""Request-log detail and the per-model first-byte timeout (DESIGN §24)."""

from __future__ import annotations

import json

import pytest

from apphelpers import (AUTH, Upstream, captured_logs, chat_body, make_proxy, mocked, ok, rate_limited,
                        running, unavailable)
from helpers import Env, ScriptedUpstream, T0, chat, make_config, model_dict
from quota_router.app import outcome_log_fields
from quota_router.config import ConfigError
from quota_router.router import Candidate, RouteFailed, RouteServed, Unavailable
from test_auto import AUTO, MODELS, ROUTES


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


def post(c, body):
    return c.post("/v1/chat/completions", json=body, headers=AUTH)


def tool_follow_up(call_id: str, model: str = "fast") -> dict:
    body = chat_body(model)
    body["messages"] += [{"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
                          "function": {"name": "f", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": call_id, "content": "done"}]
    return body


# ---- 24.1: request-log detail ------------------------------------------------

def test_route_fields_only_on_gemini_auto_lines():
    p = make_proxy(MODELS, ROUTES, auto=AUTO)
    with mocked(Upstream()), running(p) as c, captured_logs() as logs:
        post(c, chat_body("gemini-auto"))
        post(c, chat_body("gemini-fast"))
    auto, fast = request_lines(logs)
    assert (auto["route_tier"], auto["route_reason"]) == ("smart", "pace-upgrade")
    assert "route_tier" not in fast and "route_reason" not in fast
    # A served request gets no rejection detail.
    assert "retry_after_s" not in auto and "blocked" not in auto


def test_rpm_blocked_pinned_owner_logs_its_limit_and_hint():
    p = make_proxy({"a": model_dict("model-a", rpm=1)}, {"fast": ["a"]})
    with mocked(Upstream({"model-a": [ok(tool_ids=["c1"])]})), running(p) as c, captured_logs() as logs:
        post(c, chat_body())
        r = post(c, tool_follow_up("c1"))  # the owner's one RPM slot is used: 429, no handoff
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    line = request_lines(logs)[-1]
    assert line["affinity"] == "pinned" and line["attempts"] == []
    assert line["retry_after_s"] == 60
    assert line["blocked"] == [{"model": "model-a", "reason": "quota", "limits": ["rpm"], "ready_in_s": 60}]


def test_upstream_429_logs_its_kind_and_the_cooldown():
    p = make_proxy()
    up = Upstream({"model-a": [rate_limited("minute", "20s")], "model-b": [rate_limited("minute", "30s")]})
    with mocked(up), running(p) as c, captured_logs() as logs:
        r = post(c, chat_body())
    assert r.status_code == 429
    line = request_lines(logs)[-1]
    assert line["retry_after_s"] == 20
    assert line["blocked"] == [
        {"model": "model-a", "reason": "quota", "limits": ["upstream_429_rpm", "cooldown_429"], "ready_in_s": 20},
        {"model": "model-b", "reason": "quota", "limits": ["upstream_429_rpm", "cooldown_429"], "ready_in_s": 30},
    ]


def test_503_logs_the_5xx_cooldown():
    p = make_proxy()
    with mocked(Upstream({"model-a": [unavailable()], "model-b": [unavailable()]})), running(p) as c, \
            captured_logs() as logs:
        r = post(c, chat_body())
    assert r.status_code == 503
    line = request_lines(logs)[-1]
    assert line["retry_after_s"] == int(r.headers["Retry-After"])
    assert [b["limits"] for b in line["blocked"]] == [["cooldown_5xx"], ["cooldown_5xx"]]
    assert all(b["reason"] == "unavailable" and b["ready_in_s"] == 30 for b in line["blocked"])


def test_unknown_limits_become_other_and_detail_text_is_never_logged():
    m = make_config().models["a"]
    c = Candidate(m, reason="unavailable", ready_at=T0 + 2.5, detail="secret upstream text",
                  limits=["weird", "rpm", "also weird"])
    skipped = Candidate(make_config().models["b"], reason="too_large")
    out = outcome_log_fields(RouteFailed(503, "upstream_unavailable", "msg", 3, candidates=[c, skipped]), T0)
    assert out == {"retry_after_s": 3, "blocked": [
        {"model": "model-a", "reason": "unavailable", "limits": ["other", "rpm"], "ready_in_s": 3},
        {"model": "model-b", "reason": "too_large", "limits": [], "ready_in_s": None}]}
    assert "secret" not in json.dumps(out)


def test_deferred_model_keeps_no_stale_labels_after_its_last_resort_call():
    # Found in review: a model deferred in the first pass (labelled cooldown_5xx) kept that label
    # after answering 404 in the last-resort pass.
    from quota_router.router import ModelNotFound, RateLimited
    from test_cooldown import fail_times
    e = Env(make_config(None, None))
    fail_times(e, 2)  # streak 2: past the hard 30 s, still in the soft part, so deferred
    up = ScriptedUpstream({"model-a": ModelNotFound({}), "model-b": RateLimited({})})
    out = e.route(chat(), up)
    a, b = out.candidates
    assert up.called == ["model-b", "model-a"]
    assert (a.reason, a.limits) == ("disabled", [])
    assert b.limits == ["upstream_429_unknown", "cooldown_429"]


def test_400_and_served_outcomes_get_no_rejection_detail():
    m = make_config().models["a"]
    assert outcome_log_fields(RouteFailed(400, "context_length_exceeded", "msg", None), T0) == {}
    assert outcome_log_fields(RouteServed(m, None, None, [], None), T0) == {}


# ---- 24.2: per-model first-byte timeout ---------------------------------------

def timed_env(first_byte_timeout_s=None, **top) -> Env:
    a = model_dict("model-a") if first_byte_timeout_s is None else \
        model_dict("model-a", first_byte_timeout_s=first_byte_timeout_s)
    return Env(make_config({"a": a, "b": model_dict("model-b")}, {"fast": ["a", "b"]}, **top))


def streak(e: Env, key: str = "a") -> int:
    return e.store.snapshot()[e.model(key).id]["unavailable_streak"]


def test_configured_timeout_bounds_the_attempt_and_the_fallback_follows():
    e = timed_env(10)
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    out = e.route(chat(), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert up.calls == [("model-a", 10.0), ("model-b", 120.0)]


def test_models_without_the_setting_keep_120s():
    e = timed_env()
    up = ScriptedUpstream()
    e.route(chat(), up)
    assert up.calls == [("model-a", 120.0)]


def test_request_deadline_still_wins():
    e = timed_env(30)
    up = ScriptedUpstream()
    e.route(chat(), up, started_at=e.clock.now() - 175)  # 5 s left
    assert up.calls == [("model-a", 5.0)]


def test_timeout_using_the_models_own_budget_grows_the_streak():
    e = timed_env(10)
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up)
    assert streak(e) == 1


def test_timeout_cut_short_by_the_deadline_does_not_count():
    e = timed_env(30)
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    e.route(chat(), up, started_at=e.clock.now() - 170)  # 10 s left: at least 5 s under 30
    assert streak(e) == 0
    assert e.store.check(e.model("a"), 10) is None  # no cooldown either


def test_small_budget_cut_short_by_the_deadline_does_not_cool_down():
    # Found in review: with a budget of 5 s or less, the 5 s "cut short" margin could never
    # apply, so a first attempt the deadline cut to 2 of its 3 s got a 30 s hard cooldown.
    e = timed_env(3)
    up = ScriptedUpstream({"model-a": Unavailable("timeout", body_sent=True)},
                          on_call=lambda m, t: e.clock.advance(t))
    out = e.route(chat(), up, started_at=e.clock.now() - 178)  # 2 s left
    assert up.calls[0] == ("model-a", 2.0)
    assert out.candidates[0].detail == "timeout (cut short by the request deadline)"
    assert streak(e) == 0 and e.store.check(e.model("a"), 10) is None


@pytest.mark.parametrize("value", [0.5, 0, 121, -1])
def test_timeout_outside_1_to_120_is_rejected(value):
    with pytest.raises(ConfigError, match="first_byte_timeout_s"):
        make_config({"a": model_dict("model-a", first_byte_timeout_s=value)})


def test_timeout_is_optional():
    assert make_config().models["a"].first_byte_timeout_s is None
    assert make_config({"a": model_dict("model-a", first_byte_timeout_s=45)}).models["a"].first_byte_timeout_s == 45
