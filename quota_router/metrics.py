"""Prometheus text metrics and the in-memory request stats behind /status (DESIGN §17.2, §20.8).

Written by hand to keep dependencies short. Every label value comes from config
or the fixed lists below; `bounded()` maps anything else to "other".
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterable

from .clock import pt_date, pt_day_start

STATUSES = ("200", "400", "401", "403", "404", "408", "413", "429", "500", "503")
REASONS = ("served", "passthrough", "quota", "unavailable", "context_length_exceeded",
           "no_enabled_model", "model_not_found", "bad_request", "unauthorized", "forbidden_host",
           "stream_error", "client_disconnected", "internal_error", "login_throttled", "busy")
OUTCOMES = ("ok", "429_rpm", "429_tpm", "429_rpd", "429_unknown", "5xx", "timeout", "404", "4xx", "error")
STATES = ("available", "cooldown", "exhausted", "disabled")
FIRST_BYTE_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180)
RECENT_ERRORS = 20

HELP = {
    "qr_requests_total": ("counter", "Client requests by route, status and reason."),
    "qr_upstream_attempts_total": ("counter", "Upstream calls by model and outcome."),
    "qr_tokens_total": ("counter", "Reported tokens by model and kind (input, output)."),
    "qr_handoff_total": ("counter", "Tool-turn handoff attempts by owner, target and result "
                                    "(served, rejected, failed); DESIGN §16.8."),
    "qr_thought_signatures_total": ("counter", "Thought signatures by model and action: captured "
                                               "(stored, none, too_large, invalid, conflict, evicted) "
                                               "or for calls sent without one (restored, placeholder); "
                                               "DESIGN §23.6."),
    "qr_route_decisions_total": ("counter", "Routed gemini-auto requests by x-route-reason and "
                                            "x-route-tier; DESIGN §16.9.5."),
    "qr_pace_left": ("gauge", "Requests the paced tier has left today (exhausted models count 0)."),
    "qr_pace_upgrade_above": ("gauge", "A gemini-auto turn is upgraded to the paced tier while "
                                       "qr_pace_left is above this."),
    "qr_auth_failures_total": ("counter", "Requests rejected for a missing or wrong token."),
    "qr_state_errors_total": ("counter", "SQLite errors while reading or writing quota state."),
    "qr_tool_call_id_reused_total": ("counter", "Tool-call IDs recorded twice (DESIGN §20.6)."),
    "qr_first_byte_seconds": ("histogram", "Time from request arrival to the first byte sent."),
    "qr_rpd_used": ("gauge", "Requests used today (PT date)."),
    "qr_rpd_limit": ("gauge", "Configured requests per day."),
    "qr_rpm_used": ("gauge", "Requests in the last 60 seconds."),
    "qr_tpm_input_used": ("gauge", "Input tokens in the last 60 seconds."),
    "qr_model_state": ("gauge", "1 for the model's current state."),
}


def bounded(value: object, allowed: Iterable[str], other: str = "other") -> str:
    v = str(value)
    return v if v in allowed else other


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in pairs) + "}" if pairs else ""


def _num(x: float) -> str:
    if math.isinf(x):
        return "+Inf"
    return str(int(x)) if float(x).is_integer() else repr(float(x))


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self._hist: dict[tuple, list[float]] = {}  # labels -> [bucket counts..., sum, count]

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        self._counters[name][tuple(sorted(labels.items()))] += value

    def observe_first_byte(self, seconds: float, route: str) -> None:
        key = (("route", route),)
        h = self._hist.setdefault(key, [0.0] * (len(FIRST_BYTE_BUCKETS) + 2))
        for i, b in enumerate(FIRST_BYTE_BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-2] += seconds
        h[-1] += 1

    def counter_value(self, name: str, **labels: str) -> float:
        return self._counters.get(name, {}).get(tuple(sorted(labels.items())), 0.0)

    def render(self, gauges: dict[str, list[tuple[dict[str, str], float]]],
               fixed_counters: dict[str, float]) -> str:
        lines: list[str] = []

        def head(name: str) -> None:
            kind, text = HELP[name]
            lines.append(f"# HELP {name} {text}")
            lines.append(f"# TYPE {name} {kind}")

        for name in ("qr_requests_total", "qr_upstream_attempts_total", "qr_tokens_total",
                     "qr_handoff_total", "qr_thought_signatures_total", "qr_route_decisions_total"):
            head(name)
            for labels, v in sorted(self._counters.get(name, {}).items()):
                lines.append(f"{name}{_labels(labels)} {_num(v)}")
        for name, v in fixed_counters.items():
            head(name)
            lines.append(f"{name} {_num(v)}")
        head("qr_first_byte_seconds")
        for labels, h in sorted(self._hist.items()):
            for i, b in enumerate(FIRST_BYTE_BUCKETS):
                lines.append(f"qr_first_byte_seconds_bucket{_labels(labels + (('le', _num(b)),))} {_num(h[i])}")
            lines.append(f"qr_first_byte_seconds_bucket{_labels(labels + (('le', '+Inf'),))} {_num(h[-1])}")
            lines.append(f"qr_first_byte_seconds_sum{_labels(labels)} {_num(h[-2])}")
            lines.append(f"qr_first_byte_seconds_count{_labels(labels)} {_num(h[-1])}")
        for name, samples in gauges.items():
            head(name)
            for labels, v in samples:
                lines.append(f"{name}{_labels(tuple(sorted(labels.items())))} {_num(v)}")
        return "\n".join(lines) + "\n"


@dataclass
class ErrorEntry:
    at: float
    route: str
    status: int
    reason: str


class RequestStats:
    """Counts by status and reason since the PT day began (or since start), and recent errors."""

    def __init__(self, now: float) -> None:
        self.started_at = now
        self._day = pt_date(now)
        self.since = now
        self.by_status: dict[str, int] = defaultdict(int)
        self.by_reason: dict[str, int] = defaultdict(int)
        self.recent: deque[ErrorEntry] = deque(maxlen=RECENT_ERRORS)

    def _roll(self, now: float) -> None:
        day = pt_date(now)
        if day != self._day:
            self._day = day
            self.since = max(self.started_at, pt_day_start(now))
            self.by_status.clear()
            self.by_reason.clear()

    def record(self, now: float, route: str, status: int, reason: str) -> None:
        self._roll(now)
        self.by_status[bounded(status, STATUSES)] += 1
        self.by_reason[reason] += 1
        if status >= 400 or reason in ("stream_error", "client_disconnected", "internal_error"):
            self.recent.append(ErrorEntry(now, route, status, reason))

    def view(self, now: float) -> dict:
        self._roll(now)
        return {"since": self.since, "by_status": dict(self.by_status), "by_reason": dict(self.by_reason)}
