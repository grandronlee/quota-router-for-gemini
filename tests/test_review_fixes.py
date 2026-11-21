"""Regression tests for decisions taken after the Phase 1 review (DESIGN §5.2)."""

from quota_router.clock import next_pt_midnight
from quota_router.router import RateLimited, RouteFailed, RouteServed, Unavailable

from helpers import Env, FailingConn, ScriptedUpstream, chat, make_config, model_dict, pt

RPD_429 = {"error": {"code": 429, "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
RPM_429 = {"error": {"code": 429, "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}


# ---- per-day 429 in the first minutes after PT midnight -------------------

def one_model(start: float, **limits) -> Env:
    return Env(make_config({"a": model_dict("model-a", **limits)}), start=start)


def test_per_day_429_within_grace_cools_down_instead_of_exhausting():
    e = one_model(pt(2026, 11, 21, 0, 9, 59))
    out = e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert out.status == 429 and out.retry_after_s == 60
    assert e.store.snapshot()["model-a"]["exhausted_until"] is None


def test_per_day_429_after_grace_exhausts_until_midnight():
    t = pt(2026, 11, 21, 0, 10, 0)
    e = one_model(t)
    out = e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert out.status == 429 and out.retry_after_s == int(next_pt_midnight(t) - t)


def test_pt_day_start_on_both_dst_change_days():
    import datetime as dt
    from quota_router.clock import pt_day_start
    utc = lambda *a: dt.datetime(*a, tzinfo=dt.timezone.utc).timestamp()  # noqa: E731
    # 2026-11-01 01:30 PST (the second 01:30) began at 00:00 PDT = 07:00 UTC.
    assert pt_day_start(utc(2026, 11, 1, 9, 30)) == utc(2026, 11, 1, 7)
    # 2026-03-08 03:30 PDT began at 00:00 PST = 08:00 UTC.
    assert pt_day_start(utc(2026, 3, 8, 10, 30)) == utc(2026, 3, 8, 8)


def test_grace_window_on_the_25_hour_day():
    # 01:30 PST on 2026-11-01 is 2.5 hours into the PT day: no grace, exhausted until 11-02.
    import datetime as dt
    t = dt.datetime(2026, 11, 1, 9, 30, tzinfo=dt.timezone.utc).timestamp()
    e = one_model(t)
    e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert e.store.snapshot()["model-a"]["exhausted_until"] == pt(2026, 11, 2)


# ---- 5xx on a model that is also out of quota ----------------------------

def test_5xx_that_used_the_last_daily_slot_is_quota_not_unavailable():
    t = pt(2026, 11, 20, 12)
    e = one_model(t, rpd=1)
    out = e.route(chat(), ScriptedUpstream({"model-a": Unavailable("503")}))
    assert isinstance(out, RouteFailed) and out.status == 429
    assert out.retry_after_s == int(next_pt_midnight(t) - t)
    assert out.candidates[0].reason == "quota" and "rpd" in out.candidates[0].detail


# ---- SQLite errors degrade instead of failing the request ----------------

def test_reservation_write_failure_skips_the_model_and_grants_nothing(env):
    env.store._conn = FailingConn(env.conn)
    up = ScriptedUpstream()
    env.store._conn.fail_on = "INSERT INTO rpd"
    out = env.route(chat(), up)
    # Neither model can reserve, so neither is called, and memory shows no reservation.
    assert isinstance(out, RouteFailed) and out.status == 503 and up.calls == []
    assert all(s["rpd_used"] == 0 and s["rpm_used"] == 0 for s in env.store.snapshot().values())
    env.store._conn.fail_on = None
    assert isinstance(env.route(chat(), up), RouteServed)


def test_exhausted_write_failure_still_blocks_in_memory_and_falls_back(env):
    env.store._conn = FailingConn(env.conn, fail_on="INSERT INTO exhausted")
    out = env.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert isinstance(out, RouteServed) and out.model.id == "model-b"
    assert env.store.snapshot()["model-a"]["exhausted_until"] is not None
    assert env.conn.execute("SELECT COUNT(*) FROM exhausted").fetchone()[0] == 0


def test_refund_write_failure_keeps_the_slot_and_falls_back(env):
    fc = FailingConn(env.conn)
    env.store._conn = fc

    def arm(model, timeout):  # after model-a's reservation, fail the next rpd write (the refund)
        if model.id == "model-a":
            fc.fail_on, fc.times = "INSERT INTO rpd", 1

    up = ScriptedUpstream({"model-a": RateLimited(RPM_429)}, on_call=arm)
    out = env.route(chat(), up)
    assert isinstance(out, RouteServed) and out.model.id == "model-b" and fc.times == 0
    snap = env.store.snapshot()["model-a"]
    assert snap["rpd_used"] == 1  # refund not applied: conservative
    assert snap["cooldown_until"] is not None


def test_affinity_lookup_failure_routes_normally(env):
    env.affinity.record(["call-1"], "model-b")
    env.affinity._conn = FailingConn(env.conn, fail_on="FROM affinity")
    msgs = [{"role": "user", "content": "x"},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                                                  "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"}]
    out = env.route(chat(messages=msgs), ScriptedUpstream())
    assert isinstance(out, RouteServed) and out.model.id == "model-a" and out.affinity == "unknown"


def test_per_day_429_is_about_the_day_the_request_was_sent():
    # Handled outside the grace window, but sent the previous PT day: the new day isn't lost.
    from quota_router.classify import Classification
    e = one_model(pt(2026, 11, 21, 0, 20))
    e.store.on_rate_limited(e.model("a"), Classification("rpd", None), sent_at=pt(2026, 11, 20, 23, 59, 59))
    snap = e.store.snapshot()["model-a"]
    assert snap["exhausted_until"] is None and snap["cooldown_until"] == pt(2026, 11, 21, 0, 21)


# ---- follow-up review ----------------------------------------------------

def test_grace_also_for_a_model_that_can_use_its_day_in_ten_minutes():
    # Reversed after the Phase 4 review: grace applies to every model (DESIGN §5 notes).
    e = one_model(pt(2026, 11, 21, 0, 4), rpm=5, rpd=20)
    out = e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPD_429)}))
    assert e.store.snapshot()["model-a"]["exhausted_until"] is None
    assert out.status == 429 and out.retry_after_s == 60


