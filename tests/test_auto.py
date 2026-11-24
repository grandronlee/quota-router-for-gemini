"""gemini-auto: tiers, spill and pacing (DESIGN §16.9)."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from apphelpers import (AUTH, Upstream, chat_body, make_proxy, mocked, rate_limited, running,
                        unavailable)
from helpers import Env, FailingConn, ScriptedUpstream, T0, chat, make_config, model_dict, pt
from quota_router.classify import Classification
from quota_router.clock import PT
from quota_router.config import ConfigError, parse_config
from quota_router.db import MIGRATIONS, SCHEMA, SCHEMA_VERSION, connect
from quota_router.pacing import DemandCurve
from quota_router.router import RateLimited, RouteServed
from quota_router.turns import new_turn_id

# Two smart models (40 RPD in all) and two fast ones; no RPM limit, so tests can use quota freely.
MODELS = {
    "s1": model_dict("smart-1", rpm=None, rpd=20),
    "s2": model_dict("smart-2", rpm=None, rpd=20),
    "f1": model_dict("fast-1", rpm=None, rpd=500),
    "f2": model_dict("fast-2", rpm=None, rpd=500),
}
ROUTES = {"gemini-fast": ["f1", "f2"], "gemini-smart": ["s1", "s2", "f1", "f2"]}
AUTO = {"name": "gemini-auto", "tiers": {"smart": ["s1", "s2"], "fast": ["f1", "f2"]},
        "default_tier": "fast", "pacing": {"tier": "smart", "reserve": 5}}
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]


def auto_config(auto: dict | None = None, **top):
    return make_config(MODELS, ROUTES, auto=auto or AUTO, **top)


def auto_env(start: float = T0, auto: dict | None = None, **top) -> Env:
    return Env(auto_config(auto, **top), start=start)


def use(env: Env, key: str, n: int) -> None:
    """Use `n` of the model's daily requests."""
    for _ in range(n):
        res = env.store.try_reserve(env.model(key), 1)
        assert not hasattr(res, "reason"), res


def add_turn(conn, day: str, hour: int, reason: str = "first", steps: int = 1, route: str = "r") -> None:
    conn.execute("INSERT INTO turns (turn_id, day, hour, route, reason, tools, input_tokens, "
                 "output_tokens, steps, errors, handoffs, last_status) "
                 "VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, 0, 0, 200)",
                 (new_turn_id(), day, hour, route, reason, steps))


