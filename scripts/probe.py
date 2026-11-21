#!/usr/bin/env python3
"""Phase 0 probe: record real model IDs, capabilities and 429 bodies.

Uses only the standard library, so it runs with `python -I` and no virtualenv.
Reads GEMINI_API_KEY from the environment (never from a file, never from argv):

    set -a; . ./.env; set +a
    python -I scripts/probe.py --list-only            # models.list only, no generation quota
    python -I scripts/probe.py                        # full probe
    python -I scripts/probe.py --only "Gemini 3 Flash" --checks tools,roundtrip
    python -I scripts/probe.py --rescore              # rebuild the report offline, no requests
    python -I scripts/probe.py --handoffs             # multi-step handoffs only (DESIGN §16.8)

Writes:
    data/probe/<timestamp>.json   raw results (git-ignored); key, project IDs and
                                  thought signatures are not stored
    docs/probe-results.md         summary built from every raw run in data/probe/

No retries: every request is sent at most once. The only deliberate 429 is the
per-minute burst (PLAN.md Phase 0, step 3): BURST_COUNT parallel calls, one more
than the model's RPM, sent once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_BASE = BASE + "/openai"
PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "probe"
TIMEOUT_S = 120

# Display names from DESIGN.md §2, plus models probed for information only.
INVENTORY = [
    ("Gemini 4 Hydrogen", "route"),
    ("Gemini 3.8 Flash", "route"),
    ("Gemini 3.6 Flash", "route"),
    ("Gemini 3 Flash", "route"),
    ("Gemini 2.5 Flash", "route"),
    ("Gemini 3.5 Flash Lite", "route"),
    ("Gemini 3.1 Flash Lite", "route"),
    ("Gemini 2.5 Flash Lite", "route"),
    ("Gemma 4 31B", "info"),
    ("Gemma 4 26B", "info"),
    ("Gemini Robotics ER 2", "info"),
]
CHECKS = ("nonstream", "stream", "tools", "roundtrip")
BURST_MODEL = "Gemini 3.1 Flash Lite"
BURST_COUNT = 16  # RPM is 15

# Tool-loop handoffs. Each reuses a tool call already made in this run and sends the
# tool result to `reply`, so it costs one request. (name, call, reply, signature)
# signature: "keep" = as received, "drop" = removed, "dummy" = DUMMY_SIGNATURE.
EXPERIMENTS = [
    # Expected: HTTP 400, which shows Google enforces signatures on the v1 model.
    ("same model, signature removed", "Gemini 3.5 Flash Lite", "Gemini 3.5 Flash Lite", "drop"),
    ("handoff, v1 pair", "Gemini 3.5 Flash Lite", "Gemini 3.1 Flash Lite", "keep"),
    ("handoff, v1 pair reversed", "Gemini 3.1 Flash Lite", "Gemini 3.5 Flash Lite", "keep"),
    ("handoff, v1 pair, placeholder signature",
     "Gemini 3.5 Flash Lite", "Gemini 3.1 Flash Lite", "dummy"),
    ("handoff, 2.5 call into Gemini 3", "Gemini 2.5 Flash", "Gemini 3 Flash", "keep"),
]
# Documented for "transferring a trace from a different model" (thought-signatures FAQ).
DUMMY_SIGNATURE = "skip_thought_signature_validator"

# Multi-step handoffs (DESIGN §16.8 1a, `--handoffs`). The source makes call 1; each
# target gets call 1 and its result, must make call 2 itself, then answer from both.
HANDOFF_SOURCE = "Gemini 3.5 Flash Lite"
HANDOFF_TARGETS = ["Gemini 4 Hydrogen", "Gemini 3.8 Flash", "Gemini 3.6 Flash", "Gemini 3 Flash",
                   "Gemini 2.5 Flash", "Gemini 3.5 Flash Lite", "Gemini 3.1 Flash Lite",
                   "Gemini 2.5 Flash Lite"]
HANDOFF_SIGNATURES = ("dummy", "keep")  # placeholder decides; as received is information only

# Extra words a display name may carry and still count as the same model.
SUFFIX_OK = {"preview", "experimental", "exp", "latest", "it", "instruct"}

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}
TOOL_CITY = "Paris"
# Appears only in the tool result, never in the prompt, so a reply that contains it
# must have read the result.
TOOL_MARKER = "WX-4417-QZ"
TOOL_PROMPT = (f"What is the weather in {TOOL_CITY} right now? Use the tool, then "
               "answer in one sentence that quotes the report_id it returns.")
TOOL_RESULT = {"city": TOOL_CITY, "temp_c": 17.3, "condition": "light drizzle",
               "report_id": TOOL_MARKER}

# The second call needs a value from the first call's result, so a model can't make
# both at once: the turn must have two assistant tool calls in sequence.
FORECAST_TOOL = {
    "type": "function",
    "function": {
        "name": "get_forecast",
        "description": "Get tomorrow's forecast for a weather report.",
        "parameters": {
            "type": "object",
            "properties": {"report_id": {"type": "string",
                                         "description": "report_id from get_weather"}},
            "required": ["report_id"],
        },
    },
}
FORECAST_MARKER = "FC-9032-LM"  # only in the second tool result
FORECAST_RESULT = {"report_id": TOOL_MARKER, "tomorrow": "sunny, 21 °C",
                   "forecast_id": FORECAST_MARKER}
HANDOFF_PROMPT = (f"Call get_weather for {TOOL_CITY}. Then call get_forecast with the "
                  "report_id it returns. Then answer in one sentence that quotes the "
                  "forecast_id. Make one tool call at a time.")

MANUAL_HEADING = "## Manual checks"

# Headers worth keeping from error responses; everything else is dropped.
KEEP_HEADERS = {"retry-after", "content-type", "x-ratelimit-limit",
                "x-ratelimit-remaining", "x-ratelimit-reset", "server-timing"}


class Scrubber:
    """Removes the API key, anything key-shaped and project numbers."""

    KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
    PROJECT_RE = re.compile(r"(projects/)\d+")
    CONSUMER_RE = re.compile(r"(\"consumer\"\s*:\s*\")[^\"]+")

    def __init__(self, key: str):
        self.key = key

    def text(self, s: str) -> str:
        if self.key:
            s = s.replace(self.key, "<redacted-key>")
        s = self.KEY_RE.sub("<redacted-key>", s)
        s = self.PROJECT_RE.sub(r"\1<redacted>", s)
        s = self.CONSUMER_RE.sub(r"\1<redacted>", s)
        return s

    def obj(self, o):
        return json.loads(self.text(json.dumps(o)))


def strip_private(o):
    """Drop keys starting with '_' (in-memory only data such as thought signatures)."""
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [strip_private(v) for v in o]
    return o


class Client:
    def __init__(self, key: str, scrub: Scrubber):
        self.key = key
        self.scrub = scrub
        self.count = 0
        self.errors_429: list[dict] = []
        self._lock = threading.Lock()

    def _request(self, method: str, url: str, body: dict | None, openai: bool):
        headers = {"Content-Type": "application/json", "User-Agent": "quota-router-probe"}
        if openai:
            headers["Authorization"] = f"Bearer {self.key}"
        else:
            headers["x-goog-api-key"] = self.key  # header, so the key never sits in a URL
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._lock:
            self.count += 1
        return urllib.request.urlopen(req, timeout=TIMEOUT_S)

    def _error(self, label: str, e: Exception, started: float) -> dict:
        out: dict = {"ok": False, "latency_s": round(time.monotonic() - started, 2)}
        if isinstance(e, urllib.error.HTTPError):
            raw = e.read().decode("utf-8", "replace")
            out["status"] = e.code
            try:
                out["body"] = self.scrub.obj(json.loads(raw))
            except ValueError:
                out["body"] = self.scrub.text(raw)[:4000]
            out["headers"] = {k.lower(): self.scrub.text(v) for k, v in e.headers.items()
                              if k.lower() in KEEP_HEADERS}
            if e.code == 429:
                with self._lock:
                    self.errors_429.append({
                        "label": label,
                        "at_pt": dt.datetime.now(PT).isoformat(timespec="seconds"),
                        "status": 429, "headers": out["headers"], "body": out["body"],
                    })
        else:
            out["status"] = None
            out["error"] = self.scrub.text(f"{type(e).__name__}: {e}")
        return out

    def get_json(self, url: str, openai: bool) -> dict:
        started = time.monotonic()
        try:
            with self._request("GET", url, None, openai) as r:
                return {"ok": True, "status": r.status, "json": json.load(r)}
        except Exception as e:  # noqa: BLE001 - every failure is recorded, not raised
            return self._error(url, e, started)

    def chat(self, label: str, body: dict) -> dict:
        started = time.monotonic()
        try:
            with self._request("POST", OPENAI_BASE + "/chat/completions", body, True) as r:
                if body.get("stream"):
                    return self._read_stream(r, started)
                j = json.load(r)
                return summarize_completion(j, r.status, started)
        except Exception as e:  # noqa: BLE001
            return self._error(label, e, started)

    def _read_stream(self, r, started: float) -> dict:
        chunks = 0
        first_byte_s = None
        content = []
        finish = None
        usage = None
        usage_chunk_index = None
        usage_chunk_had_choices = None
        tool_calls: dict[int, dict] = {}
        extra_paths: set[str] = set()
        saw_done = False
        error_event = None
        try:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                if first_byte_s is None:
                    first_byte_s = round(time.monotonic() - started, 2)
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                if "error" in ev:
                    error_event = self.scrub.obj(ev["error"])
                    continue
                chunks += 1
                if ev.get("usage"):
                    usage = ev["usage"]
                    usage_chunk_index = chunks
                    usage_chunk_had_choices = bool(ev.get("choices"))
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("extra_content"):
                        extra_paths.add("delta.extra_content")
                    if d.get("content"):
                        content.append(d["content"])
                    for tc in d.get("tool_calls") or []:
                        slot = tool_calls.setdefault(
                            tc.get("index", 0),
                            {"id": None, "name": None, "arguments": "", "_extra": None})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["arguments"] += fn.get("arguments") or ""
                        if tc.get("extra_content"):
                            slot["_extra"] = tc["extra_content"]
                            extra_paths.add("delta.tool_calls[].extra_content")
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        except Exception as e:  # noqa: BLE001 - a broken stream is a result, not a crash
            error_event = {"read_error": self.scrub.text(f"{type(e).__name__}: {e}")}
        text = "".join(content)
        calls = [tool_calls[i] for i in sorted(tool_calls)]
        return {
            "ok": error_event is None, "status": r.status,
            "latency_s": round(time.monotonic() - started, 2),
            "first_byte_s": first_byte_s, "chunks": chunks, "saw_done": saw_done,
            "finish_reason": finish, "content_chars": len(text), "content_head": text[:80],
            "has_marker": TOOL_MARKER in text,
            "usage": usage, "usage_chunk_index": usage_chunk_index,
            "usage_chunk_had_choices": usage_chunk_had_choices,
            "tool_calls": [summarize_tool_call(t) for t in calls],
            "extra_content_paths": sorted(extra_paths),
            "error_event": error_event,
            # in memory only, for the round trip; stripped before saving
            "_text": text, "_calls": calls,
        }


def summarize_tool_call(t: dict) -> dict:
    extra = t.get("_extra") or {}
    sig = ((extra.get("google") or {}).get("thought_signature")
           if isinstance(extra, dict) else None)
    return {"has_id": bool(t.get("id")), "name": t.get("name"),
            "arguments": (t.get("arguments") or "")[:200],
            "extra_content_keys": sorted(extra) if isinstance(extra, dict) else [],
            "thought_signature_chars": len(sig) if isinstance(sig, str) else 0}


def summarize_completion(j: dict, status: int, started: float) -> dict:
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    text = msg.get("content") or ""
    tcs = [summarize_tool_call({"id": tc.get("id"),
                                "name": (tc.get("function") or {}).get("name"),
                                "arguments": (tc.get("function") or {}).get("arguments") or "",
                                "_extra": tc.get("extra_content")})
           for tc in msg.get("tool_calls") or []]
    return {"ok": True, "status": status, "latency_s": round(time.monotonic() - started, 2),
            "model_echo": j.get("model"), "finish_reason": ch.get("finish_reason"),
            "content_chars": len(text), "content_head": text[:80],
            "usage": j.get("usage"), "tool_calls": tcs}


# ---------- verdicts (also applied to older raw runs by --rescore) ----------

TRANSIENT_ERRORS = ("TimeoutError", "ConnectionResetError", "ConnectionAbortedError",
                    "IncompleteRead", "RemoteDisconnected", "URLError", "OSError")


def _stream_error_is_transient(ev: dict) -> bool:
    """A broken read (timeout, reset) or an in-stream 429/5xx: the model wasn't testable."""
    if "read_error" in ev:
        return str(ev["read_error"]).split(":")[0] in TRANSIENT_ERRORS
    code = ev.get("code")
    return isinstance(code, int) and (code == 429 or code >= 500)


