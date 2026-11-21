"""Schema migration 1→2 and the decision log (DESIGN §16.8 1d)."""

from __future__ import annotations

import sqlite3

import pytest

from apphelpers import (AUTH, Upstream, chat_body, chunk, make_proxy, mocked, ok, rate_limited, running,
                        sse, streamed, unavailable)
from helpers import T0, FailingConn, pt
from quota_router import db as dbmod
from quota_router.clock import FakeClock
from quota_router.db import SCHEMA, StateDBError, connect

COLS = ("turn_id, day, hour, route, first_model, last_model, reason, tools, input_tokens, "
        "output_tokens, steps, errors, handoffs, last_status")
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}] * 3


def rows(p) -> list[dict]:
    cur = p.conn.execute(f"SELECT {COLS} FROM turns ORDER BY rowid")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def follow_up(*ids: str, model: str = "fast", **extra) -> dict:
    body = chat_body(model, **extra)
    for i in ids:
        body["messages"] += [{"role": "assistant", "tool_calls": [{"id": i, "type": "function",
                              "function": {"name": "f", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": i, "content": "done"},
                             {"role": "user", "content": "<environment_details>"}]  # Zoo Code
    return body


def post(c, body):
    return c.post("/v1/chat/completions", json=body, headers=AUTH)


def version_1_file(path) -> None:
    """A database as the version-1 proxy left it, with open tool turns."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO rpd VALUES ('model-a', ?, 4)", ("2026-11-20",))
    c.executemany("INSERT INTO affinity VALUES (?, ?, ?)",
                  [("old-1", "model-a", T0 - 60), ("old-2", "model-b", T0 - 30)])
    c.commit()
    c.close()


# ---- migration 1→2 -----------------------------------------------------------

def test_migration_from_version_1_keeps_affinity_and_adds_the_turn_log(tmp_path):
    path = tmp_path / "q.sqlite3"
    version_1_file(path)
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT tool_call_id, model_id, turn_id FROM affinity ORDER BY tool_call_id"
                        ).fetchall() == [("old-1", "model-a", None), ("old-2", "model-b", None)]
    assert conn.execute("SELECT count FROM rpd").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    conn.close()
    again = connect(path)  # a second start runs nothing
    assert again.execute("PRAGMA user_version").fetchone()[0] == 2
    again.close()


def test_pre_migration_tool_turn_stays_pinned_and_starts_a_row(tmp_path):
    path = tmp_path / "q.sqlite3"
    version_1_file(path)
    up = Upstream({"model-b": [ok(tool_ids=["new-1"]), ok()]})
    p = make_proxy(db=str(path))
    with mocked(up), running(p) as c:
        r = post(c, follow_up("old-2"))
        r2 = post(c, follow_up("old-2", "new-1"))
        got = rows(p)
    assert r.headers["x-served-model"] == r2.headers["x-served-model"] == "model-b"
    assert r.headers["x-turn-affinity"] == "pinned"
    [row] = got  # the second request found the row through new-1's turn_id
    assert row["reason"] == "pinned" and row["steps"] == 2 and row["first_model"] == "model-b"


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    path = tmp_path / "q.sqlite3"
    version_1_file(path)
    monkeypatch.setitem(dbmod.MIGRATIONS, 1, [*dbmod.MIGRATIONS[1], "THIS IS NOT SQL"])
    with pytest.raises(StateDBError):
        connect(path)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert [r[1] for r in c.execute("PRAGMA table_info(affinity)")] == ["tool_call_id", "model_id", "created_at"]
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'turns'").fetchall() == []
    assert c.execute("SELECT COUNT(*) FROM affinity").fetchone()[0] == 2
    c.close()


# ---- turn rows ---------------------------------------------------------------

def test_new_and_continued_turn_share_one_row():
    up = Upstream({"model-a": [ok(tool_ids=["c1"], prompt=100, completion_tokens=10),
                               ok(tool_ids=["c2"], prompt=150, completion_tokens=20),
                               ok("done", prompt=200, completion_tokens=5)]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(tools=TOOLS))
        post(c, follow_up("c1", tools=TOOLS[:1]))
        post(c, follow_up("c1", "c2"))
        status = c.get("/status", headers=AUTH).json()
        got = rows(p)
        turn_ids = {r[0] for r in p.conn.execute("SELECT turn_id FROM affinity")}
    [row] = got
    assert len(row["turn_id"]) == 32 and int(row["turn_id"], 16) >= 0
    assert row == {**row, "day": "2026-11-20", "hour": 12, "route": "fast", "first_model": "model-a",
                   "last_model": "model-a", "reason": "first", "tools": 3, "input_tokens": 450,
                   "output_tokens": 35, "steps": 3, "errors": 0, "handoffs": 0, "last_status": 200}
    assert status["turns_today"] == {"fast": 1}
    assert turn_ids == {row["turn_id"]}


def test_a_new_user_message_starts_a_new_row():
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), ok()]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        body = follow_up("c1")
        body["messages"].append({"role": "assistant", "content": "ok"})
        body["messages"].append({"role": "user", "content": "next task"})
        post(c, body)
        got = rows(p)
    assert [r["reason"] for r in got] == ["first", "first"]


def test_unknown_follow_up_starts_its_own_row():
    p = make_proxy()
    with mocked(Upstream()), running(p) as c:
        r = post(c, follow_up("never-seen"))
        got = rows(p)
    assert r.headers["x-turn-affinity"] == "unknown"
    [row] = got
    assert row["reason"] == "unknown" and row["steps"] == 1


def test_retried_turn_start_makes_a_second_row_and_the_failed_one_has_no_steps():
    up = Upstream({"model-a": [unavailable(), ok()], "model-b": [unavailable()]})
    p = make_proxy(max_attempts=2)
    with mocked(up), running(p) as c:
        r1 = post(c, chat_body())
        p.clock.advance(120)  # past the 5xx cooldowns
        r2 = post(c, chat_body())
        got = rows(p)
    assert r1.status_code == 503 and r2.status_code == 200
    failed, retried = got
    assert failed["turn_id"] != retried["turn_id"]
    assert (failed["steps"], failed["errors"], failed["last_status"], failed["first_model"]) == (0, 1, 503, None)
    assert (retried["steps"], retried["errors"], retried["reason"]) == (1, 0, "first")


def test_turn_start_served_by_a_later_model_is_a_spill():
    up = Upstream({"model-a": [rate_limited("minute")]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        got = rows(p)
    [row] = got
    assert (row["reason"], row["first_model"]) == ("spill", "model-b")


def test_handoff_and_errors_inside_a_turn_are_counted():
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), unavailable()],
                   "model-b": [ok(tool_ids=["c2"]), ok()]})
    p = make_proxy(handoff_targets=["b"])
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, follow_up("c1"))
        post(c, follow_up("c1", "c2"))
        got = rows(p)
    assert r.headers["x-turn-affinity"] == "handoff"
    [row] = got
    assert (row["first_model"], row["last_model"], row["reason"]) == ("model-a", "model-b", "first")
    assert (row["steps"], row["errors"], row["handoffs"]) == (3, 0, 1)


def test_failed_request_inside_a_turn_counts_as_an_error():
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), rate_limited("minute")]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, follow_up("c1"))
        got = rows(p)
    assert r.status_code == 429
    [row] = got
    assert (row["steps"], row["errors"], row["last_status"], row["last_model"]) == (1, 1, 429, "model-a")


def test_streamed_turn_is_logged_with_its_usage():
    usage = {"prompt_tokens": 70, "completion_tokens": 9, "total_tokens": 79}
    up = Upstream({"model-a": [streamed(sse(chunk(tool_ids=["s1"]), chunk(usage=usage, finish="stop")))]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True))
        got = rows(p)
        s1_turn = p.conn.execute("SELECT turn_id FROM affinity WHERE tool_call_id = 's1'").fetchone()[0]
    [row] = got
    assert (row["steps"], row["input_tokens"], row["output_tokens"]) == (1, 70, 9)
    assert s1_turn == row["turn_id"]


def test_turn_keeps_its_start_day_across_pt_midnight():
    clock = FakeClock(pt(2026, 11, 20, 23, 59, 0))
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), ok()]})
    p = make_proxy(clock=clock)
    with mocked(up), running(p) as c:
        post(c, chat_body())
        clock.advance(120)
        post(c, follow_up("c1"))
        status = c.get("/status", headers=AUTH).json()
        got = rows(p)
    [row] = got
    assert (row["day"], row["hour"], row["steps"]) == ("2026-11-20", 23, 2)
    assert status["turns_today"] == {}


def test_direct_model_id_is_logged_under_its_id():
    p = make_proxy()
    with mocked(Upstream()), running(p) as c:
        post(c, chat_body("model-b"))
        post(c, chat_body("no-such-model"))  # 404 before routing: no row
        got = rows(p)
    assert [(r["route"], r["reason"]) for r in got] == [("model-b", "first")]


# ---- failures never fail the request -------------------------------------------

def test_failed_turn_write_does_not_fail_the_request():
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), ok()]})
    p = make_proxy()
    real = p.conn
    p.turns._conn = FailingConn(real, fail_on="INSERT INTO turns", times=1)
    with mocked(up), running(p) as c:
        r1 = post(c, chat_body())
        r2 = post(c, follow_up("c1"))
        got = rows(p)
        affinity_turn = real.execute("SELECT turn_id FROM affinity").fetchone()[0]
    assert r1.status_code == r2.status_code == 200
    assert r2.headers["x-turn-affinity"] == "pinned"  # affinity was still recorded
    assert p.router.state_errors == 1
    [row] = got  # the follow-up created the missing row under the same turn
    assert row["reason"] == "pinned" and row["steps"] == 1
    assert affinity_turn == row["turn_id"]


def test_failed_turn_log_read_still_serves_status():
    p = make_proxy()
    p.turns._conn = FailingConn(p.conn, fail_on="FROM turns")
    with mocked(Upstream()), running(p) as c:
        status = c.get("/status", headers=AUTH)
        got = rows(p)
    assert status.status_code == 200 and status.json()["turns_today"] is None
    assert p.router.state_errors >= 1


def test_failed_turn_prune_is_counted_and_retried():
    p = make_proxy()
    p.turns._conn = FailingConn(p.conn, fail_on="DELETE FROM turns", times=1)
    p.prune_once()
    assert p.router.state_errors == 1
    p.prune_once()
    assert p.router.state_errors == 1


# ---- review follow-ups -----------------------------------------------------------

def test_failed_migration_is_retried_at_the_next_start(tmp_path, monkeypatch):
    for name, make in (("v1.sqlite3", version_1_file), ("new.sqlite3", None)):
        path = tmp_path / name
        if make:
            make(path)
        good = list(dbmod.MIGRATIONS[1])
        monkeypatch.setitem(dbmod.MIGRATIONS, 1, [*good, "THIS IS NOT SQL"])
        with pytest.raises(StateDBError):
            connect(path)
        monkeypatch.setitem(dbmod.MIGRATIONS, 1, good)
        conn = connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2, name
        assert "turn_id" in [r[1] for r in conn.execute("PRAGMA table_info(affinity)")], name
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0, name
        conn.close()


def test_phase_1_file_gets_the_version_2_tables(tmp_path):
    path = tmp_path / "p1.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)  # Phase 1 left user_version at 0
    c.execute("INSERT INTO affinity VALUES ('p1', 'model-a', ?)", (T0,))
    c.commit()
    c.close()
    conn = connect(path)
    assert conn.execute("SELECT tool_call_id, turn_id FROM affinity").fetchall() == [("p1", None)]
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    conn.close()


def test_follow_up_continues_the_turn_of_its_latest_call():
    # The second request's lookup fails, so it starts its own row; the third request names
    # both turns and must continue the newer one.
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), ok(tool_ids=["c2"]), ok()]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        p.affinity._conn = FailingConn(p.conn, fail_on="FROM affinity", times=1)
        post(c, follow_up("c1"))
        post(c, follow_up("c1", "c2"))
        got = rows(p)
    first, second = got
    assert (first["reason"], first["steps"]) == ("first", 1)
    assert (second["reason"], second["steps"]) == ("unknown", 2)


def test_handed_off_follow_up_that_creates_its_row_is_a_handoff():
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), unavailable()]})
    p = make_proxy(handoff_targets=["b"])
    p.turns._conn = FailingConn(p.conn, fail_on="INSERT INTO turns", times=1)
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, follow_up("c1"))
        got = rows(p)
    assert r.headers["x-turn-affinity"] == "handoff"
    [row] = got
    assert (row["reason"], row["first_model"], row["handoffs"]) == ("handoff", "model-b", 1)


def test_known_call_of_a_removed_model_keeps_its_turn():
    p = make_proxy()
    with mocked(Upstream()), running(p) as c:
        p.affinity.record(["gone-1"], "model-removed", "ab" * 16)
        r = post(c, follow_up("gone-1"))
        got = rows(p)
    assert r.headers["x-turn-affinity"] == "unknown"
    [row] = got
    assert (row["turn_id"], row["reason"], row["first_model"]) == ("ab" * 16, "pinned", "model-a")


def test_passthrough_4xx_inside_a_turn_counts_as_an_error():
    import httpx
    bad = httpx.Response(400, json={"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
    up = Upstream({"model-a": [ok(tool_ids=["c1"]), bad]})
    p = make_proxy()
    with mocked(up), running(p) as c:
        post(c, chat_body())
        r = post(c, follow_up("c1"))
        got = rows(p)
    assert r.status_code == 400
    [row] = got
    assert (row["steps"], row["errors"], row["last_status"], row["last_model"]) == (1, 1, 400, "model-a")


def _dropped_stream(p, turn):
    """A stream the client drops after its first chunk (as in test_app's disconnect test)."""
    import asyncio
    import json as _json

    from quota_router.app import StreamSession
    from quota_router.router import RouteServed
    from quota_router.upstream import SSEEvent, Stream

    class FakeResponse:
        async def aclose(self):
            pass

    async def events():
        yield SSEEvent(b"data: x\n\n", "x")

    first = SSEEvent(b"", _json.dumps(chunk(tool_ids=["d1"])))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None, turn=turn)
    session = StreamSession(p, served, Stream(FakeResponse(), events(), first),  # type: ignore[arg-type]
                            request_id="r", route="fast", started=0.0, input_estimate=10)

    async def run():
        gen = session.body()
        await gen.__anext__()
        await gen.aclose()
        await session.finish()
        await session.finish()  # idempotent: one row write

    asyncio.run(run())
    return session


def test_dropped_stream_counts_as_a_step_without_usage():
    from quota_router.turns import TurnRef
    p = make_proxy()
    session = _dropped_stream(p, TurnRef("cd" * 16, "first", 0, T0))
    assert session.reason == "client_disconnected"
    [row] = rows(p)
    assert (row["steps"], row["errors"], row["input_tokens"], row["output_tokens"]) == (1, 0, 0, 0)
    assert p.conn.execute("SELECT turn_id FROM affinity WHERE tool_call_id = 'd1'").fetchone()[0] == "cd" * 16


def test_failed_turn_write_at_stream_finish_is_counted_not_raised():
    from quota_router.turns import TurnRef
    p = make_proxy()
    p.turns._conn = FailingConn(p.conn, fail_on="INSERT INTO turns")
    session = _dropped_stream(p, TurnRef("ef" * 16, "first", 0, T0))
    assert session.finished and p.router.state_errors == 1
    assert rows(p) == []


def test_turn_writes_in_either_order_give_the_same_counters():
    from quota_router.turns import TurnRef
    p = make_proxy()
    start = TurnRef("12" * 16, "first", 3, T0)
    follow = TurnRef("12" * 16, "pinned", 1, T0 + 5)
    p.turns.note(follow, route="fast", status=200, served_model="model-a", prompt_tokens=5, output_tokens=1)
    p.turns.note(start, route="fast", status=200, served_model="model-a", prompt_tokens=7, output_tokens=2)
    [row] = rows(p)
    assert (row["steps"], row["input_tokens"], row["output_tokens"]) == (2, 12, 3)
    assert (row["reason"], row["tools"]) == ("pinned", 1)  # first writer's start columns (documented)


def test_streamed_turn_row_exists_before_its_tool_calls_are_visible():
    """Review finding: a follow-up must never write a turn's row first. Its tool calls become
    visible mid-stream, so the row has to be written before they are recorded."""
    import httpx

    from apphelpers import Chunks
    seen: dict = {}
    p = make_proxy()

    def before(n, part):
        if part.startswith(b"data: [DONE]"):  # the calls were recorded with the usage chunk
            seen["affinity"] = p.affinity.lookup(["s9"])
            seen["rows"] = rows(p)

    usage = {"prompt_tokens": 40, "completion_tokens": 4, "total_tokens": 44}
    parts = sse(chunk(tool_ids=["s9"]), chunk(usage=usage, finish="stop"))
    up = Upstream({"model-a": [httpx.Response(200, headers={"content-type": "text/event-stream"},
                                              stream=Chunks(parts, before=before))]})
    with mocked(up), running(p) as c:
        post(c, chat_body(stream=True, tools=TOOLS))
        got = rows(p)
    assert seen["affinity"] == {"s9": "model-a"}
    [row] = seen["rows"]
    assert (row["reason"], row["tools"], row["input_tokens"], row["output_tokens"]) == ("first", 3, 40, 4)
    assert got == seen["rows"]  # finishing the stream wrote nothing more
