"""Stream token counts come from the last `usage` chunk (DESIGN §27): Google sends `usage` on
every chunk as a running total, and the proxy used to keep the first."""

from __future__ import annotations

import json

import httpx

from apphelpers import (AUTH, Upstream, captured_logs, chat_body, chunk, error_body, make_proxy, mocked,
                        ok, running, sse, streamed)
from helpers import FailingConn


def usage(prompt: int, completion: int) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def turn_output(p) -> list[int]:
    return [r[0] for r in p.conn.execute("SELECT output_tokens FROM turns ORDER BY rowid")]


def affinity_ids(p) -> list[str]:
    return [r[0] for r in p.conn.execute("SELECT tool_call_id FROM affinity ORDER BY rowid")]


def run(*responses, proxy=None, stream: bool = True):
    """Serve each response to one request on model-a; (proxy, log lines, turn output tokens)."""
    p = proxy or make_proxy()
    up = Upstream({"model-a": list(responses)})
    with mocked(up), running(p) as c, captured_logs() as logs:
        for _ in responses:
            r = c.post("/v1/chat/completions", json=chat_body(model="model-a", stream=stream), headers=AUTH)
            assert r.status_code == 200
        out = turn_output(p)
        p.recorded_ids = affinity_ids(p)  # read before the database closes
    lines = []
    for line in logs.getvalue().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event") == "request":
            lines.append(obj)
    return p, lines, out


def output_metric(p) -> float:
    return p.metrics.counter_value("qr_tokens_total", model="model-a", kind="output")


def test_cumulative_usage_gives_the_last_count():
    """The regression test: the old code logged 5 (the first chunk's count)."""
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40)),
               chunk("c", usage=usage(70, 90), finish="stop"))
    p, [line], turns = run(streamed(body))
    assert (line["prompt_tokens"], line["output_tokens"]) == (70, 90)
    assert output_metric(p) == 90
    assert turns == [90]
    assert line["stream_end"]["end"] == "done"


def test_settles_once_at_the_first_usage_chunk(monkeypatch):
    p = make_proxy()
    calls = []
    real = p.router.complete

    def spy(served, prompt_tokens, *a, **kw):
        calls.append((prompt_tokens, bool(a and a[0])))
        return real(served, prompt_tokens, *a, **kw)

    monkeypatch.setattr(p.router, "complete", spy)
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40)),
               chunk(usage=usage(70, 60), finish="stop"))
    _, [line], _ = run(streamed(body), proxy=p)
    assert calls == [(70, False)]  # one settle, at the first usage chunk, with its prompt count
    assert line["output_tokens"] == 60


def test_tool_call_ids_after_usage_are_still_recorded():
    """Every chunk now carries usage; IDs arriving on later usage chunks must still be recorded."""
    body = sse(chunk("a", usage=usage(70, 5)), chunk(tool_ids=["late"], usage=usage(70, 30)),
               chunk(usage=usage(70, 31), finish="tool_calls"))
    p, [line], turns = run(streamed(body))
    assert p.recorded_ids == ["late"]
    assert line["output_tokens"] == 31 and turns == [31]


def test_error_event_after_two_usage_chunks_keeps_the_second_count():
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40)),
               error_body(503, "UNAVAILABLE", "x"), done=False)
    p, [line], turns = run(streamed(body))
    assert line["stream_end"]["end"] == "error_event"
    assert line["output_tokens"] == 40 and turns == [40] and output_metric(p) == 40


def test_eof_after_two_usage_chunks_keeps_the_second_count():
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40)), done=False)
    p, [line], turns = run(streamed(body))
    assert line["stream_end"]["end"] == "eof"
    assert line["output_tokens"] == 40 and turns == [40] and output_metric(p) == 40


def test_read_error_after_two_usage_chunks_keeps_the_second_count():
    resp = streamed(sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40)), done=False),
                    error=httpx.RemoteProtocolError("dropped"))
    p, [line], turns = run(resp)
    assert line["stream_end"]["end"] == "read_error"
    assert line["output_tokens"] == 40 and turns == [40] and output_metric(p) == 40


def test_two_requests_of_one_turn_each_add_their_own_difference():
    """A follow-up shares the turn row: the row ends with the sum of both final counts."""
    first = streamed(sse(chunk(tool_ids=["c1"], usage=usage(70, 5)), chunk(usage=usage(70, 25), finish="tool_calls")))
    second = streamed(sse(chunk("a", usage=usage(90, 3)), chunk("b", usage=usage(90, 50), finish="stop")))
    p = make_proxy()
    up = Upstream({"model-a": [first, second]})
    follow = chat_body(model="model-a", stream=True)
    follow["messages"] += [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}]},
                           {"role": "tool", "tool_call_id": "c1", "content": "done"}]
    with mocked(up), running(p) as c:
        c.post("/v1/chat/completions", json=chat_body(model="model-a", stream=True), headers=AUTH)
        c.post("/v1/chat/completions", json=follow, headers=AUTH)
        got = p.conn.execute("SELECT steps, input_tokens, output_tokens FROM turns").fetchall()
    assert got == [(2, 160, 75)]


def test_prompt_count_changing_between_chunks():
    """Not seen live (§27.3 assumption): the log shows the last prompt count, the row the first."""
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(75, 40), finish="stop"))
    p, [line], _ = run(streamed(body))
    assert (line["prompt_tokens"], line["output_tokens"]) == (75, 40)


def test_falling_or_malformed_usage_never_lowers_the_count():
    body = sse(chunk("a", usage=usage(70, 50)),
               chunk("b", usage=usage(70, 10)),                        # goes down
               chunk("c", usage={"prompt_tokens": "x", "total_tokens": None}),  # malformed
               chunk(usage={"prompt_tokens": -1}, finish="stop"))     # negative
    p, [line], turns = run(streamed(body))
    assert (line["prompt_tokens"], line["output_tokens"]) == (70, 50)
    assert turns == [50]


def test_failed_add_output_does_not_fail_the_stream():
    p = make_proxy()
    p.turns._conn = FailingConn(p.conn, fail_on="UPDATE turns SET output_tokens")
    body = sse(chunk("a", usage=usage(70, 5)), chunk("b", usage=usage(70, 40), finish="stop"))
    _, [line], turns = run(streamed(body), proxy=p)
    assert line["reason"] == "served" and line["output_tokens"] == 40
    assert turns == [5]  # the row keeps what it had at settle time
    assert p.router.state_errors == 1


def test_single_chunk_stream_and_non_stream_unchanged():
    p, [line], turns = run(streamed(sse(chunk("a", usage=usage(70, 9), finish="stop"))))
    assert line["output_tokens"] == 9 and turns == [9] and output_metric(p) == 9
    p, [line], turns = run(ok(), stream=False)
    assert line["output_tokens"] == turns[0]


def test_no_usage_keeps_the_estimate_and_adds_nothing():
    p, [line], turns = run(streamed(sse(chunk("a"), chunk("b", finish="stop"))))
    assert line["output_tokens"] is None and turns == [0]