def verdict(kind: str, r: dict | None) -> tuple[str, str]:
    """Return (status, reason). status is PASS, FAIL, UNAVAILABLE or NOT RUN.

    UNAVAILABLE means the model could not be tested (503, 5xx, timeout, 429), which
    says nothing about its capabilities. FAIL means it answered and the answer was wrong.
    """
    if r is None:
        return "NOT RUN", ""
    if not r.get("ok"):
        st = r.get("status")
        ev = r.get("error_event")
        if ev:  # 200 headers, then an error event or a broken read
            reason = "stream broke: " + json.dumps(ev)[:80]
            return ("UNAVAILABLE" if _stream_error_is_transient(ev) else "FAIL"), reason
        if st is None:
            return "UNAVAILABLE", (r.get("error") or "no response").split(":")[0]
        if st == 429 or st >= 500:
            return "UNAVAILABLE", str(st)
        return "FAIL", f"HTTP {st}"
    problems = []
    streamed = kind != "nonstream"
    if streamed and not r.get("saw_done"):
        problems.append("no [DONE]")
    finish = r.get("finish_reason")
    if not finish:
        problems.append("no finish_reason")
    elif finish not in ("stop", "tool_calls"):  # "length", "content_filter", ...
        problems.append(f"finish_reason {finish!r}")
    calls = r.get("tool_calls") or []
    if kind == "tools":
        if not calls:
            problems.append("no tool call")
        else:
            t = calls[0]
            if not t.get("has_id"):
                problems.append("tool call has no id")
            if t.get("name") != TOOL["function"]["name"]:
                problems.append(f"wrong tool name {t.get('name')!r}")
            try:
                args = json.loads(t.get("arguments") or "")
            except ValueError:
                args = None
            city = args.get("city") if isinstance(args, dict) else None
            if not isinstance(city, str) or city.strip().lower() != TOOL_CITY.lower():
                problems.append(f"'city' is not {TOOL_CITY!r}")
    else:
        if not r.get("content_chars"):
            problems.append("empty reply")
        if kind == "roundtrip":
            if calls:
                problems.append("called the tool again")
            # The marker exists only in the tool result, so quoting it proves the
            # model read the result rather than answering around it.
            if not r.get("has_marker"):
                problems.append(f"reply doesn't quote {TOOL_MARKER}")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", ""


