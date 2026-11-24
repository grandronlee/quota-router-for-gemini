"""gemini-auto rules (DESIGN §16.10.2–16.10.5)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from apphelpers import AUTH, Upstream, captured_logs, make_proxy, mocked, running
from helpers import Env, FakeClock, ScriptedUpstream, T0, chat, make_config, pt
from quota_router.classify import Classification
from quota_router.config import ConfigError
from quota_router.db import MIGRATIONS, SCHEMA, SCHEMA_VERSION, connect
from quota_router.router import (SYSTEM_TEXT_LIMIT, RateLimited, RouteServed, estimate_input_tokens,
                                 system_text)
from quota_router.turns import TurnLog, TurnRef
from test_auto import AUTO, MODELS, ROUTES, TOOLS, use

LEADER = "You are Zoo, an experienced technical leader"
ORCH = "You are Zoo, a strategic workflow orchestrator"
# The live config's rules (probe of 2026-11-23), in its order.
RULES = [
    {"name": "task-title", "when": {"max_tools": 0, "max_input_tokens": 500}, "tier": "fast"},
    {"name": "planning-mode", "when": {"system_line_prefix": [LEADER, ORCH]}, "tier": "smart"},
]
# At T0 (PT noon, flat share 0.5): capacity 40, reserve 5, so the upgrade line is 22.5 and the
# rule line max(0, 40 × 0.5 − 5) = 15.


def rules_env(rules=None, start: float = T0, **top) -> Env:
    return Env(make_config(MODELS, ROUTES, auto={**AUTO, "rules": RULES if rules is None else rules},
                           **top), start=start)


def planning(system: str = LEADER + " who plans.\nMore text.", **extra) -> dict:
    return chat("gemini-auto", messages=[{"role": "system", "content": system},
                                         {"role": "user", "content": "plan it"}],
                **{"tools": TOOLS, **extra})


def one(when: dict, tier: str = "smart") -> list[dict]:
    return [{"name": "r", "when": when, "tier": tier}]


def decided(e: Env, request: dict) -> tuple:
    out = e.route(request, ScriptedUpstream())
    return out.route_tier, out.route_reason, out.route_rule


# ---- conditions --------------------------------------------------------------

@pytest.mark.parametrize("system, matched", [
    (LEADER + " who plans.", True),                       # at the text's start
    ("Intro line.\n" + LEADER + ".", True),               # after a newline
    ("Intro. " + LEADER + ".", False),                    # mid-line
    ("you are zoo, an experienced technical leader", False),  # case matters
    (ORCH + ".", True),                                   # any of the listed prefixes
])
def test_system_line_prefix_matches_only_at_a_line_start(system, matched):
    e = rules_env(one({"system_line_prefix": [LEADER, ORCH]}))
    assert decided(e, planning(system))[2] == ("r" if matched else None)


def test_prefix_in_list_parts_and_developer_messages():
    e = rules_env(one({"system_line_prefix": [LEADER]}))
    # Split across a message's text parts: parts join as they are (as the probe read them).
    parts = [{"type": "text", "text": "You are Zoo, "}, {"type": "image_url", "image_url": {"url": "x"}},
             {"type": "text", "text": "an experienced technical leader."}]
    req = chat("gemini-auto", messages=[{"role": "system", "content": parts},
                                        {"role": "user", "content": "go"}], tools=TOOLS)
    assert decided(e, req)[2] == "r"
    dev = chat("gemini-auto", messages=[{"role": "developer", "content": LEADER},
                                        {"role": "user", "content": "go"}], tools=TOOLS)
    assert decided(e, dev)[2] == "r"
    # A second system message starts on a new line.
    two = chat("gemini-auto", messages=[{"role": "system", "content": "first"},
                                        {"role": "system", "content": LEADER},
                                        {"role": "user", "content": "go"}], tools=TOOLS)
    assert decided(e, two)[2] == "r"
    # User and assistant text is never searched.
    user = chat("gemini-auto", messages=[{"role": "user", "content": LEADER}], tools=TOOLS)
    assert decided(e, user)[2] is None


def test_text_past_the_limit_is_not_searched():
    e = rules_env(one({"system_line_prefix": [LEADER]}))
    pad = "x" * (SYSTEM_TEXT_LIMIT - 1)
    inside = pad[:-len(LEADER) - 1] + "\n" + LEADER + "\n" + "y" * 100
    straddles = pad[:-10] + "\n" + LEADER
    beyond = pad + "\n" + LEADER
    assert decided(e, planning(inside))[2] == "r"
    assert decided(e, planning(straddles))[2] is None
    assert decided(e, planning(beyond))[2] is None


def test_system_text_is_bounded_and_never_raises():
    huge = {"messages": [{"role": "system", "content": [{"type": "text", "text": "a" * 100_000}] * 5}]}
    assert len(system_text(huge)) == SYSTEM_TEXT_LIMIT
    for bad in ({"messages": None}, {"messages": [None, 3, "s"]},
                {"messages": [{"role": "system", "content": {"text": LEADER}}]},
                {"messages": [{"role": "system", "content": [{"type": "text", "text": 42}, None]}]}):
        assert system_text(bad) == ""
    # A malformed sibling adds nothing; a valid system message still counts.
    mixed = {"messages": [{"role": "system", "content": 7}, {"role": "system", "content": LEADER}]}
    assert system_text(mixed) == LEADER


@pytest.mark.parametrize("tools, matched", [(0, False), (1, True), (2, True), (3, False)])
def test_tool_bounds_at_their_edges(tools, matched):
    e = rules_env(one({"min_tools": 1, "max_tools": 2}))
    req = chat("gemini-auto", tools=TOOLS * tools) if tools else chat("gemini-auto")
    assert decided(e, req)[2] == ("r" if matched else None)


def test_tools_that_are_not_a_list_match_no_tool_condition():
    e = rules_env(one({"max_tools": 0}, tier="fast"))
    assert decided(e, chat("gemini-auto", tools="oops"))[2] is None
    assert decided(e, chat("gemini-auto", tools=None))[2] == "r"  # null: none offered
    assert decided(e, chat("gemini-auto"))[2] == "r"


def test_input_token_bounds_at_their_edges():
    req = chat("gemini-auto", tools=TOOLS)
    n = estimate_input_tokens(req)
    for lo, hi, matched in ((n, n, True), (None, n - 1, False), (n + 1, None, False), (0, None, True)):
        when = {k: v for k, v in (("min_input_tokens", lo), ("max_input_tokens", hi)) if v is not None}
        assert decided(rules_env(one(when)), req)[2] == ("r" if matched else None), (lo, hi)


def test_reasoning_effort():
    e = rules_env(one({"reasoning_effort": ["high", "medium"]}))
    assert decided(e, chat("gemini-auto", tools=TOOLS, reasoning_effort="high"))[2] == "r"
    assert decided(e, chat("gemini-auto", tools=TOOLS, reasoning_effort="low"))[2] is None
    assert decided(e, chat("gemini-auto", tools=TOOLS, reasoning_effort=["high"]))[2] is None
    assert decided(e, chat("gemini-auto", tools=TOOLS))[2] is None


def test_all_conditions_must_hold():
    e = rules_env(one({"system_line_prefix": [LEADER], "min_tools": 2}))
    assert decided(e, planning())[2] is None  # one tool
    assert decided(e, planning(tools=TOOLS * 2))[2] == "r"
    assert decided(e, planning(system="Other.", tools=TOOLS * 2))[2] is None


# ---- first match, and step 2 without one -------------------------------------

def test_first_match_wins_and_no_match_is_step_2():
    rules = [{"name": "a", "when": {"min_tools": 1}, "tier": "fast"},
             {"name": "b", "when": {"min_tools": 1}, "tier": "smart"}]
    e = rules_env(rules)
    assert decided(e, chat("gemini-auto", tools=TOOLS)) == ("fast", "a", "a")
    assert decided(e, chat("gemini-auto")) == ("smart", "pace-upgrade", None)


def test_live_rules_on_zoo_shaped_requests():
    e = rules_env()
    assert decided(e, chat("gemini-auto", messages=[{"role": "user", "content": "Title this."}])) == (
        "fast", "task-title", "task-title")
    assert decided(e, planning()) == ("smart", "planning-mode", "planning-mode")
    code = planning(system="You are Zoo, a highly skilled software engineer.")
    assert decided(e, code) == ("smart", "pace-upgrade", None)


# ---- the rule line (DESIGN §16.10.2) -------------------------------------------

@pytest.mark.parametrize("at, used, on_smart", [
    (pt(2026, 11, 20, 0, 0, 0), 4, True),        # share 0: line 35, left 36
    (pt(2026, 11, 20, 0, 0, 0), 5, False),       # left 35
    (T0, 24, True),                              # mid-day: line 15, left 16
    (T0, 25, False),                             # left 15
    (pt(2026, 11, 20, 23, 59, 59), 39, True),    # share ~1: line 0, left 1
    (pt(2026, 11, 20, 23, 59, 59), 40, False),   # nothing left
])
def test_paced_tier_rule_line(at, used, on_smart):
    e = rules_env(start=at)
    use(e, "s1", min(used, 20))
    use(e, "s2", max(0, used - 20))
    up = ScriptedUpstream()
    out = e.route(planning(), up)
    assert isinstance(out, RouteServed)
    if on_smart:
        assert (out.route_tier, out.route_reason, out.route_rule) == ("smart", "planning-mode", "planning-mode")
        assert (out.turn.reason, out.turn.tier, out.turn.rule) == ("planning-mode", "smart", "planning-mode")
    else:
        assert (up.called, out.route_tier, out.route_reason, out.route_rule) == (
            ["fast-1"], "fast", "paced", "planning-mode")
        assert (out.turn.reason, out.turn.tier, out.turn.rule) == ("paced", "fast", "planning-mode")


def test_rule_turn_gets_smart_where_an_unmatched_turn_is_not_upgraded():
    e = rules_env()
    use(e, "s1", 20)  # left 20: below the upgrade line (22.5), above the rule line (15)
    assert decided(e, chat("gemini-auto", tools=TOOLS))[1] == "default"
    assert decided(e, planning())[1] == "planning-mode"


def test_rule_line_view():
    e = rules_env()
    view = e.router.pace_view()
    assert (view.upgrade_above, view.rule_above, view.rule_allowed) == (22.5, 15, True)
    assert view.status()["rule_above"] == 15


def test_non_paced_rule_is_never_upgraded():
    e = rules_env()  # full pool at noon: step 2 would upgrade
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto"), up)
    assert (up.called, out.route_tier, out.route_reason, out.route_rule) == (
        ["fast-1"], "fast", "task-title", "task-title")


def test_failed_pace_read_makes_a_rule_turn_paced(monkeypatch):
    e = rules_env()

    def broken(models):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(e.store, "pool", broken)
    up = ScriptedUpstream()
    out = e.route(planning(), up)
    assert (up.called, out.route_tier, out.route_reason, out.route_rule) == (
        ["fast-1"], "fast", "paced", "planning-mode")
    assert e.router.state_errors == 1


def test_non_paced_rule_does_not_read_the_pace(monkeypatch):
    e = rules_env()
    monkeypatch.setattr(e.store, "pool", lambda models: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    assert decided(e, chat("gemini-auto")) == ("fast", "task-title", "task-title")
    assert e.router.state_errors == 0


# ---- spill -----------------------------------------------------------------------

def test_rule_turn_served_outside_its_tier_is_a_spill_and_keeps_its_rule():
    e = rules_env()
    up = ScriptedUpstream({"smart-1": RateLimited({}), "smart-2": RateLimited({})})
    out = e.route(planning(), up)
    assert up.called == ["smart-1", "smart-2", "fast-1"]
    assert (out.route_tier, out.route_reason, out.route_rule) == ("smart", "spill", "planning-mode")
    assert (out.turn.reason, out.turn.tier, out.turn.rule) == ("spill", "smart", "planning-mode")


def test_non_paced_rule_blocks_the_upgrade_not_spill():
    # A fast rule starts the turn on fast; with no fast model left it still spills into smart
    # (§16.9.4), so it doesn't keep the title request off the smart pool.
    e = rules_env()
    for k in ("f1", "f2"):
        e.store.on_rate_limited(e.model(k), Classification("rpd", None))
    up = ScriptedUpstream()
    out = e.route(chat("gemini-auto"), up)
    assert (up.called, out.route_tier, out.route_reason, out.route_rule) == (
        ["smart-1"], "fast", "spill", "task-title")


def test_paced_turn_spills_into_smart_when_fast_is_out():
    e = rules_env(start=pt(2026, 11, 20, 0, 0, 0))
    use(e, "s1", 5)  # paced at midnight
    for k in ("f1", "f2"):
        e.store.on_rate_limited(e.model(k), Classification("rpd", None))
    up = ScriptedUpstream()
    out = e.route(planning(), up)
    assert (up.called, out.route_tier, out.route_reason, out.route_rule) == (
        ["smart-1"], "fast", "spill", "planning-mode")
    assert (out.turn.reason, out.turn.rule) == ("spill", "planning-mode")


# ---- follow-ups --------------------------------------------------------------------

def with_call(request: dict, call_id: str) -> dict:
    msgs = [*request["messages"],
            {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
                                                  "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": "done"}]
    return {**request, "messages": msgs}


def test_pinned_follow_up_ignores_rules():
    # A title-shaped turn on fast; its follow-up matches planning-mode now, but stays pinned.
    e = rules_env([{"name": "fast-first", "when": {"min_tools": 1}, "tier": "fast"},
                   *RULES[1:]])
    up = ScriptedUpstream()
    start = e.route(chat("gemini-auto", tools=TOOLS), up)
    assert (start.model.key, start.route_rule) == ("f1", "fast-first")
    e.router.complete(start, 10, ["c1"])
    out = e.route(with_call(planning(tools=[]), "c1"), up)
    assert (out.model.key, out.affinity, out.route_tier, out.route_reason, out.route_rule) == (
        "f1", "pinned", "fast", "pinned", None)


def test_unknown_follow_up_evaluates_rules():
    e = rules_env()
    out = e.route(with_call(planning(), "never-seen"), ScriptedUpstream())
    assert (out.affinity, out.route_tier, out.route_reason, out.route_rule) == (
        "unknown", "smart", "planning-mode", "planning-mode")
    assert (out.turn.reason, out.turn.tier, out.turn.rule) == ("unknown", "smart", "planning-mode")


def test_other_routes_get_no_rule():
    e = rules_env()
    for name in ("gemini-fast", "gemini-smart", "smart-1"):
        out = e.route({**planning(), "model": name}, ScriptedUpstream())
        assert (out.route_rule, out.turn.rule) == (None, None), name


# ---- validation ----------------------------------------------------------------------

@pytest.mark.parametrize("rules, message", [
    ([{"name": "default", "when": {"max_tools": 0}, "tier": "fast"}], "reserved"),
    ([{"name": "paced", "when": {"max_tools": 0}, "tier": "fast"}], "reserved"),
    ([{"name": "handback", "when": {"max_tools": 0}, "tier": "fast"}], "reserved"),
    ([{"name": "a b", "when": {"max_tools": 0}, "tier": "fast"}], "x-route-reason"),
    ([{"name": "", "when": {"max_tools": 0}, "tier": "fast"}], "x-route-reason"),
    ([{"name": "a", "when": {"max_tools": 0}, "tier": "fast"},
      {"name": "a", "when": {"min_tools": 1}, "tier": "smart"}], "two rules"),
    ([{"name": "a", "when": {"max_tools": 0}, "tier": "medium"}], "not a tier"),
    ([{"name": "a", "when": {}, "tier": "fast"}], "at least one condition"),
    ([{"name": "a", "when": {"max_tools": None}, "tier": "fast"}], "at least one condition"),
    ([{"name": "a", "when": {"system_matches": "x"}, "tier": "fast"}], "system_matches"),
    ([{"name": "a", "when": {"system_line_prefix": []}, "tier": "fast"}], "at least one prefix"),
    ([{"name": "a", "when": {"system_line_prefix": [""]}, "tier": "fast"}], "non-empty"),
    ([{"name": "a", "when": {"system_line_prefix": ["a\nb"]}, "tier": "fast"}], "without a newline"),
    ([{"name": "a", "when": {"system_line_prefix": "You are"}, "tier": "fast"}], "list"),
    ([{"name": "a", "when": {"min_tools": 3, "max_tools": 2}, "tier": "fast"}], "above max_tools"),
    ([{"name": "a", "when": {"min_input_tokens": 9, "max_input_tokens": 8}, "tier": "fast"}],
     "above max_input_tokens"),
    ([{"name": "a", "when": {"max_tools": -1}, "tier": "fast"}], "greater than or equal"),
    ([{"name": "a", "when": {"max_tools": True}, "tier": "fast"}], "integer"),
    ([{"name": "a", "when": {"max_tools": 1.5}, "tier": "fast"}], "integer"),
    ([{"name": "a", "when": {"reasoning_effort": []}, "tier": "fast"}], "at least one value"),
    ([{"name": "a", "when": {"reasoning_effort": [1]}, "tier": "fast"}], "string"),
    ([{"name": "a", "when": {"max_tools": 0}}], "tier"),
])
def test_invalid_rules_are_rejected(rules, message):
    with pytest.raises(ConfigError, match=message):
        make_config(MODELS, ROUTES, auto={**AUTO, "rules": rules})


def test_valid_rules_load_and_no_rules_is_step_2():
    cfg = make_config(MODELS, ROUTES, auto={**AUTO, "rules": RULES})
    assert [r.name for r in cfg.auto.rules] == ["task-title", "planning-mode"]
    assert cfg.auto.rules[0].when.max_tools == 0  # 0 counts as a condition
    assert make_config(MODELS, ROUTES, auto=AUTO).auto.rules == []


# ---- migration 4→5 ----------------------------------------------------------------------

def test_migration_from_version_4_keeps_turns_and_adds_the_rule(tmp_path):
    path = tmp_path / "v4.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for v in (1, 2, 3):
        for statement in MIGRATIONS[v]:
            c.execute(statement)
    c.execute("PRAGMA user_version = 4")
    c.execute("INSERT INTO turns VALUES ('t1', '2026-11-20', 9, 'gemini-auto', 'm', 'm', 'pace-upgrade', "
              "2, 10, 20, 1, 0, 0, 200, 'smart')")
    c.commit()
    c.close()
    conn = connect(path)
    assert SCHEMA_VERSION == 5 and conn.execute("PRAGMA user_version").fetchone()[0] == 5
    assert conn.execute("SELECT reason, steps, tier, rule FROM turns").fetchall() == [
        ("pace-upgrade", 1, "smart", None)]
    # A follow-up of that old turn adds to its counters and leaves its start's columns alone.
    log = TurnLog(conn, FakeClock(T0))
    log.note(TurnRef("t1", "pinned", 0, T0, "fast", "planning-mode"), route="gemini-auto", status=200,
             served_model="m2", prompt_tokens=5)
    assert conn.execute("SELECT reason, steps, tier, rule, last_model FROM turns").fetchall() == [
        ("pace-upgrade", 2, "smart", None, "m2")]
    conn.close()
    assert connect(path).execute("PRAGMA user_version").fetchone()[0] == 5  # reopens cleanly


# ---- HTTP layer (DESIGN §16.10.3) ------------------------------------------------------

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


def test_headers_log_row_status_and_metrics_with_rules():
    p = make_proxy(MODELS, ROUTES, auto={**AUTO, "rules": RULES})
    with mocked(Upstream()), running(p) as c, captured_logs() as logs:
        post = lambda body: c.post("/v1/chat/completions", json=body, headers=AUTH)  # noqa: E731
        plan = post(planning())
        title = post(chat("gemini-auto", messages=[{"role": "user", "content": "Title"}]))
        plain = post(chat("gemini-auto", tools=TOOLS))
        other = post({**planning(), "model": "gemini-fast"})
        status = c.get("/status", headers=AUTH).json()
        metrics = c.get("/metrics", headers=AUTH).text
        rows = p.conn.execute("SELECT reason, tier, rule FROM turns WHERE route = 'gemini-auto' "
                              "ORDER BY rowid").fetchall()
    assert [r.status_code for r in (plan, title, plain, other)] == [200] * 4
    assert (plan.headers["x-route-tier"], plan.headers["x-route-reason"]) == ("smart", "planning-mode")
    assert (title.headers["x-route-tier"], title.headers["x-route-reason"]) == ("fast", "task-title")
    assert plain.headers["x-route-reason"] == "pace-upgrade"
    assert rows == [("planning-mode", "smart", "planning-mode"), ("task-title", "fast", "task-title"),
                    ("pace-upgrade", "smart", None)]
    lines = request_lines(logs)
    assert [ln.get("route_rule") for ln in lines] == ["planning-mode", "task-title", None, None]
    assert "route_rule" not in lines[2] and "route_rule" not in lines[3]
    assert status["auto"]["pace"]["rule_above"] == 15
    assert status["auto"]["turn_starts_today_by_reason"] == {
        "pace-upgrade": 1, "planning-mode": 1, "task-title": 1}
    assert 'qr_route_decisions_total{reason="planning-mode",tier="smart"} 1' in metrics
    assert 'qr_route_decisions_total{reason="task-title",tier="fast"} 1' in metrics
    assert "# HELP qr_pace_rule_above " in metrics
    assert 'qr_pace_rule_above{tier="smart"} 15' in metrics


def test_paced_label_is_counted_in_metrics():
    p = make_proxy(MODELS, ROUTES, auto={**AUTO, "rules": RULES})
    for key, n in (("s1", 20), ("s2", 5)):  # left 15: at the rule line
        for _ in range(n):
            p.store.try_reserve(p.config.models[key], 1)
    with mocked(Upstream()), running(p) as c:
        r = c.post("/v1/chat/completions", json=planning(), headers=AUTH)
        metrics = c.get("/metrics", headers=AUTH).text
        status = c.get("/status", headers=AUTH).json()
    assert (r.headers["x-route-tier"], r.headers["x-route-reason"]) == ("fast", "paced")
    assert 'qr_route_decisions_total{reason="paced",tier="fast"} 1' in metrics
    assert status["auto"]["turn_starts_today_by_reason"] == {"paced": 1}