def test_rollover_read_failure_after_a_5xx_does_not_raise():
    e = one_model(pt(2026, 11, 20, 23, 59, 55))
    fc = FailingConn(e.conn)
    e.store._conn = fc

    def cross_midnight(model, timeout):
        e.clock.set(pt(2026, 11, 21, 0, 0, 2))
        fc.fail_on = "SELECT count FROM rpd"  # the new day's first read fails

    out = e.route(chat(), ScriptedUpstream({"model-a": Unavailable("503")}, on_call=cross_midnight))
    assert isinstance(out, RouteFailed) and out.status == 503
    assert out.retry_after_s == 30  # not "ready now" when the state can't be read


def test_rollover_read_failure_after_a_429_does_not_advertise_ready_now():
    e = one_model(pt(2026, 11, 20, 23, 59, 55))
    fc = FailingConn(e.conn)
    e.store._conn = fc

    def cross_midnight(model, timeout):
        e.clock.set(pt(2026, 11, 21, 0, 0, 2))
        fc.fail_on = "SELECT count FROM rpd"

    out = e.route(chat(), ScriptedUpstream({"model-a": RateLimited(RPM_429)}, on_call=cross_midnight))
    assert isinstance(out, RouteFailed) and out.status == 429 and out.retry_after_s == 30


# ---- Phase 2 review, finding 8: deadline-shortened attempts ------------------

from quota_router.router import RouteFailed, Unavailable as _Unavailable


def _slow_upstream(env, script):
    """Each model: (seconds the call takes on the fake clock, result). Records timeouts given."""
    timeouts = {}

    async def call(model, timeout_s):
        timeouts[model.id] = timeout_s
        took, result = script[model.id]
        env.clock.advance(min(took, timeout_s))
        return result

    return call, timeouts


def test_timeout_cut_short_by_the_deadline_does_not_cool_down_a_healthy_model():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    call, timeouts = _slow_upstream(e, {"model-a": (120, _Unavailable("timeout")),
                                        "model-b": (70, _Unavailable("timeout"))})
    cooled = []
    real = e.store.on_unavailable
    e.store.on_unavailable = lambda m, res=None: (cooled.append(m.id), real(m, res))
    out = e.route(chat(), call)
    assert isinstance(out, RouteFailed) and out.status == 503
    assert timeouts == {"model-a": 120, "model-b": 60}
    assert cooled == ["model-a"]                           # a full-length timeout still cools down
    assert e.store.check(e.model("b"), 10) is None        # b only got 60 s: no cooldown
    assert e.store.snapshot()["model-b"]["rpd_used"] == 1  # its reservation is kept
    b = next(c for c in out.candidates if c.model.id == "model-b")
    assert b.reason == "unavailable" and "cut short" in b.detail


def test_no_later_attempt_with_less_than_the_minimum_time_left():
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}, request_deadline_s=124))
    call, timeouts = _slow_upstream(e, {"model-a": (120, _Unavailable("http 503")),
                                        "model-b": (1, _Unavailable("http 503"))})
    out = e.route(chat(), call)
    assert list(timeouts) == ["model-a"]  # 4 s left: model-b isn't called
    assert out.status == 503 and out.retry_after_s == 1
    b = next(c for c in out.candidates if c.model.id == "model-b")
    assert b.reason == "not_tried"
    assert e.store.snapshot()["model-b"]["rpd_used"] == 0


def test_first_attempt_runs_even_under_a_small_configured_deadline():
    e = Env(make_config({"a": model_dict("model-a")}, request_deadline_s=2))
    call, timeouts = _slow_upstream(e, {"model-a": (2, _Unavailable("timeout"))})
    e.route(chat(), call)
    assert timeouts == {"model-a": 2}
    a = e.store.check(e.model("a"), 10)
    assert a is not None and a.reason == "unavailable"  # its full time ran out: cooldown as before


def test_early_network_timeout_on_a_later_attempt_still_cools_down():
    """Review follow-up: a 10 s connect timeout isn't the deadline's doing."""
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    call, timeouts = _slow_upstream(e, {"model-a": (61, _Unavailable("http 503")),
                                        "model-b": (10, _Unavailable("timeout"))})
    e.route(chat(), call)
    assert timeouts["model-b"] == 119
    b = e.store.check(e.model("b"), 10)
    assert b is not None and b.reason == "unavailable"


def test_nearly_full_budget_hang_on_a_later_attempt_still_cools_down():
    """Review follow-up: 119 s instead of 120 s is not "cut short"."""
    e = Env(make_config({"a": model_dict("model-a"), "b": model_dict("model-b")}))
    call, timeouts = _slow_upstream(e, {"model-a": (61, _Unavailable("http 503")),
                                        "model-b": (119, _Unavailable("timeout"))})
    out = e.route(chat(), call)
    assert timeouts["model-b"] == 119
    b = next(c for c in out.candidates if c.model.id == "model-b")
    assert "cut short" not in b.detail
    assert e.store.check(e.model("b"), 10) is not None
