import asyncio

import pytest

from quota_router.classify import Classification
from quota_router.clock import next_pt_midnight
from quota_router.router import (ModelNotFound, RateLimited, Rejected, RouteFailed, RoutePassthrough,
                                 RouteServed, Served, Unavailable, active_turn_tool_call_ids,
                                 estimate_input_tokens)

from helpers import T0, Env, ScriptedUpstream, chat, make_config, model_dict, pt

RPM_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "20s"}]}}
RPD_429 = [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}]
SIGNATURE_400 = {"error": {"code": 400, "message": "Function call is missing a thought_signature"}}


def four(**over) -> Env:
    models = {k: model_dict(f"model-{k}", **over.get(k, {})) for k in "abcd"}
    top = {k: v for k, v in over.items() if k not in "abcd"}
    return Env(make_config(models, {"fast": list("abcd")}, **top))


def tool_turn(*call_ids_per_step: list[str]) -> list[dict]:
    msgs = [{"role": "user", "content": "do it"}]
    for ids in call_ids_per_step:
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for i in ids]})
        msgs += [{"role": "tool", "tool_call_id": i, "content": "ok"} for i in ids]
    return msgs


# ---- basic flow ----------------------------------------------------------

def test_serves_first_candidate(env):
    up = ScriptedUpstream()
    out = env.route(chat(), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-a"
    assert out.path == ["model-a"] and out.affinity is None
    assert env.store.snapshot()["model-a"]["rpd_used"] == 1


def test_429_falls_back_and_refunds_the_rpd_slot(env):
    up = ScriptedUpstream({"model-a": RateLimited(RPM_429)})
    out = env.route(chat(), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert out.path == ["model-a", "model-b"]
    snap = env.store.snapshot()["model-a"]
    assert snap["rpd_used"] == 0 and snap["rpm_used"] == 1  # RPD refunded, RPM slot kept
    assert snap["cooldown_until"] == T0 + 20


def test_per_day_429_marks_exhausted(env):
    out = env.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert out.model.id == "model-b"
    assert env.store.snapshot()["model-a"]["exhausted_until"] == next_pt_midnight(T0)


def test_per_day_429_handled_after_midnight_does_not_lock_out_the_new_day():
    e = Env(make_config({"a": model_dict("model-a")}), start=pt(2026, 11, 20, 23, 59, 59))
    midnight = pt(2026, 11, 21)
    up = ScriptedUpstream({"model-a": RateLimited(RPD_429)},
                          on_call=lambda m, t: e.clock.set(midnight + 0.2))
    out = e.route(chat(), up)
    assert out.status == 429 and out.retry_after_s <= 60  # was: exhausted until the next midnight
    assert e.store.snapshot()["model-a"]["exhausted_until"] is None


def test_infinite_retry_delay_does_not_crash_or_block_forever(env):
    body = {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "9" * 400 + "s"}]}}
    out = env.route(chat("model-a"), ScriptedUpstream({"model-a": RateLimited(body)}))
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 60