def experiment_verdict(e: dict) -> tuple[str, str]:
    """Verdict for a handoff experiment; `source_signature_chars` is the replayed call's."""
    if e.get("skipped"):
        return "NOT RUN", e["skipped"]
    r = e.get("result") or {}
    v = verdict("roundtrip", r)
    if v[0] == "FAIL" and r.get("status") == 400:
        body = r.get("body")
        err = body[0] if isinstance(body, list) and body else body
        msg = err.get("error", {}).get("message", "") if isinstance(err, dict) else str(err)
        v = ("FAIL", f"HTTP 400: {msg[:160]}")
    if not e.get("source_signature_chars") and e["signature"] in ("drop", "keep"):
        # Nothing was removed (or nothing was carried over), so the trial can't show
        # whether the signature matters.
        return "INCONCLUSIVE", f"source call had no signature; reply {v[0]}"
    return v


# ---------- model discovery ----------

def norm(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9.]+", " ", name.lower()).split()


def key_of(name: str) -> str:
    return " ".join(norm(name))


def is_suffix_word(w: str) -> bool:
    # dates/versions like "05-20" or "001"; mixture-of-experts tags like "a4b"
    return (w in SUFFIX_OK or bool(re.fullmatch(r"[0-9]{2,}([.\-][0-9]+)*", w))
            or bool(re.fullmatch(r"a[0-9]+b", w)))


