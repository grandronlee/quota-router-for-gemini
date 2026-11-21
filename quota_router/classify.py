"""Classify a Gemini 429 body as per-minute requests, input tokens, per-day, or unknown (DESIGN §6).

No real 429 was captured in Phase 0, so the signals come from Google's public RPC
error format (`google.rpc.QuotaFailure` violations with a `quotaId`, and
`google.rpc.RetryInfo.retryDelay`), with the message text as a fallback.
Anything not recognised is "unknown"; the store gives it a 60 s cooldown.

Error bodies may be an object (`{"error": {...}}`) or an array (`[{"error": {...}}]`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

Kind = Literal["rpm", "tpm", "rpd", "unknown"]

_DAY = re.compile(r"per[\s_-]?day", re.I)
_MINUTE = re.compile(r"per[\s_-]?minute", re.I)
_TOKENS = re.compile(r"token", re.I)
_DELAY = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$", re.A)
_MSG_DELAY = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I | re.A)
_HEADER_DELAY = re.compile(r"^\s*(\d+)\s*$", re.A)
MAX_DELAY_S = 86400.0  # anything longer (or not finite) is ignored
_PRECEDENCE: dict[Kind, int] = {"rpd": 3, "tpm": 2, "rpm": 1, "unknown": 0}


@dataclass(frozen=True)
class Classification:
    kind: Kind
    retry_after_s: float | None
    detail: str = ""


def parse_error_body(body: Any) -> dict | None:
    """Return the inner `error` object from an object- or array-shaped body, or None."""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            return None
    if isinstance(body, list):
        body = next((x for x in body if isinstance(x, dict) and isinstance(x.get("error"), dict)), None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body["error"]
    return None


def _kind_of(text: str) -> Kind:
    if _DAY.search(text):
        return "rpd"
    if _TOKENS.search(text):
        return "tpm"
    if _MINUTE.search(text):
        return "rpm"
    return "unknown"


def _delay(pattern: re.Pattern, value: Any, search: bool = False) -> float | None:
    if not isinstance(value, str):
        return None
    m = pattern.search(value) if search else pattern.match(value)
    if not m:
        return None
    x = float(m.group(1))
    return x if math.isfinite(x) and 0 <= x <= MAX_DELAY_S else None


def classify_429(body: Any, headers: Mapping[str, str] | None = None) -> Classification:
    """Never raises: anything unexpected is classified as "unknown"."""
    try:
        return _classify(body, headers)
    except Exception:
        return Classification("unknown", None, "unparseable 429 body")


def _classify(body: Any, headers: Mapping[str, str] | None) -> Classification:
    err = parse_error_body(body)
    kind: Kind = "unknown"
    delay: float | None = None
    detail: list[str] = []

    details = err.get("details") if err else None
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        dtype = str(d.get("@type", ""))
        if dtype.endswith("google.rpc.QuotaFailure"):
            violations = d.get("violations")
            for v in violations if isinstance(violations, list) else []:
                if not isinstance(v, dict):
                    continue
                qid = str(v.get("quotaId", ""))
                metric = str(v.get("quotaMetric", ""))
                k = _kind_of(qid)
                if k == "unknown":
                    k = _kind_of(metric)
                if qid or metric:
                    detail.append(qid or metric)
                if _PRECEDENCE[k] > _PRECEDENCE[kind]:
                    kind = k
        elif dtype.endswith("google.rpc.RetryInfo"):
            delay = _delay(_DELAY, d.get("retryDelay"))

    message = str(err.get("message", "")) if err else ""
    if kind == "unknown" and message:
        kind = _kind_of(message)
    if delay is None and message:
        delay = _delay(_MSG_DELAY, message, search=True)
    if delay is None and headers:
        ra = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
        delay = _delay(_HEADER_DELAY, ra)
    return Classification(kind, delay, "; ".join(detail))