def test_malformed_429_still_refunds_and_falls_back(env):
    body = {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": 1}]}}
    out = env.route(chat(), ScriptedUpstream({"model-a": RateLimited(body)}))
    assert out.model.id == "model-b"
    assert env.store.snapshot()["model-a"]["rpd_used"] == 0


def test_tpm_window_holds_input_only(env):
    req = chat(max_tokens=60_000)
    out = env.route(req, ScriptedUpstream())
    estimate = estimate_input_tokens(req)
    assert env.store.snapshot()["model-a"]["tpm_input_used"] == estimate < 100
    env.router.complete(out, prompt_tokens=8)  # a large completion is never added
    assert env.store.snapshot()["model-a"]["tpm_input_used"] == 8


def test_5xx_falls_back_and_keeps_the_reservation(env):
    out = env.route(chat(), ScriptedUpstream({"model-a": Unavailable("503")}))
    assert out.model.id == "model-b"
    snap = env.store.snapshot()["model-a"]
    assert snap["rpd_used"] == 1 and snap["cooldown_until"] == T0 + 30


def test_other_4xx_is_returned_unchanged_without_fallback(env):
    up = ScriptedUpstream({"model-a": Rejected(SIGNATURE_400)})
    out = env.route(chat(), up)
    assert isinstance(out, RoutePassthrough) and out.payload == SIGNATURE_400
    assert up.called == ["model-a"]


def test_404_model_disables_it_and_falls_back(env):
    up = ScriptedUpstream({"model-a": ModelNotFound({"error": {"code": 404}})})
    assert env.route(chat(), up).model.id == "model-b"
    assert env.store.is_disabled(env.model("a"))
    assert env.store.snapshot()["model-a"]["rpd_used"] == 0
    env.route(chat(), up)
    assert up.called == ["model-a", "model-b", "model-b"]


def test_unknown_model_name_is_404(env):
    out = env.route(chat("nope"), ScriptedUpstream())
    assert isinstance(out, RouteFailed) and (out.status, out.code) == (404, "model_not_found")


def test_real_model_id_routes_to_that_model_alone(env):
    up = ScriptedUpstream({"model-b": Unavailable("503")})
    out = env.route(chat("model-b"), up)
    assert isinstance(out, RouteFailed) and out.status == 503 and up.called == ["model-b"]


def test_upstream_that_ignores_its_timeout_is_cut_off():
    # The fake clock never moves, so only the router's own wait_for can end the call.
    e = Env(make_config({"a": model_dict("model-a")}, request_deadline_s=0.05))

    async def hang(model, timeout_s):
        await asyncio.sleep(10)

    out = e.route(chat(), hang)
    assert isinstance(out, RouteFailed) and out.status == 503
    assert out.candidates[0].reason == "unavailable" and out.candidates[0].detail == "timeout"


# ---- size and context fit ------------------------------------------------

def test_estimate_counts_messages_and_tools():
    small = estimate_input_tokens(chat())
    with_tools = estimate_input_tokens(chat(tools=[{"type": "function",
                                                    "function": {"name": "x" * 400}}]))
    assert 0 < small < with_tools
    img = chat(messages=[{"role": "user", "content": [
        {"type": "text", "text": "a" * 40},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1_000_000}}]}])
    assert estimate_input_tokens(img) < 2000  # image bytes are not counted as text


def test_too_large_for_every_candidate_is_400_without_reservation(env):
    up = ScriptedUpstream()
    big = chat(messages=[{"role": "user", "content": "x" * 1_000_000}])  # ~300K tokens > 250K TPM
    out = env.route(big, up)
    assert isinstance(out, RouteFailed) and (out.status, out.code) == (400, "context_length_exceeded")
    assert out.retry_after_s is None and up.calls == []
    assert all(s["rpd_used"] == 0 and s["rpm_used"] == 0 for s in env.store.snapshot().values())


def test_max_tokens_over_output_limit_is_too_large(env):
    out = env.route(chat(max_tokens=70_000), ScriptedUpstream())
    assert out.status == 400 and out.code == "context_length_exceeded"


def test_skips_model_whose_free_tpm_is_too_small():
    e = Env(make_config({"a": model_dict("model-a", tpm_input=5000), "b": model_dict("model-b")}))
    e.store.try_reserve(e.model("a"), 4000)
    req = chat(messages=[{"role": "user", "content": "x" * 8000}])  # ~2400 tokens > 1000 free
    up = ScriptedUpstream()
    out = e.route(req, up)
    assert out.model.id == "model-b" and up.called == ["model-b"]


# ---- final status table (DESIGN §5 step 5) -------------------------------

def test_all_disabled_is_503_no_enabled_model_without_retry_after(env):
    for k in "ab":
        env.store.disable(env.model(k))
    out = env.route(chat(), ScriptedUpstream())
    assert (out.status, out.code, out.retry_after_s) == (503, "no_enabled_model", None)