def match_display(target: str, models: list[dict]) -> tuple[dict | None, list[dict]]:
    """Exact display-name match first, then target + harmless suffix words.

    Returns (chosen, all_candidates). `chosen` is None if there is no match or the
    match is ambiguous, so a wrong ID is never guessed silently.
    """
    t = norm(target)
    exact, suffixed = [], []
    for m in models:
        d = norm(m.get("displayName", ""))
        if d == t:
            exact.append(m)
        elif d[:len(t)] == t and all(is_suffix_word(w) for w in d[len(t):]):
            suffixed.append(m)
    pool = exact or suffixed
    if len(pool) == 1:
        return pool[0], exact + suffixed
    return None, exact + suffixed


def list_native(client: Client) -> tuple[list[dict], list[dict]]:
    models, pages, token = [], [], ""
    for _ in range(10):
        url = BASE + "/models?pageSize=1000" + (f"&pageToken={token}" if token else "")
        res = client.get_json(url, openai=False)
        pages.append({k: v for k, v in res.items() if k != "json"})
        if not res["ok"]:
            break
        models += res["json"].get("models", [])
        token = res["json"].get("nextPageToken", "")
        if not token:
            break
    return models, pages


# ---------- probe ----------

def stream_body(model_id: str, messages: list, **extra) -> dict:
    return {"model": model_id, "max_tokens": 1024, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}, **extra}


def roundtrip_body(reply_model: str, tool_result: dict, signature: str) -> dict:
    """Second turn of a tool loop: the assistant's tool call plus the tool's result."""
    calls = []
    for c in tool_result["_calls"]:
        tc = {"id": c["id"], "type": "function",
              "function": {"name": c["name"], "arguments": c["arguments"]}}
        if signature == "keep" and c.get("_extra"):
            tc["extra_content"] = c["_extra"]
        elif signature == "dummy":
            tc["extra_content"] = {"google": {"thought_signature": DUMMY_SIGNATURE}}
        calls.append(tc)
    messages = [
        {"role": "user", "content": TOOL_PROMPT},
        {"role": "assistant", "content": tool_result["_text"] or None, "tool_calls": calls},
        *({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(TOOL_RESULT)}
          for c in tool_result["_calls"]),
    ]
    return stream_body(reply_model, messages, tools=[TOOL])


def probe_model(client: Client, model_id: str, checks: set[str]) -> dict:
    plain = [{"role": "user", "content": "Reply with the single word: ok"}]
    out: dict = {}
    if "nonstream" in checks:
        out["nonstream"] = client.chat(f"{model_id} nonstream",
                                       {"model": model_id, "max_tokens": 1024,
                                        "messages": plain})
    if "stream" in checks:
        out["stream"] = client.chat(f"{model_id} stream", stream_body(model_id, plain))
    if "tools" in checks or "roundtrip" in checks:
        # Streamed, because Zoo Code and Copilot Chat stream tool calls.
        out["tools"] = client.chat(f"{model_id} tools", stream_body(
            model_id, [{"role": "user", "content": TOOL_PROMPT}],
            tools=[TOOL], tool_choice="auto"))
    if "roundtrip" in checks and verdict("tools", out["tools"])[0] == "PASS":
        out["roundtrip"] = client.chat(f"{model_id} roundtrip",
                                       roundtrip_body(model_id, out["tools"], "keep"))
    return out


def run_experiments(client: Client, mapping: list[dict], probes: dict) -> list[dict]:
    ids = {key_of(m["display"]): m["id"] for m in mapping}
    results = []
    for name, call_disp, reply_disp, sig in EXPERIMENTS:
        call_id, reply_id = ids.get(key_of(call_disp)), ids.get(key_of(reply_disp))
        tools = (probes.get(call_id) or {}).get("tools")
        entry = {"name": name, "call_model": call_id, "reply_model": reply_id,
                 "signature": sig}
        if not reply_id or verdict("tools", tools)[0] != "PASS":
            entry["skipped"] = f"no passing tool call from {call_id} in this run"
        else:
            calls = tools.get("tool_calls") or []
            entry["source_signature_chars"] = calls[0].get("thought_signature_chars", 0)
            entry["result"] = client.chat(f"experiment: {name}",
                                          roundtrip_body(reply_id, tools, sig))
            print(f"    {name}: {short(experiment_verdict(entry))}")
        results.append(entry)
    return results


def _wire_call(c: dict, signature: str) -> dict:
    """An assistant tool call as sent back upstream, with its signature treated as asked."""
    tc = {"id": c["id"], "type": "function",
          "function": {"name": c["name"], "arguments": c["arguments"]}}
    if signature == "keep" and c.get("_extra"):
        tc["extra_content"] = c["_extra"]
    elif signature == "dummy":
        tc["extra_content"] = {"google": {"thought_signature": DUMMY_SIGNATURE}}
    return tc


def handoff_messages(source: dict, signature: str, own: dict | None = None) -> list:
    """The handed-off turn: the source's call 1 (signature as asked) and its result; with
    `own`, also the target's call 2, signature as received, and its result."""
    msgs = [
        {"role": "user", "content": HANDOFF_PROMPT},
        {"role": "assistant", "content": source["_text"] or None,
         "tool_calls": [_wire_call(c, signature) for c in source["_calls"]]},
        *({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(TOOL_RESULT)}
          for c in source["_calls"]),
    ]
    if own is not None:
        msgs.append({"role": "assistant", "content": own["_text"] or None,
                     "tool_calls": [_wire_call(c, "keep") for c in own["_calls"]]})
        msgs += [{"role": "tool", "tool_call_id": c["id"], "content": json.dumps(FORECAST_RESULT)}
                 for c in own["_calls"]]
    return msgs


