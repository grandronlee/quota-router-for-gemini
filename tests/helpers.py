from __future__ import annotations

import asyncio
import copy
import datetime as dt
from typing import Any, Callable

from quota_router.affinity import AffinityStore
from quota_router.clock import PT, FakeClock
from quota_router.config import Config, ModelConfig, parse_config
from quota_router.db import connect
from quota_router.quota import QuotaStore
from quota_router.router import Router, Served, UpstreamResult


def pt(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> float:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=PT).timestamp()


T0 = pt(2026, 11, 20, 12, 0, 0)  # midday PT, far from any boundary


def model_dict(model_id: str, **over: Any) -> dict:
    d = {"id": model_id, "rpm": 15, "tpm_input": 250_000, "rpd": 500,
         "input_limit": 1_048_576, "output_limit": 65_536}
    d.update(over)
    return d


def make_config(models: dict[str, dict] | None = None, routes: dict[str, list[str]] | None = None,
                **top: Any) -> Config:
    models = models or {"a": model_dict("model-a"), "b": model_dict("model-b")}
    routes = routes or {"fast": list(models)}
    return parse_config({"models": copy.deepcopy(models), "routes": routes, **top})


class Env:
    """A router wired to a fake clock and a SQLite database (in memory or on disk)."""

    def __init__(self, config: Config, db: str = ":memory:", start: float = T0,
                 clock: FakeClock | None = None) -> None:
        self.config = config
        self.clock = clock or FakeClock(start)
        self.conn = connect(db)
        self.store = QuotaStore(config.models.values(), self.conn, self.clock)
        self.affinity = AffinityStore(self.conn, self.clock)
        self.router = Router(config, self.store, self.affinity, self.clock)

    def model(self, key: str) -> ModelConfig:
        return self.config.models[key]

    def route(self, request: dict, upstream: Callable, **kw: Any):
        return asyncio.run(self.router.route(request, upstream, **kw))


class ScriptedUpstream:
    """Returns scripted results per model ID (default: Served). Records every call."""

    def __init__(self, script: dict[str, list[UpstreamResult] | UpstreamResult] | None = None,
                 on_call: Callable[[ModelConfig, float], None] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, float]] = []
        self.bodies: list[dict | None] = []  # the `body` passed for each call (None = the client's)
        self.on_call = on_call

    async def __call__(self, model: ModelConfig, timeout_s: float, *,
                       body: dict | None = None) -> UpstreamResult:
        self.calls.append((model.id, timeout_s))
        self.bodies.append(body)
        if self.on_call:
            self.on_call(model, timeout_s)
        await asyncio.sleep(0)
        r = self.script.get(model.id, Served({"ok": model.id}))
        if isinstance(r, list):
            return r.pop(0) if r else Served({"ok": model.id})
        return r

    @property
    def called(self) -> list[str]:
        return [m for m, _ in self.calls]


def chat(model: str = "fast", messages: list | None = None, **extra: Any) -> dict:
    return {"model": model, "messages": messages or [{"role": "user", "content": "hi"}], **extra}


class FailingConn:
    """Wraps a sqlite3 connection; statements containing `fail_on` raise OperationalError.
    With `times`, only that many matching statements fail."""

    def __init__(self, conn, fail_on: str | None = None, times: int | None = None) -> None:
        self._conn = conn
        self.fail_on = fail_on
        self.times = times

    def _check(self, sql: str) -> None:
        import sqlite3
        if self.fail_on and self.fail_on in sql and (self.times is None or self.times > 0):
            if self.times is not None:
                self.times -= 1
            raise sqlite3.OperationalError("disk I/O error (simulated)")

    def execute(self, sql, *args):
        self._check(sql)
        return self._conn.execute(sql, *args)

    def executemany(self, sql, *args):
        self._check(sql)
        return self._conn.executemany(sql, *args)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)