def test_too_large_and_rest_disabled_is_400():
    e = Env(make_config({"a": model_dict("model-a", tpm_input=100), "b": model_dict("model-b")}))
    e.store.disable(e.model("b"))
    out = e.route(chat(messages=[{"role": "user", "content": "x" * 1000}]), ScriptedUpstream())
    assert (out.status, out.code) == (400, "context_length_exceeded")


def test_disabled_plus_quota_is_429(env):
    env.store.disable(env.model("a"))
    env.store.on_rate_limited(env.model("b"), Classification("rpm", 15.0))
    out = env.route(chat(), ScriptedUpstream())
    assert (out.status, out.code, out.retry_after_s) == (429, "rate_limit_exceeded", 15)


def test_quota_plus_unavailable_is_503(env):
    env.store.on_rate_limited(env.model("a"), Classification("rpm", 45.0))
    out = env.route(chat(), ScriptedUpstream({"model-b": Unavailable("503")}))
    assert out.status == 503 and out.code == "upstream_unavailable"
    assert out.retry_after_s == 30  # b's 5xx cooldown ends before a's 429 cooldown
    assert "model-a: quota" in out.message and "model-b: unavailable" in out.message


def test_every_model_exhausted_is_429_until_pt_midnight(env):
    for k in "ab":
        env.store.on_rate_limited(env.model(k), Classification("rpd", None))
    out = env.route(chat(), ScriptedUpstream())
    assert out.status == 429 and out.retry_after_s == int(next_pt_midnight(T0) - T0)


def test_retry_after_ignores_models_that_cannot_serve_the_request():
    e = Env(make_config({"a": model_dict("model-a", tpm_input=2000), "b": model_dict("model-b")}))
    # a can never take this request (too large); b is out for the day.
    e.store.on_rate_limited(e.model("b"), Classification("rpd", None))
    out = e.route(chat(messages=[{"role": "user", "content": "x" * 8000}]), ScriptedUpstream())
    assert out.status == 429 and out.retry_after_s == int(next_pt_midnight(T0) - T0)


def test_retry_after_waits_for_tpm_room_for_this_input():
    e = Env(make_config({"a": model_dict("model-a", tpm_input=10_000)}))
    a = e.model("a")
    e.store.try_reserve(a, 3000)           # expires at T0+60
    e.clock.advance(30)
    e.store.try_reserve(a, 6000)           # expires at T0+90
    req = chat(messages=[{"role": "user", "content": "x" * 20_000}])  # ~6000 tokens
    out = e.route(req, ScriptedUpstream())
    # 1000 free now; after T0+60 there are 4000 free, still too few; after T0+90 there's room.
    assert out.status == 429 and out.retry_after_s == 60


# ---- attempts and deadline -----------------------------------------------

def test_stops_after_max_attempts_and_skips_do_not_count():
    e = four(a={"rpm": 1})
    e.store.try_reserve(e.model("a"), 1)  # a is full: skipped, not an attempt
    up = ScriptedUpstream({m: Unavailable("503") for m in ("model-b", "model-c", "model-d")})
    out = e.route(chat(), up)
    assert up.called == ["model-b", "model-c", "model-d"]
    assert out.status == 503

    e2 = four()
    up2 = ScriptedUpstream({f"model-{k}": Unavailable("503") for k in "abcd"})
    out2 = e2.route(chat(), up2)
    assert up2.called == ["model-a", "model-b", "model-c"]
    assert out2.status == 503 and out2.candidates[3].reason == "not_tried"
    assert out2.retry_after_s == 1  # not_tried counts as "now"


def test_not_tried_model_that_is_blocked_reports_its_real_ready_time():
    e = four(max_attempts=1)
    e.store.on_rate_limited(e.model("b"), Classification("rpm", 40.0))
    out = e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPM_429)}))
    reasons = [c.reason for c in out.candidates]
    assert reasons == ["quota", "quota", "not_tried", "not_tried"]
    assert out.status == 503 and out.retry_after_s == 1