def _call_problems(r: dict, name: str, arg: str, value: str) -> list[str]:
    calls = r.get("tool_calls") or []
    if len(calls) != 1:
        return [f"{len(calls)} tool calls, expected 1"]
    t = calls[0]
    out = []
    if not t.get("has_id"):
        out.append("tool call has no id")
    if t.get("name") != name:
        out.append(f"called {t.get('name')!r}, expected {name!r}")
    try:
        args = json.loads(t.get("arguments") or "")
    except ValueError:
        args = None
    got = args.get(arg) if isinstance(args, dict) else None
    if not isinstance(got, str) or got.strip().lower() != value.lower():
        out.append(f"{arg!r} is not {value!r}")
    return out


def _http_fail(r: dict, step: str) -> tuple[str, str]:
    body = r.get("body")
    err = body[0] if isinstance(body, list) and body else body
    msg = err.get("error", {}).get("message", "") if isinstance(err, dict) else str(err or "")
    return "FAIL", f"{step}: HTTP {r.get('status')}" + (f": {msg[:160]}" if msg else "")


def _call_stream_verdict(r: dict | None) -> tuple[str, str]:
    """verdict("stream") for a reply that should be a tool call, which may carry no text."""
    v = verdict("stream", r)
    return ("PASS", "") if v == ("FAIL", "empty reply") else v


def handoff_source_verdict(r: dict | None) -> tuple[str, str]:
    v = _call_stream_verdict(r)  # not "tools": it checks get_weather's city the same way below
    if v[0] != "PASS":
        return v
    problems = _call_problems(r, "get_weather", "city", TOOL_CITY)
    return ("FAIL", "; ".join(problems)) if problems else ("PASS", "")


def handoff_verdict(e: dict) -> tuple[str, str]:
    """PASS: no 400, the target made call 2 itself (step 2), then answered from both
    results without another call (step 3)."""
    if e.get("skipped"):
        return "NOT RUN", e["skipped"]
    for step, key in (("step 2", "step2"), ("step 3", "step3")):
        r = e.get(key)
        if r is None:
            return "NOT RUN", f"{step} not sent"
        v = _call_stream_verdict(r) if key == "step2" else verdict("stream", r)
        if v[0] == "UNAVAILABLE":
            return "UNAVAILABLE", f"{step}: {v[1]}"
        if not r.get("ok") and r.get("status"):
            return _http_fail(r, step)
        if v[0] != "PASS":
            return "FAIL", f"{step}: {v[1]}"
        if key == "step2":
            problems = _call_problems(r, "get_forecast", "report_id", TOOL_MARKER)
            if problems:
                return "FAIL", "step 2: " + "; ".join(problems)
    r3 = e["step3"]
    if r3.get("tool_calls"):
        return "FAIL", "step 3: called a tool again"
    if not e.get("step3_has_marker"):
        return "FAIL", f"step 3: reply doesn't quote {FORECAST_MARKER}"
    return "PASS", ""


def run_handoffs(client: Client, mapping: list[dict], wanted: set[str],
                 signatures: tuple[str, ...], run: dict, save) -> None:
    """One source call, then for each target and signature mode at most two requests."""
    ids = {key_of(m["display"]): m["id"] for m in mapping}
    source_id = ids.get(key_of(HANDOFF_SOURCE))
    if not source_id:
        print(f"  {HANDOFF_SOURCE} has no confirmed ID; nothing sent")
        return
    tools = [TOOL, FORECAST_TOOL]
    source = client.chat(f"handoff source {source_id}", stream_body(
        source_id, [{"role": "user", "content": HANDOFF_PROMPT}], tools=tools, tool_choice="auto"))
    sv = handoff_source_verdict(source)
    calls = source.get("tool_calls") or []
    run["handoff_source"] = {"model": source_id, "result": source, "verdict": list(sv),
                             "signature_chars": calls[0].get("thought_signature_chars", 0)
                             if calls else 0}
    print(f"    source {source_id}: {short(sv)}")
    save()
    if sv[0] != "PASS":
        return
    for disp in HANDOFF_TARGETS:
        if wanted and key_of(disp) not in wanted:
            continue
        target = ids.get(key_of(disp))
        for sig in signatures:
            entry: dict = {"target": target, "display": disp, "signature": sig,
                           "source_model": source_id}
            run["handoffs"].append(entry)
            if not target:
                entry["skipped"] = "no confirmed model ID"
                continue
            label = f"handoff {sig} -> {target}"
            entry["step2"] = client.chat(label + " step 2", stream_body(
                target, handoff_messages(source, sig), tools=tools))
            s2 = entry["step2"]
            if (_call_stream_verdict(s2)[0] == "PASS"
                    and not _call_problems(s2, "get_forecast", "report_id", TOOL_MARKER)):
                entry["step3"] = client.chat(label + " step 3", stream_body(
                    target, handoff_messages(source, sig, own=s2), tools=tools))
                entry["step3_has_marker"] = FORECAST_MARKER in (entry["step3"].get("_text") or "")
            print(f"    {disp} ({sig}): {short(handoff_verdict(entry))}", flush=True)
            save()


def burst(client: Client, model_id: str) -> dict:
    """Send BURST_COUNT tiny calls at once. Sent once; nothing is retried."""
    body = {"model": model_id, "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply: ok"}]}
    t0 = time.monotonic()

    def one(i: int) -> dict:
        r = client.chat(f"burst {model_id} #{i + 1}", body)
        return {"n": i + 1, "status": r.get("status"), "t_s": round(time.monotonic() - t0, 2)}

    with ThreadPoolExecutor(max_workers=BURST_COUNT) as pool:
        results = sorted(pool.map(one, range(BURST_COUNT)), key=lambda x: x["t_s"])
    return {"model": model_id, "sent": len(results), "parallel": True, "results": results,
            "got_429": any(x["status"] == 429 for x in results)}


