"""Helpers for HTTP-layer tests: a Proxy wired to a fake clock, and respx upstream scripts."""

from __future__ import annotations

import io
import json
import logging
from contextlib import contextmanager
from typing import Any, Callable

import httpx
import respx
from fastapi.testclient import TestClient

from helpers import T0, make_config, model_dict
from quota_router.app import Proxy, create_app
from quota_router.clock import FakeClock
from quota_router.db import connect
from quota_router.logs import JsonFormatter
from quota_router.scrub import Scrubber
from quota_router.settings import Settings

BASE = "https://upstream.test/v1beta/openai/"
URL = BASE + "chat/completions"
KEY = "AIzaSyTEST-key-000000000000000000000000000"
TOKEN = "proxy-token-for-tests-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LOCAL = "http://localhost:4000"


def make_proxy(models: dict | None = None, routes: dict | None = None, *, db: str = ":memory:",
               clock: FakeClock | None = None, allowed_hosts: frozenset[str] | None = None, **top: Any) -> Proxy:
    models = models or {"a": model_dict("model-a"), "b": model_dict("model-b")}
    cfg = make_config(models, routes, **{"upstream_base_url": BASE, "custom_upstream": True, "db_path": db, **top})
    kw = {} if allowed_hosts is None else {"allowed_hosts": allowed_hosts}
    settings = Settings(gemini_api_key=KEY, proxy_token=TOKEN, **kw)
    conn = connect(db, check_same_thread=False)
    return Proxy(settings, cfg, clock=clock or FakeClock(T0), conn=conn)


@contextmanager
def running(proxy: Proxy):
    with TestClient(create_app(proxy), base_url=LOCAL) as client:
        yield client


# ---- upstream responses ---------------------------------------------------

def completion(text: str = "hello", *, tool_ids: list[str] | None = None, prompt: int = 8,
               total: int = 20, completion_tokens: int = 2) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if tool_ids:
        msg["tool_calls"] = [{"id": i, "type": "function",
                              "function": {"name": "get_weather", "arguments": "{}"}} for i in tool_ids]
    return {"id": "x", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion_tokens, "total_tokens": total}}


def ok(text: str = "hello", **kw: Any) -> httpx.Response:
    return httpx.Response(200, json=completion(text, **kw))


def error_body(code: int, status: str, message: str, details: list | None = None, array: bool = False) -> Any:
    err = {"error": {"code": code, "message": message, "status": status, **({"details": details} if details else {})}}
    return [err] if array else err


def rate_limited(kind: str = "minute", delay: str | None = "20s") -> httpx.Response:
    quota_id = {"minute": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                "day": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}[kind]
    details: list = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                      "violations": [{"quotaId": quota_id, "quotaMetric": "x"}]}]
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return httpx.Response(429, json=error_body(429, "RESOURCE_EXHAUSTED", "quota exceeded", details))


def unavailable() -> httpx.Response:
    return httpx.Response(503, json=error_body(503, "UNAVAILABLE", "high demand", array=True))


def sse(*events: Any, done: bool = True) -> list[bytes]:
    parts = [b"data: " + (e if isinstance(e, bytes) else json.dumps(e).encode()) + b"\n\n" for e in events]
    if done:
        parts.append(b"data: [DONE]\n\n")
    return parts


def chunk(content: str | None = None, *, tool_ids: list[str] | None = None, usage: dict | None = None,
          finish: str | None = None) -> dict:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if tool_ids:
        delta["tool_calls"] = [{"index": n, "id": i, "type": "function",
                                "function": {"name": "f", "arguments": "{}"}} for n, i in enumerate(tool_ids)]
    out: dict = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        out["usage"] = usage
    return out


class Chunks(httpx.AsyncByteStream):
    """Async SSE body. `before` is called before each part is produced (index, part);
    `error` is raised after the last part."""

    def __init__(self, parts: list[bytes], error: Exception | None = None,
                 before: Callable[[int, bytes], None] | None = None) -> None:
        self.parts = parts
        self.error = error
        self.before = before
        self.closed = False

    async def __aiter__(self):
        for n, part in enumerate(self.parts):
            if self.before:
                self.before(n, part)
            yield part
        if self.error:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


def streamed(parts: list[bytes], **kw: Any) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(parts, **kw))


class Upstream:
    """Scripted Google: per model ID, a list of responses (or callables taking the request)."""

    def __init__(self, script: dict[str, list] | None = None) -> None:
        self.script = script or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        queue = self.script.get(model)
        r = queue.pop(0) if queue else ok(f"from {model}")
        if isinstance(r, Exception):
            raise r
        return r(request) if callable(r) else r

    @property
    def models(self) -> list[str]:
        return [json.loads(r.content)["model"] for r in self.requests]

    def body(self, n: int = -1) -> dict:
        return json.loads(self.requests[n].content)


@contextmanager
def mocked(upstream: Upstream):
    with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(side_effect=upstream)
        yield router


@contextmanager
def captured_logs():
    """Every log line, formatted and scrubbed exactly as in production."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(Scrubber([KEY, TOKEN])))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def chat_body(model: str = "fast", content: str = "hi", **extra: Any) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}