def test_deadline_stops_the_loop_and_caps_each_attempt_timeout():
    e = four()
    up = ScriptedUpstream({f"model-{k}": Unavailable("timeout") for k in "abcd"},
                          on_call=lambda m, t: e.clock.advance(100))
    out = e.route(chat(), up)
    assert [round(t) for _, t in up.calls] == [120, 80]  # min(120, time left)
    assert out.status == 503
    assert [c.reason for c in out.candidates] == ["unavailable", "unavailable", "not_tried", "not_tried"]


def test_deadline_counts_from_arrival():
    e = four()
    up = ScriptedUpstream()
    out = e.route(chat(), up, started_at=T0 - 179.5)
    assert isinstance(out, RouteServed) and round(up.calls[0][1], 1) == 0.5
    out = e.route(chat(), up, started_at=T0 - 180)
    assert isinstance(out, RouteFailed) and out.status == 503 and len(up.calls) == 1


# ---- parallel reservations ----------------------------------------------

def test_parallel_requests_never_exceed_rpm():
    e = Env(make_config({"a": model_dict("model-a", rpm=3), "b": model_dict("model-b", rpm=2)}))
    up = ScriptedUpstream()

    async def go():
        return await asyncio.gather(*(e.router.route(chat(), up) for _ in range(10)))

    outs = asyncio.run(go())
    served = [o for o in outs if isinstance(o, RouteServed)]
    assert len(served) == 5
    assert up.called.count("model-a") == 3 and up.called.count("model-b") == 2
    assert all(o.status == 429 for o in outs if isinstance(o, RouteFailed))


def test_parallel_requests_never_exceed_rpd():
    e = Env(make_config({"a": model_dict("model-a", rpm=100, rpd=4)}))
    up = ScriptedUpstream()

    async def go():
        return await asyncio.gather(*(e.router.route(chat(), up) for _ in range(12)))

    outs = asyncio.run(go())
    assert sum(isinstance(o, RouteServed) for o in outs) == 4
    assert e.conn.execute("SELECT count FROM rpd").fetchone()[0] == 4
    assert e.store.snapshot()["model-a"]["rpd_used"] == 4


# ---- tool-loop pinning (DESIGN §5.1) -------------------------------------

def test_active_turn_ids():
    answered = [{"role": "assistant", "content": "done"}]
    msgs = tool_turn(["old"]) + answered + tool_turn(["c1", "c2"], ["c3"])
    assert active_turn_tool_call_ids(msgs) == ["c1", "c2", "c3"]
    # A user message straight after tool results continues the turn (DESIGN §5.1).
    assert active_turn_tool_call_ids(tool_turn(["old"]) + tool_turn(["c1"])) == ["old", "c1"]
    assert active_turn_tool_call_ids([{"role": "user", "content": "x"}]) == []
    assert active_turn_tool_call_ids(None) == []


def test_tool_turn_is_pinned_and_does_not_fall_back(env):
    env.affinity.record(["call-1"], "model-b")
    up = ScriptedUpstream()
    out = env.route(chat(messages=tool_turn(["call-1"])), up)
    assert out.model.id == "model-b" and out.affinity == "pinned" and up.called == ["model-b"]

    env.store.on_rate_limited(env.model("b"), Classification("rpm", 25.0))
    out = env.route(chat(messages=tool_turn(["call-1"])), up)
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 25
    assert up.called == ["model-b"]  # no new call, and never model-a


def test_pinned_model_unavailable_is_503_without_fallback(env):
    env.affinity.record(["call-1"], "model-a")
    up = ScriptedUpstream({"model-a": Unavailable("503")})
    out = env.route(chat(messages=tool_turn(["call-1"])), up)
    assert out.status == 503 and up.called == ["model-a"]