# ---------- report ----------

def fmt_usage(u: dict | None) -> str:
    if not u:
        return "none"
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    parts = [f"{k}={u[k]}" for k in keys if k in u]
    if all(k in u for k in keys):
        hidden = u["total_tokens"] - u["prompt_tokens"] - u["completion_tokens"]
        if hidden:
            parts.append(f"(unlisted {hidden})")
    extra = sorted(set(u) - set(keys))
    if extra:
        parts.append("+" + ",".join(extra))
    return " ".join(parts)


def short(v: tuple[str, str]) -> str:
    return v[0] + (f" ({v[1]})" if v[1] else "")


def latest(runs: list[dict], model_id: str, kind: str) -> tuple[dict | None, list[str]]:
    """Newest result for (model, check), plus the verdicts of earlier runs."""
    found = []
    for run in runs:  # oldest first
        r = (run.get("probes", {}).get(model_id) or {}).get(kind)
        if kind == "tools" and r is None:  # first run called it tools_stream
            r = (run.get("probes", {}).get(model_id) or {}).get("tools_stream")
        if r is not None:
            found.append((run["started_pt"], r))
    if not found:
        return None, []
    return found[-1][1], [f"{verdict(kind, r)[0]} {t[11:16]}" for t, r in found[:-1]]


def write_report(path: Path, runs: list[dict]) -> None:
    runs = sorted(runs, key=lambda r: r["started_pt"])
    mapping = next(r["mapping"] for r in reversed(runs) if r.get("mapping"))
    L = ["# Phase 0 probe results\n"]
    L.append("Generated by `scripts/probe.py` from the runs below (times are "
             "America/Los_Angeles). Keys, project numbers and thought signatures "
             "are not stored.\n")
    for r in runs:
        L.append(f"- {r['started_pt']}: {r.get('requests_sent', '?')} requests")
    L.append("\nVerdicts: **PASS** works; **FAIL** answered wrongly; **UNAVAILABLE** "
             "could not be tested (503, timeout or 429), which says nothing about "
             "capability; **INCONCLUSIVE** ran but can't answer the question asked "
             "(e.g. removing a signature from a call that had none). A round trip passes "
             f"only if the reply quotes `{TOOL_MARKER}`, a value that appears only in the "
             "tool result. Each cell is the newest result; older ones follow it.\n")

    L.append("## Model IDs\n")
    L.append("| Display name | Use | Model ID | Input limit | Output limit | Other matches |")
    L.append("|---|---|---|---|---|---|")
    for m in mapping:
        others = ", ".join(f"`{c}`" for c in m["candidates"] if c != m["id"])
        mid = f"`{m['id']}`" if m["id"] else "**unconfirmed**"
        L.append(f"| {m['display']} | {m['use']} | {mid} | {m.get('input_limit') or ''} "
                 f"| {m.get('output_limit') or ''} | {others} |")
    L.append(f"\nOpenAI-compatible `GET /models`: {runs[-1]['openai_models']['summary']}.\n")

    L.append("## Capabilities\n")
    L.append("Tool calls and round trips are streamed. A round trip sends the tool result "
             "back to the same model, with its thought signature.\n")
    L.append("| Model ID | Non-stream | Stream | Tool call | Thought signature | Round trip |")
    L.append("|---|---|---|---|---|---|")
    for m in mapping:
        if not m["id"]:
            continue
        cells = []
        for kind in CHECKS:
            r, older = latest(runs, m["id"], kind)
            c = short(verdict(kind, r))
            if older:
                c += "; earlier " + ", ".join(older)
            cells.append(c)
            if kind == "tools":
                calls = (r or {}).get("tool_calls") or []
                if not calls or "thought_signature_chars" not in calls[0]:
                    cells.append("not recorded" if r and r.get("ok") and calls else "n/a")
                else:
                    n = calls[0]["thought_signature_chars"]
                    cells.append(f"yes ({n} chars)" if n else "none")
        L.append(f"| `{m['id']}` | " + " | ".join(cells) + " |")

    L.append("\n### Usage fields\n")
    L.append("`unlisted` = total_tokens − prompt_tokens − completion_tokens "
             "(reasoning tokens that are not reported separately).\n")
    for m in mapping:
        if not m["id"]:
            continue
        ns, _ = latest(runs, m["id"], "nonstream")
        st, _ = latest(runs, m["id"], "stream")
        if ns or st:
            L.append(f"- `{m['id']}`: non-stream {fmt_usage((ns or {}).get('usage'))}; "
                     f"stream {fmt_usage((st or {}).get('usage'))}")
    sample = next((r["usage_chunk_had_choices"] for run in runs
                   for p in run.get("probes", {}).values()
                   for r in p.values() if r and r.get("usage_chunk_index")), None)
    if sample is not None:
        L.append("\nIn streams, `usage` arrives " + (
            "on the last content chunk (it still has `choices`)." if sample else
            "in a separate final chunk with empty `choices`."))

    exp = [(r["started_pt"], e) for r in runs for e in r.get("experiments") or []]
    if exp:
        L.append("\n## Tool-loop experiments\n")
        L.append("| Run | Experiment | Tool call from | Result sent to | Signature | Verdict |")
        L.append("|---|---|---|---|---|---|")
        names = {"keep": "as received", "drop": "removed", "dummy": "placeholder"}
        for t, e in exp:
            v = short(experiment_verdict(e))
            L.append(f"| {t[11:16]} | {e['name']} | `{e['call_model']}` | `{e['reply_model']}` "
                     f"| {names.get(e['signature'], e['signature'])} | {v} |")

    hand = [(r["started_pt"], r) for r in runs if r.get("handoff_source")]
    if hand:
        L.append("\n## Multi-step handoffs (DESIGN §16.8)\n")
        L.append("The source makes tool call 1. The target gets call 1 and its result (signature "
                 "as shown), must make call 2 itself (step 2), then answer from both results "
                 f"(step 3, quoting `{FORECAST_MARKER}`). Step 3 is where Google checks the "
                 "source's signature in a turn that also holds the target's own call. Only the "
                 "placeholder decides `handoff_targets`; as received is information only.\n")
        L.append("| Run | Source | Target | Signature | Verdict |")
        L.append("|---|---|---|---|---|")
        names = {"keep": "as received", "dummy": "placeholder"}
        for t, r in hand:
            src = r["handoff_source"]
            if src["verdict"][0] != "PASS":
                L.append(f"| {t[11:16]} | `{src['model']}` | (none sent) | | "
                         f"source {short(tuple(src['verdict']))} |")
            for e in r.get("handoffs") or []:
                L.append(f"| {t[11:16]} | `{e['source_model']}` | `{e['target']}` "
                         f"| {names.get(e['signature'], e['signature'])} "
                         f"| {short(handoff_verdict(e))} |")

    L.append("\n## Per-minute burst\n")
    bursts = [(r["started_pt"], r["burst"]) for r in runs if r.get("burst")]
    if not bursts:
        L.append("Not run.")
    for t, b in bursts:
        mode = "in parallel" if b.get("parallel") else "one after another"
        L.append(f"- {t}: `{b['model']}`, {b['sent']} calls {mode}, 429 received: "
                 f"{'yes' if b['got_429'] else 'no'}. Statuses: "
                 + ", ".join(f"#{x['n']}={x['status']}@{x['t_s']}s" for x in b["results"]))

    L.append("\n## 429 samples\n")
    samples = [e for r in runs for e in r.get("errors_429") or []]
    if not samples:
        L.append("None captured.")
    shown = set()
    for e in samples:
        sig = json.dumps(e["body"], sort_keys=True)
        if sig in shown:  # parallel bursts produce identical bodies
            continue
        shown.add(sig)
        n = sum(1 for x in samples if json.dumps(x["body"], sort_keys=True) == sig)
        L.append(f"### {e['label']} ({e['at_pt']})" + (f", {n} identical" if n > 1 else "") + "\n")
        L.append("Headers: `" + json.dumps(e["headers"]) + "`\n")
        body = e["body"] if isinstance(e["body"], str) else json.dumps(e["body"], indent=2)
        L.append("```json\n" + body + "\n```\n")

    L.append("## Failures and unavailable results\n")
    any_bad = False
    for run in runs:
        for mid, p in run.get("probes", {}).items():
            for kind, r in p.items():
                kind = "tools" if kind == "tools_stream" else kind
                v = verdict(kind, r)
                if v[0] in ("PASS", "NOT RUN") or (r and r.get("status") == 429):
                    continue
                any_bad = True
                detail = r.get("body") or r.get("error") or r.get("error_event") or v[1]
                detail = detail if isinstance(detail, str) else json.dumps(detail)
                L.append(f"- {run['started_pt'][11:16]} `{mid}` {kind}: {v[0]}: `{detail[:240]}`")
    if not any_bad:
        L.append("None.")
    if any(isinstance(e["body"], list) for e in samples) or any(
            isinstance(r.get("body"), list) for run in runs
            for p in run.get("probes", {}).values() for r in p.values() if r):
        L.append("\nNote: some error bodies are a JSON **array** (`[{\"error\": ...}]`), "
                 "not an object.")
    out = "\n".join(L) + "\n"
    # Hand-written results (e.g. the Zoo Code check) live after MANUAL_HEADING and
    # survive every regeneration.
    old = path.read_text() if path.exists() else ""
    if MANUAL_HEADING in old:
        out += "\n" + old[old.index(MANUAL_HEADING):]
    path.write_text(out)


