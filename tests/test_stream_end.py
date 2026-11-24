"""How a stream ended, in the request log's `stream_end` field (DESIGN §26)."""

from __future__ import annotations

import asyncio
import json

import httpx

from apphelpers import (AUTH, Upstream, captured_logs, chat_body, chunk, error_body, make_proxy, mocked,
                        ok, running, sse, streamed)
from quota_router.app import StreamSession
from quota_router.router import RouteServed
from quota_router.upstream import SSEEvent, Stream

USAGE = {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}
SECRET = "https://example.invalid/v1?key=not-to-be-logged"


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


def one_stream(*responses) -> tuple[list[dict], str]:
    """Each response served in turn to a streamed request on model-a; their log lines."""
    up = Upstream({"model-a": list(responses)})
    with mocked(up), running(make_proxy()) as c, captured_logs() as logs:
        for _ in responses:
            c.post("/v1/chat/completions", json=chat_body(model="model-a", stream=True), headers=AUTH)
    return request_lines(logs), logs.getvalue()


def test_done():
    [line], _ = one_stream(streamed(sse(chunk("hi"), chunk(finish="stop", usage=USAGE))))
    end = line["stream_end"]
    assert end == {"end": "done", "chunks": 2, "finish_reason": "stop", "usage_seen": True,
                   "idle_ms": end["idle_ms"]}
    assert isinstance(end["idle_ms"], int) and end["idle_ms"] >= 0
    assert line["reason"] == "served"


def test_error_event():
    err = error_body(503, "UNAVAILABLE", SECRET)
    [line], text = one_stream(streamed(sse(chunk("x"), err, done=False)))
    end = line["stream_end"]
    assert end["end"] == "error_event" and end["chunks"] == 2 and "error" not in end
    assert end["finish_reason"] is None and end["usage_seen"] is False
    assert SECRET not in text


def test_eof_after_finish_reason_is_the_regression_case():
    """Live run 4, 06:53:34: usage and a finish_reason, then the body ends without [DONE]."""
    [line], _ = one_stream(streamed(sse(chunk("a"), chunk(finish="stop", usage=USAGE), done=False)))
    end = line["stream_end"]
    assert end["end"] == "eof" and "error" not in end
    assert end["finish_reason"] == "stop" and end["usage_seen"] is True and end["chunks"] == 2
    assert line["reason"] == "stream_error"
    assert [a["outcome"] for a in line["attempts"]] == ["stream_incomplete"]


def test_read_error_logs_the_httpx_class_never_the_message():
    resp = streamed(sse(chunk("a"), done=False), error=httpx.RemoteProtocolError(SECRET))
    [line], text = one_stream(resp)
    end = line["stream_end"]
    assert end["end"] == "read_error" and end["error"] == "RemoteProtocolError"
    assert end["chunks"] == 1 and end["finish_reason"] is None
    assert [a["outcome"] for a in line["attempts"]] == ["stream_incomplete"]
    assert SECRET not in text


def test_comments_and_done_are_not_chunks_and_last_finish_reason_wins():
    parts = [b": keep-alive\n\n"] + sse(chunk("a", finish="length"), chunk("b"), chunk(finish="tool_calls"))
    [line], _ = one_stream(streamed(parts))
    end = line["stream_end"]
    assert end["chunks"] == 3 and end["finish_reason"] == "tool_calls"  # a null one doesn't reset it
    assert end["usage_seen"] is False


def test_unknown_or_odd_finish_reason_is_other():
    odd = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": ["x"]}]}
    lines, text = one_stream(streamed(sse(chunk(finish="MALFORMED_" + "secretish"))), streamed(sse(odd)))
    assert [ln["stream_end"]["finish_reason"] for ln in lines] == ["other", "other"]
    assert "secretish" not in text


def test_malformed_choices_dont_break_the_stream():
    bad = [{"object": "x", "choices": "nope"}, {"object": "x", "choices": [1, None]}, {"usage": "x"}]
    [line], _ = one_stream(streamed(sse(*bad)))
    assert line["reason"] == "served"
    assert line["stream_end"]["chunks"] == 3 and line["stream_end"]["usage_seen"] is False


def test_non_streamed_lines_have_no_stream_end():
    up = Upstream({"model-a": [ok("fine")]})
    with mocked(up), running(make_proxy()) as c, captured_logs() as logs:
        assert c.post("/v1/chat/completions", json=chat_body(model="model-a"), headers=AUTH).status_code == 200
    [line] = request_lines(logs)
    assert "stream_end" not in line


# ---- session-level endings -----------------------------------------------------

def _session(events):
    p = make_proxy()

    class FakeResponse:
        async def aclose(self):
            pass

    first = SSEEvent(b"data: {}\n\n", json.dumps(chunk("first")))
    res = p.store.try_reserve(p.config.models["a"], 10)
    served = RouteServed(p.config.models["a"], None, res, ["model-a"], None)
    stream = Stream(FakeResponse(), events(), first)  # type: ignore[arg-type]
    return StreamSession(p, served, stream, request_id="r", route="fast", started=0.0, input_estimate=10)


def test_client_gone():
    async def events():
        yield SSEEvent(b"data: {}\n\n", json.dumps(chunk("second")))

    session = _session(events)

    async def run():
        gen = session.body()
        await gen.__anext__()  # the first chunk, then the client goes away
        await gen.aclose()
        await session.finish()

    with captured_logs() as logs:
        asyncio.run(run())
    [line] = request_lines(logs)
    assert line["reason"] == "client_disconnected"
    assert line["stream_end"]["end"] == "client_gone" and line["stream_end"]["chunks"] == 1


def test_non_httpx_read_error_is_other():
    async def events():
        raise ValueError(SECRET)
        yield  # pragma: no cover

    session = _session(events)

    async def run():
        async for _ in session.body():
            pass
        await session.finish()

    with captured_logs() as logs:
        asyncio.run(run())
    [line] = request_lines(logs)
    end = line["stream_end"]
    assert end["end"] == "read_error" and end["error"] == "other" and end["chunks"] == 1
    assert line["reason"] == "stream_error"
    assert session.outcome[0] == "stream_incomplete"  # this bare session has no attempts list
    assert SECRET not in json.dumps(line)


def test_client_gone_while_waiting_for_upstream():
    """The usual disconnect: cancelled while the next upstream read is pending."""
    async def events():
        await asyncio.sleep(10)
        yield SSEEvent(b"data: {}\n\n", json.dumps(chunk("never")))  # pragma: no cover

    session = _session(events)

    async def run():
        async def consume():
            async for _ in session.body():
                pass
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await session.finish()

    with captured_logs() as logs:
        asyncio.run(run())
    [line] = request_lines(logs)
    end = line["stream_end"]
    assert end["end"] == "client_gone" and end["chunks"] == 1
    assert end["idle_ms"] >= 40  # counts the wait since the first event


def test_first_ending_is_kept():
    async def events():
        return
        yield  # pragma: no cover

    session = _session(events)

    async def run():
        async for _ in session.body():
            pass
        await session.finish()  # finish() must not turn eof into client_gone

    with captured_logs() as logs:
        asyncio.run(run())
    [line] = request_lines(logs)
    assert line["stream_end"]["end"] == "eof"