def test_multi_step_turn_is_pinned_to_the_turns_model(env):
    # Step 1 was served by b and recorded; step 2's call ID is unknown (e.g. not recorded).
    env.affinity.record(["s1"], "model-b")
    out = env.route(chat(messages=tool_turn(["s1"], ["s2"])), ScriptedUpstream())
    assert out.model.id == "model-b" and out.affinity == "pinned"


def test_unknown_ids_route_normally(env, caplog):
    out = env.route(chat(messages=tool_turn(["never-seen"])), ScriptedUpstream())
    assert out.model.id == "model-a" and out.affinity == "unknown"
    assert "none known" in caplog.text and "never-seen" not in caplog.text


def test_mixed_ids_pin_to_the_latest_calls_model(env):
    env.affinity.record(["s1"], "model-a")
    env.affinity.record(["s2"], "model-b")
    out = env.route(chat(messages=tool_turn(["s1"], ["s2"])), ScriptedUpstream())
    assert out.model.id == "model-b" and out.affinity == "mixed"


def test_signature_400_on_a_pinned_turn_is_returned_unchanged(env):
    env.affinity.record(["call-1"], "model-a")
    up = ScriptedUpstream({"model-a": Rejected(SIGNATURE_400)})
    out = env.route(chat(messages=tool_turn(["call-1"])), up)
    assert isinstance(out, RoutePassthrough) and out.payload == SIGNATURE_400 and up.called == ["model-a"]


def test_complete_records_affinity_and_settles_usage(env):
    out = env.route(chat(), ScriptedUpstream())
    env.router.complete(out, prompt_tokens=7, tool_call_ids=["t1", "t2"])
    assert env.affinity.lookup(["t1", "t2", "t3"]) == {"t1": "model-a", "t2": "model-a"}
    assert env.store.snapshot()["model-a"]["tpm_input_used"] == 7


def test_complete_survives_an_affinity_write_failure(env, caplog):
    out = env.route(chat(), ScriptedUpstream())

    def boom(*a):
        raise OSError("disk full")

    env.affinity.record = boom
    env.router.complete(out, prompt_tokens=7, tool_call_ids=["t1"])
    assert "could not record" in caplog.text


def test_affinity_survives_restart_and_expires_after_24h(tmp_path):
    db = str(tmp_path / "q.sqlite3")
    cfg = make_config()
    e1 = Env(cfg, db=db)
    e1.affinity.record(["call-1"], "model-b")
    e1.conn.close()

    e2 = Env(cfg, db=db, start=T0 + 23 * 3600)
    out = e2.route(chat(messages=tool_turn(["call-1"])), ScriptedUpstream())
    assert out.model.id == "model-b" and out.affinity == "pinned"
    e2.conn.close()

    e3 = Env(cfg, db=db, start=T0 + 24 * 3600)
    out = e3.route(chat(messages=tool_turn(["call-1"])), ScriptedUpstream())
    assert out.model.id == "model-a" and out.affinity == "unknown"
    assert e3.conn.execute("SELECT COUNT(*) FROM affinity").fetchone()[0] == 0


def test_affinity_expires_after_24h_without_a_restart(env):
    env.affinity.record(["call-1"], "model-b")
    env.clock.advance(24 * 3600 - 1)
    assert env.affinity.lookup(["call-1"]) == {"call-1": "model-b"}
    env.clock.advance(1)
    assert env.affinity.lookup(["call-1"]) == {}


@pytest.mark.parametrize("status, code",[(429, "rate_limit_exceeded"), (503, "upstream_unavailable")])
def test_error_body_is_openai_shaped(env, status, code):
    if status == 429:
        for k in "ab":
            env.store.on_rate_limited(env.model(k), Classification("rpm", 10.0))
        out = env.route(chat(), ScriptedUpstream())
    else:
        out = env.route(chat(), ScriptedUpstream({m: Unavailable("x") for m in ("model-a", "model-b")}))
    body = out.error_body()["error"]
    assert body["code"] == code and body["message"] and body["type"]