def load_runs() -> list[dict]:
    runs = []
    for f in sorted(DATA_DIR.glob("*.json")):
        r = json.loads(f.read_text())
        if r.get("probes") or r.get("burst") or r.get("handoff_source"):  # skip list-only runs
            runs.append(r)
    return runs


# ---------- main ----------

def parse_overrides(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        name, sep, mid = it.partition("=")
        if not sep or not mid:
            sys.exit(f"--id expects 'Display Name=model-id', got: {it!r}")
        out[key_of(name)] = mid.removeprefix("models/")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list-only", action="store_true",
                    help="only call models.list and print the mapping (no generation quota)")
    ap.add_argument("--rescore", action="store_true",
                    help="rebuild the report from data/probe/*.json; sends no requests")
    ap.add_argument("--no-burst", action="store_true", help="skip the per-minute 429 burst")
    ap.add_argument("--no-experiments", action="store_true", help="skip tool-loop handoffs")
    ap.add_argument("--handoffs", action="store_true",
                    help="run only the multi-step handoff probe (DESIGN §16.8); --only limits "
                         "the targets")
    ap.add_argument("--signatures", default=",".join(HANDOFF_SIGNATURES),
                    help="with --handoffs: dummy (placeholder), keep (as received), or both")
    ap.add_argument("--only", action="append", default=[], metavar="DISPLAY_NAME",
                    help="probe only these inventory entries (repeatable)")
    ap.add_argument("--checks", default=",".join(CHECKS),
                    help=f"comma-separated subset of {','.join(CHECKS)}")
    ap.add_argument("--id", action="append", default=[], metavar="'NAME=ID'",
                    help="override the model ID for a display name (repeatable)")
    ap.add_argument("--report", default=str(ROOT / "docs" / "probe-results.md"))
    args = ap.parse_args()

    checks = {c.strip() for c in args.checks.split(",") if c.strip()}
    if not checks <= set(CHECKS):
        sys.exit(f"--checks: unknown {sorted(checks - set(CHECKS))}")
    known = {key_of(d) for d, _ in INVENTORY}
    wanted = {key_of(n) for n in args.only}
    if wanted - known:
        sys.exit(f"--only: not in the inventory: {sorted(wanted - known)}")
    signatures = tuple(s.strip() for s in args.signatures.split(",") if s.strip())
    if not signatures or not set(signatures) <= set(HANDOFF_SIGNATURES):
        sys.exit(f"--signatures: use a subset of {','.join(HANDOFF_SIGNATURES)}")

    if args.rescore:
        runs = load_runs()
        if not runs:
            sys.exit(f"no probe runs in {DATA_DIR}")
        write_report(Path(args.report), runs)
        print(f"report: {args.report} (from {len(runs)} runs, no requests sent)")
        return 0

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        sys.exit("GEMINI_API_KEY is not set. Run: set -a; . ./.env; set +a")
    scrub = Scrubber(key)
    client = Client(key, scrub)
    overrides = parse_overrides(args.id)
    started = dt.datetime.now(PT)
    print(f"Probe start {started.isoformat(timespec='seconds')} PT")

    native, pages = list_native(client)
    if not native:
        print("models.list failed:", json.dumps(scrub.obj(pages), indent=2))
        return 1
    gen = [m for m in native if "generateContent" in m.get("supportedGenerationMethods", [])]
    oa = client.get_json(OPENAI_BASE + "/models", openai=True)
    oa_ids = sorted(m.get("id", "") for m in (oa.get("json") or {}).get("data", []))
    oa_summary = (f"{len(oa_ids)} models" if oa["ok"] else f"failed with status {oa.get('status')}")

    mapping = []
    for display, use in INVENTORY:
        chosen, cands = match_display(display, gen)
        mid = overrides.get(key_of(display))
        if mid is None and chosen:
            mid = chosen["name"].removeprefix("models/")
        src = next((m for m in gen if m["name"].removeprefix("models/") == mid), {}) if mid else {}
        if mid and oa["ok"] and not any(i.removeprefix("models/") == mid for i in oa_ids):
            print(f"  note: {mid} is not in the OpenAI-compatible model list")
        mapping.append({"display": display, "use": use, "id": mid,
                        "candidates": [c["name"].removeprefix("models/") for c in cands],
                        "input_limit": src.get("inputTokenLimit"),
                        "output_limit": src.get("outputTokenLimit")})
        print(f"  {display:24} -> {mid or 'UNCONFIRMED'}"
              + (f"   (matches: {', '.join(mapping[-1]['candidates'])})"
                 if len(mapping[-1]["candidates"]) > 1 or not mid else ""))

    run = {"started_pt": started.isoformat(timespec="seconds"), "mapping": mapping,
           "checks": sorted(checks),
           "openai_models": {"summary": oa_summary, "ids": oa_ids},
           "native_models": [{k: m.get(k) for k in ("name", "displayName", "version",
                              "inputTokenLimit", "outputTokenLimit",
                              "supportedGenerationMethods")} for m in native],
           "probes": {}, "experiments": [], "burst": None, "handoffs": []}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = DATA_DIR / (started.strftime("%Y%m%dT%H%M%S") + ".json")

    def save() -> None:
        """Write what we have so far, so a crash or Ctrl-C doesn't lose spent quota."""
        run["requests_sent"] = client.count
        run["errors_429"] = list(client.errors_429)
        clean = scrub.obj(strip_private(run))  # nothing leaves this process unscrubbed
        tmp = raw_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2))
        tmp.replace(raw_path)

    if args.list_only:
        print("\nAll generateContent models (display name -> id):")
        for m in sorted(gen, key=lambda m: m.get("displayName", "")):
            print(f"  {m.get('displayName', ''):40} {m['name'].removeprefix('models/')}")
    elif args.handoffs:
        try:
            print("multi-step handoffs ...", flush=True)
            run_handoffs(client, mapping, wanted, signatures, run, save)
        finally:
            run["interrupted"] = sys.exc_info()[0] is not None
            save()
            print(f"\nraw: {raw_path}" + (" (partial)" if run["interrupted"] else ""))
    else:
        try:
            for m in mapping:
                if not m["id"] or (wanted and key_of(m["display"]) not in wanted):
                    continue
                print(f"probing {m['id']} ...", flush=True)
                p = run["probes"][m["id"]] = probe_model(client, m["id"], checks)
                print("    " + "  ".join(f"{k}={short(verdict(k, v))}" for k, v in p.items()))
                save()
            if not args.no_experiments and "roundtrip" in checks:
                print("tool-loop experiments ...", flush=True)
                run["experiments"] = run_experiments(client, mapping, run["probes"])
                save()
            burst_id = next((m["id"] for m in mapping if m["display"] == BURST_MODEL), None)
            if not args.no_burst and burst_id and (not wanted or key_of(BURST_MODEL) in wanted):
                print(f"burst: {BURST_COUNT} parallel calls on {burst_id} ...", flush=True)
                run["burst"] = burst(client, burst_id)
                print(f"    got_429={run['burst']['got_429']}")
        finally:
            run["interrupted"] = sys.exc_info()[0] is not None
            save()
            print(f"\nraw: {raw_path}" + (" (partial)" if run["interrupted"] else ""))

    save()
    if args.list_only:
        print(f"\nraw: {raw_path}")
    if not args.list_only:
        write_report(Path(args.report), load_runs())
        print(f"report: {args.report}")
    print(f"requests sent: {client.count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