def days_before(day: dt.date, n: int) -> list[str]:
    return [(day - dt.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def follow_up(*ids: str, model: str = "gemini-auto") -> dict:
    msgs: list = [{"role": "user", "content": "task"}]
    for i in ids:
        msgs += [{"role": "assistant", "tool_calls": [{"id": i, "type": "function",
                  "function": {"name": "f", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": i, "content": "done"}]
    return {"model": model, "messages": msgs}


# ---- config (DESIGN §16.9.1) -----------------------------------------------

def test_auto_route_is_added_to_routes_in_tier_order():
    cfg = auto_config()
    assert cfg.routes["gemini-auto"] == ["s1", "s2", "f1", "f2"]
    assert cfg.tier_of("smart-2") == "smart" and cfg.tier_of("fast-1") == "fast"
    assert [m.key for m in cfg.tier_models("fast")] == ["f1", "f2", "s1", "s2"]


def test_config_without_auto_is_unchanged():
    cfg = make_config(MODELS, ROUTES)
    assert cfg.auto is None and set(cfg.routes) == set(ROUTES) and cfg.tier_of("smart-1") is None


@pytest.mark.parametrize("change, message", [
    ({"name": "gemini-fast"}, "same name as a route"),
    ({"name": "smart-1"}, "same name as a model ID"),
    ({"tiers": {"smart": ["s1", "s2"]}, "default_tier": "smart"}, "at least two tiers"),
    ({"tiers": {"smart": [], "fast": ["f1"]}}, "has no models"),
    ({"tiers": {"smart": ["s1", "nope"], "fast": ["f1"]}}, "unknown model"),
    ({"tiers": {"smart": ["s1", "f1"], "fast": ["f1"]}}, "one tier only"),
    ({"default_tier": "medium"}, "default_tier"),
    ({"pacing": {"tier": "medium"}}, "pacing tier"),
    ({"pacing": {"tier": "fast"}}, "must differ from default_tier"),
    ({"pacing": {"tier": "smart", "working_hours": "9-18", "timezone": "UTC"}}, "HH:MM-HH:MM"),
    ({"pacing": {"tier": "smart", "working_hours": "09:00-09:00", "timezone": "UTC"}}, "same time"),
    ({"pacing": {"tier": "smart", "working_hours": "09:00-18:00"}}, "needs a timezone"),
    ({"pacing": {"tier": "smart", "working_hours": "09:00-18:00", "timezone": "Mars/Olympus"}},
     "IANA"),
    ({"pacing": {"tier": "smart", "reserve": -1}}, "reserve"),
    # Found in review: a name the x-route-tier header can't carry made a served request a 500.
    ({"tiers": {"智能": ["s1", "s2"], "fast": ["f1", "f2"]}, "pacing": {"tier": "智能"}}, "tier name"),
    ({"tiers": {"smart tier": ["s1", "s2"], "fast": ["f1", "f2"]}, "pacing": {"tier": "smart tier"}},
     "tier name"),
    ({"tiers": {"": ["s1", "s2"], "fast": ["f1", "f2"]}, "pacing": {"tier": ""}}, "tier name"),
])
def test_invalid_auto_config_is_rejected(change, message):
    with pytest.raises(ConfigError, match=message):
        make_config(MODELS, ROUTES, auto={**AUTO, **change})


def test_paced_model_without_rpd_is_rejected():
    models = {**MODELS, "s2": model_dict("smart-2", rpm=None, rpd=None)}
    with pytest.raises(ConfigError, match="need a numeric rpd"):
        parse_config({"models": models, "routes": ROUTES, "auto": AUTO})


def test_working_hours_may_wrap_past_midnight():
    cfg = make_config(MODELS, ROUTES, auto={**AUTO, "pacing": {
        "tier": "smart", "working_hours": "22:00-06:00", "timezone": "UTC"}})
    assert cfg.auto.pacing.working_hours == "22:00-06:00"


# ---- the upgrade test (DESIGN §16.9.2) -------------------------------------
# With no measured curve and no working_hours the share is flat over the PT day: 0.5 at T0
# (midday), so a turn is upgraded while left > (40 + 5) × 0.5 = 22.5.

@pytest.mark.parametrize("at, used, upgraded", [
    (pt(2026, 11, 20, 0, 0, 0), 0, False),      # share 0: never upgraded
    (T0, 0, True),
    (T0, 17, True),                             # left 23 > 22.5
    (T0, 18, False),                            # left 22
    (pt(2026, 11, 20, 23, 59, 59), 39, True),   # share ~1: while anything is left
    (pt(2026, 11, 20, 23, 59, 59), 40, False),
])
def test_upgrade_follows_quota_left_against_the_share(at, used, upgraded):
    e = auto_env(start=at)
    use(e, "s1", min(used, 20))
    use(e, "s2", max(0, used - 20))
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto"), up)
    assert isinstance(out, RouteServed)
    if upgraded:
        assert (up.called, out.route_tier, out.route_reason) == (["smart-1" if used < 20 else "smart-2"],
                                                                 "smart", "pace-upgrade")
    else:
        assert (up.called, out.route_tier, out.route_reason) == (["fast-1"], "fast", "default")


@pytest.mark.parametrize("left, upgraded", [(17, False), (18, True)])
def test_upgrade_cutoff_is_exact_despite_float_error(left, upgraded):
    # Found in review, 2026-11-23: (80 + 5) × (1 − 0.8) is 16.999999999999996 in floating point,
    # so 17 left was upgraded although the line is exactly 17.
    from types import SimpleNamespace
    from quota_router.pacing import Pacer
    config = SimpleNamespace(models={}, auto=SimpleNamespace(
        tiers={"smart": []}, pacing=SimpleNamespace(tier="smart", reserve=5)))
    store = SimpleNamespace(pool=lambda models: (80, left))
    curve = SimpleNamespace(share=lambda now: (0.8, "flat"))
    view = Pacer(config, store, curve).view(0.0)
    assert view.upgrade_above == 17
    assert view.upgrade is upgraded


def test_exhausted_model_counts_nothing_even_with_room_by_the_proxys_count():
    e = auto_env()
    e.store.on_rate_limited(e.model("s2"), Classification("rpd", None))  # Google says it's out
    view = e.router.pace_view()
    assert (view.capacity, view.left) == (40, 20)
    out = e.route(chat("gemini-auto"), ScriptedUpstream())
    assert out.route_reason == "default"  # 20 is not above 22.5


def test_disabled_model_leaves_the_capacity():
    e = auto_env()
    e.store.disable(e.model("s2"))
    view = e.router.pace_view()
    assert (view.capacity, view.left, view.upgrade_above) == (20, 20, 12.5)
    assert e.route(chat("gemini-auto"), ScriptedUpstream()).route_reason == "pace-upgrade"


def test_smart_quota_used_through_another_route_counts():
    e = auto_env()
    up = ScriptedUpstream()
    for _ in range(18):
        e.route(chat("gemini-smart"), up)
    assert e.router.pace_view().left == 22
    assert e.route(chat("gemini-auto"), up).route_reason == "default"


def test_failed_state_read_does_not_upgrade(monkeypatch):
    e = auto_env()

    def broken(models):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(e.store, "pool", broken)
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto"), up)
    assert (up.called, out.route_reason, out.route_tier) == (["fast-1"], "default", "fast")
    assert e.router.state_errors == 1
    assert e.router.pace_view() is None and e.router.state_errors == 2


# ---- candidate order (DESIGN §16.9.4) --------------------------------------

def test_default_turn_spills_into_the_smart_tier_when_fast_is_out():
    e = auto_env(start=pt(2026, 11, 20, 1, 0, 0))  # early: not upgraded
    for k in ("f1", "f2"):
        e.store.on_rate_limited(e.model(k), Classification("rpd", None))
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto"), up)
    assert (up.called, out.route_tier, out.route_reason) == (["smart-1"], "fast", "spill")
    assert (out.turn.reason, out.turn.tier) == ("spill", "fast")


def test_upgraded_turn_spills_to_fast_when_smart_models_are_busy():
    e = auto_env()
    up = ScriptedUpstream({"smart-1": RateLimited({}), "smart-2": RateLimited({})})
    out = e.route(chat("gemini-auto"), up)
    assert up.called == ["smart-1", "smart-2", "fast-1"]
    assert (out.route_tier, out.route_reason, out.turn.reason, out.turn.tier) == (
        "smart", "spill", "spill", "smart")


def test_turn_start_reason_and_tier_go_to_the_turn():
    e = auto_env()
    out = e.route(chat("gemini-auto"), ScriptedUpstream())
    assert (out.turn.reason, out.turn.tier) == ("pace-upgrade", "smart")


def _start_tool_turn(e: Env, up: ScriptedUpstream) -> RouteServed:
    out = e.route(chat("gemini-auto", tools=TOOLS), up)
    assert isinstance(out, RouteServed)
    e.router.complete(out, 10, ["c1"])
    return out


def test_pinned_follow_up_stays_with_its_owner_and_is_not_paced_again():
    e = auto_env()
    up = ScriptedUpstream()
    start = _start_tool_turn(e, up)
    assert start.model.key == "s1"
    use(e, "s2", 20)  # pace would now say "default"
    out = e.route(follow_up("c1"), up)
    assert (out.model.key, out.affinity, out.route_tier, out.route_reason) == ("s1", "pinned", "smart", "pinned")


def test_handoff_targets_come_from_the_owners_tier_first():
    e = auto_env(handoff_targets=["f1", "s2", "f2"])
    up = ScriptedUpstream()
    _start_tool_turn(e, up)
    e.store.on_rate_limited(e.model("s1"), Classification("rpd", None))
    out = e.route(follow_up("c1"), up)
    # s2 (the owner's tier) before f1, though handoff_targets lists f1 first; pace isn't checked.
    assert (out.model.key, out.handoff_from, out.route_tier, out.route_reason) == (
        "s2", "smart-1", "smart", "handoff")


def test_failed_handoff_is_labelled_handoff():
    e = auto_env(handoff_targets=["s2"])
    up = ScriptedUpstream({"smart-2": RateLimited({})})
    _start_tool_turn(e, up)
    e.store.on_rate_limited(e.model("s1"), Classification("rpd", None))
    out = e.route(follow_up("c1"), up)
    assert (out.status, out.route_reason, out.route_tier) == (429, "handoff", "smart")


def test_unknown_follow_up_is_decided_as_a_turn_start():
    e = auto_env()
    up = ScriptedUpstream()
    out = e.route(follow_up("never-seen"), up)
    assert (out.affinity, out.route_reason, out.route_tier) == ("unknown", "pace-upgrade", "smart")
    assert (out.turn.reason, out.turn.tier) == ("unknown", "smart")


def test_other_routes_get_no_route_labels():
    e = auto_env()
    for name in ("gemini-fast", "gemini-smart", "smart-1"):
        out = e.route(chat(name), ScriptedUpstream())
        assert (out.route_tier, out.route_reason, out.turn.tier) == (None, None, None), name


def test_request_too_large_for_fast_goes_to_smart():
    models = {**MODELS, "f1": model_dict("fast-1", rpm=None, rpd=500, input_limit=100),
              "f2": model_dict("fast-2", rpm=None, rpd=500, input_limit=100)}
    e = Env(make_config(models, ROUTES, auto=AUTO), start=pt(2026, 11, 20, 1, 0, 0))
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto", messages=[{"role": "user", "content": "x" * 2000}]), up)
    assert (up.called, out.route_reason) == (["smart-1"], "spill")


# ---- the demand curve (DESIGN §16.9.3) -------------------------------------

def curve(conn, **pacing) -> DemandCurve:
    auto = auto_config({**AUTO, "pacing": {"tier": "smart", **pacing}}).auto
    return DemandCurve(conn, auto)


TODAY = dt.date(2026, 11, 20)


def test_measured_curve_after_seven_days():
    conn = connect(":memory:")
    for day in days_before(TODAY, 7):
        add_turn(conn, day, 10)
        add_turn(conn, day, 14)
    c = curve(conn)
    assert c.share(T0) == (0.5, "measured")
    assert curve(conn).share(pt(2026, 11, 20, 10, 30)) == (0.25, "measured")
    assert curve(conn).share(pt(2026, 11, 20, 15, 0)) == (1.0, "measured")


def test_six_days_are_not_enough_and_old_days_dont_count():
    conn = connect(":memory:")
    for day in days_before(TODAY, 6):
        add_turn(conn, day, 10)
    add_turn(conn, (TODAY - dt.timedelta(days=15)).isoformat(), 10)  # outside the 14 days
    assert curve(conn).share(T0) == (0.5, "flat")


def test_follow_ups_retries_and_today_are_left_out():
    conn = connect(":memory:")
    for day in days_before(TODAY, 7):
        add_turn(conn, day, 10)
        add_turn(conn, day, 14)
        for reason in ("pinned", "handoff", "unknown"):
            add_turn(conn, day, 20, reason=reason)
        add_turn(conn, day, 20, steps=0)  # a retried start that nothing served
        add_turn(conn, day, 9, reason="pace-upgrade")
        add_turn(conn, day, 9, reason="spill")
    add_turn(conn, TODAY.isoformat(), 20)
    # 9:00 ×14, 10:00 ×7, 14:00 ×7: before noon is 21 of 28.
    assert curve(conn).share(T0) == (0.75, "measured")


def test_working_hours_map_onto_the_pt_day():
    conn = connect(":memory:")
    # 17:00-01:00 UTC is 09:00-17:00 PST on 2026-11-20 (and wraps past UTC midnight).
    c = curve(conn, working_hours="17:00-01:00", timezone="UTC")
    assert c.share(pt(2026, 11, 20, 8, 0)) == (0.0, "working_hours")
    assert curve(conn, working_hours="17:00-01:00", timezone="UTC").share(T0) == (0.375, "working_hours")
    assert curve(conn, working_hours="17:00-01:00", timezone="UTC").share(
        pt(2026, 11, 20, 18, 0)) == (1.0, "working_hours")


def test_measured_curve_replaces_working_hours():
    conn = connect(":memory:")
    for day in days_before(TODAY, 7):
        add_turn(conn, day, 10)
        add_turn(conn, day, 14)
    assert curve(conn, working_hours="17:00-01:00", timezone="UTC").share(T0) == (0.5, "measured")


@pytest.mark.parametrize("restart", [False, True])
def test_share_never_goes_down_on_the_fall_back_day(restart):
    # Found in review: a restart in the repeated hour lost an in-memory high-water mark, and the
    # share fell from 45.8% to 29.2%. The share now depends only on the time.
    conn = connect(":memory:")
    fall_back = dt.date(2026, 11, 1)  # DST ends at 02:00 PDT
    for day in days_before(fall_back, 7):
        for hour in (0, 1, 2, 3):
            add_turn(conn, day, hour)
    c = curve(conn)
    first = dt.datetime(2026, 11, 1, 1, 50, tzinfo=PT).timestamp()  # 01:50 PDT
    times = [first, first + 20 * 60, first + 60 * 60, first + 80 * 60]  # 01:10 PST, 01:50 PST, 02:10 PST
    shares = []
    for t in times:
        if restart:
            c = curve(conn)
        shares.append(c.share(t)[0])
    assert shares == sorted(shares) and shares[1] == shares[2] == 0.5
    assert shares[0] == pytest.approx(0.25 + 0.25 * 50 / 60)


def test_new_rows_are_read_at_most_once_per_hour():
    conn = connect(":memory:")
    for day in days_before(TODAY, 6):
        add_turn(conn, day, 10)
    c = curve(conn)
    assert c.share(T0)[1] == "flat"
    add_turn(conn, days_before(TODAY, 7)[-1], 10)  # the seventh day
    assert c.share(T0 + 60)[1] == "flat"
    assert c.share(T0 + 3600)[1] == "measured"


def test_failed_curve_read_keeps_the_last_curve_and_is_counted():
    real = connect(":memory:")
    for day in days_before(TODAY, 7):
        add_turn(real, day, 10)
        add_turn(real, day, 14)
    conn = FailingConn(real)
    errors = []
    c = DemandCurve(conn, auto_config().auto, on_error=lambda: errors.append(1))
    assert c.share(T0)[1] == "measured"
    conn.fail_on = "FROM turns"
    assert c.share(T0 + 3600) == (0.5, "measured")  # 13:00: still the measured curve
    assert errors == [1]


def test_failed_first_curve_read_uses_the_cold_start_source():
    conn = FailingConn(connect(":memory:"), fail_on="FROM turns")
    errors = []
    c = DemandCurve(conn, auto_config().auto, on_error=lambda: errors.append(1))
    assert c.share(T0) == (0.5, "flat") and errors == [1]


def test_share_is_not_held_across_a_change_of_source():
    # Found in review: the first read fails (flat, 0.5 at noon), the next hour's succeeds. The
    # measured share (all demand at 14:00 and 15:00, so 0 at 13:00) must not be held at 0.54.
    real = connect(":memory:")
    for day in days_before(TODAY, 7):
        add_turn(real, day, 14)
        add_turn(real, day, 15)
    conn = FailingConn(real, fail_on="FROM turns", times=1)
    c = DemandCurve(conn, auto_config().auto)
    assert c.share(T0) == (0.5, "flat")
    assert c.share(T0 + 3600) == (0.0, "measured")


def test_router_counts_a_failed_curve_read():
    e = auto_env()
    e.router._pacer._curve._conn = FailingConn(e.conn, fail_on="FROM turns")
    e.route(chat("gemini-auto"), ScriptedUpstream())
    assert e.router.state_errors == 1


# ---- migration 3→4 (DESIGN §16.9.5) ----------------------------------------

def test_migration_from_version_3_keeps_turns_and_adds_the_tier(tmp_path):
    path = tmp_path / "q.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for v in (1, 2):
        for statement in MIGRATIONS[v]:
            c.execute(statement)
    c.execute("PRAGMA user_version = 3")
    c.execute("INSERT INTO turns VALUES ('t1', '2026-11-19', 9, 'gemini-smart', 'm', 'm', 'first', "
              "0, 1, 2, 1, 0, 0, 200)")
    c.commit()
    c.close()
    conn = connect(path)
    assert SCHEMA_VERSION == 4 and conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute("SELECT turn_id, reason, steps, tier FROM turns").fetchall() == [
        ("t1", "first", 1, None)]


# ---- HTTP layer (DESIGN §16.9.5) -------------------------------------------

def auto_proxy(**top):
    return make_proxy(MODELS, ROUTES, auto=AUTO, **top)


def test_headers_turn_row_status_and_metrics_on_a_served_turn():
    p = auto_proxy()
    with mocked(Upstream()), running(p) as c:
        r = c.post("/v1/chat/completions", json=chat_body("gemini-auto"), headers=AUTH)
        plain = c.post("/v1/chat/completions", json=chat_body("gemini-fast"), headers=AUTH)
        # An orphaned follow-up makes an `unknown` row: not a turn start (found in review).
        orphan = c.post("/v1/chat/completions", json=follow_up("never-seen"), headers=AUTH)
        status = c.get("/status", headers=AUTH).json()
        metrics = c.get("/metrics", headers=AUTH).text
        models = [m["id"] for m in c.get("/v1/models", headers=AUTH).json()["data"]]
        row = p.conn.execute("SELECT reason, tier, first_model FROM turns WHERE route = 'gemini-auto' "
                             "ORDER BY rowid").fetchone()
    assert r.status_code == 200
    assert (r.headers["x-route-tier"], r.headers["x-route-reason"], r.headers["x-served-model"]) == (
        "smart", "pace-upgrade", "smart-1")
    assert "x-route-tier" not in plain.headers and "x-route-reason" not in plain.headers
    assert row == ("pace-upgrade", "smart", "smart-1")
    assert "gemini-auto" in models
    assert status["routes"]["gemini-auto"] == ["s1", "s2", "f1", "f2"]
    assert status["auto"]["route"] == "gemini-auto"
    assert orphan.headers["x-turn-affinity"] == "unknown"
    assert status["auto"]["pace"] == {"tier": "smart", "capacity": 40, "left": 38, "share": 0.5,
                                      "source": "flat", "upgrade_above": 22.5, "upgrade_now": True}
    assert status["auto"]["turn_starts_today_by_reason"] == {"pace-upgrade": 1}
    assert 'qr_route_decisions_total{reason="pace-upgrade",tier="smart"} 2' in metrics
    assert 'qr_pace_left{tier="smart"} 38' in metrics
    assert 'qr_pace_upgrade_above{tier="smart"} 22.5' in metrics


def test_headers_on_429_and_503():
    every = ("smart-1", "smart-2", "fast-1", "fast-2")
    p = auto_proxy(max_attempts=4)
    with mocked(Upstream({m: [rate_limited("day")] for m in every})), running(p) as c:
        r429 = c.post("/v1/chat/completions", json=chat_body("gemini-auto"), headers=AUTH)
    p = auto_proxy(max_attempts=4)
    with mocked(Upstream({m: [unavailable()] for m in every})), running(p) as c:
        r503 = c.post("/v1/chat/completions", json=chat_body("gemini-auto"), headers=AUTH)
    assert r429.status_code == 429 and r503.status_code == 503
    for r in (r429, r503):
        assert (r.headers["x-route-tier"], r.headers["x-route-reason"]) == ("smart", "pace-upgrade")


def test_status_and_metrics_without_auto():
    p = make_proxy(MODELS, ROUTES)
    with running(p) as c:
        status = c.get("/status", headers=AUTH).json()
        metrics = c.get("/metrics", headers=AUTH).text
    assert status["auto"] is None
    assert "qr_pace_left{" not in metrics
