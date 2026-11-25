import json
import subprocess
import sys
from pathlib import Path

import pytest

def run_summarize(*args, input_str=None):
    cmd = [sys.executable, "scripts/summarize_log.py"] + list(args)
    result = subprocess.run(
        cmd,
        input=input_str,
        capture_output=True,
        text=True,
        encoding="utf-8"
    )
    return result

def test_basic_summary(tmp_path):
    log_file = tmp_path / "test.jsonl"
    logs = [
        {"logger": "quota_router.request", "route": "/chat", "status": 200, "route_reason": "default", "route_tier": "fast", "route_rule": "none"},
        {"logger": "quota_router.request", "route": "/chat", "status": 200, "route_reason": "default", "route_tier": "fast", "route_rule": "none"},
        {"logger": "quota_router.request", "route": "/chat", "status": 429, "route_reason": "spill", "route_tier": "smart", "route_rule": "none", "retry_after_s": 10},
        {"logger": "other", "msg": "ignore me"}
    ]
    with open(log_file, "w") as f:
        for log in logs:
            f.write(json.dumps(log) + "\n")
    
    res = run_summarize(str(log_file), "--json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    
    assert len(data["routes"]) == 2
    # Sort for stable testing
    data["routes"].sort(key=lambda x: x["route"] + x["reason"])
    
    assert data["routes"][0]["route"] == "/chat"
    assert data["routes"][0]["reason"] == "default"
    assert data["routes"][0]["total"] == 2
    assert data["routes"][0]["2xx"] == 2
    
    assert data["routes"][1]["route"] == "/chat"
    assert data["routes"][1]["reason"] == "spill"
    assert data["routes"][1]["total"] == 1
    assert data["routes"][1]["4xx"] == 1
    
    assert data["statuses"]["200"] == 2
    assert data["statuses"]["429"] == 1
    
    assert data["retry_after"]["429"]["with_retry_after"] == 1
    assert data["retry_after"]["429"]["min"] == 10.0

def test_stdin():
    logs = [
        {"logger": "quota_router.request", "route": "/v1", "status": 200, "route_reason": "none", "route_tier": "none", "route_rule": "none"},
    ]
    input_str = "\n".join(json.dumps(l) for l in logs) + "\n"
    res = run_summarize("-", "--json", input_str=input_str)
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["routes"][0]["route"] == "/v1"

def test_malformed_json(tmp_path):
    log_file = tmp_path / "bad.jsonl"
    with open(log_file, "w") as f:
        f.write('{"logger": "quota_router.request", "status": 200}\n')
        f.write('not json\n')
        f.write('\n')
        f.write('{"logger": "quota_router.request", "status": 500}\n')
    
    res = run_summarize(str(log_file), "--json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["statuses"]["200"] == 1
    assert data["statuses"]["500"] == 1

def test_retry_after_aggregation(tmp_path):
    log_file = tmp_path / "retry.jsonl"
    logs = [
        {"logger": "quota_router.request", "status": 429, "retry_after_s": 5},
        {"logger": "quota_router.request", "status": 429, "retry_after_s": 15},
        {"logger": "quota_router.request", "status": 429}, # missing retry_after
        {"logger": "quota_router.request", "status": 503, "retry_after_s": 30},
    ]
    with open(log_file, "w") as f:
        for log in logs:
            f.write(json.dumps(log) + "\n")
            
    res = run_summarize(str(log_file), "--json")
    data = json.loads(res.stdout)
    
    r429 = data["retry_after"]["429"]
    assert r429["total_errors"] == 3
    assert r429["with_retry_after"] == 2
    assert r429["min"] == 5.0
    assert r429["max"] == 15.0
    assert r429["avg"] == 10.0
    
    r503 = data["retry_after"]["503"]
    assert r503["total_errors"] == 1
    assert r503["with_retry_after"] == 1
    assert r503["min"] == 30.0

def test_top_n(tmp_path):
    log_file = tmp_path / "top.jsonl"
    logs = [
        {"logger": "quota_router.request", "route": "a", "status": 200},
        {"logger": "quota_router.request", "route": "a", "status": 200},
        {"logger": "quota_router.request", "route": "b", "status": 200},
    ]
    with open(log_file, "w") as f:
        for log in logs:
            f.write(json.dumps(log) + "\n")
            
    res = run_summarize(str(log_file), "--json", "--top", "1")
    data = json.loads(res.stdout)
    assert len(data["routes"]) == 1
    assert data["routes"][0]["route"] == "a"

def test_text_output(tmp_path):
    log_file = tmp_path / "text.jsonl"
    logs = [
        {"logger": "quota_router.request", "route": "/chat", "status": 429, "retry_after_s": 10},
    ]
    with open(log_file, "w") as f:
        for log in logs:
            f.write(json.dumps(log) + "\n")
            
    res = run_summarize(str(log_file))
    assert "=== Route Summary ===" in res.stdout
    assert "/chat" in res.stdout
    assert "429: 1" in res.stdout
    assert "Status 429:" in res.stdout
    assert "Delay (min/max/avg): 10.00 / 10.00 / 10.00 seconds" in res.stdout

def test_glob(tmp_path):
    (tmp_path / "log1.jsonl").write_text(json.dumps({"logger": "quota_router.request", "status": 200}))
    (tmp_path / "log2.jsonl").write_text(json.dumps({"logger": "quota_router.request", "status": 200}))
    
    res = run_summarize(str(tmp_path / "*.jsonl"), "--json")
    data = json.loads(res.stdout)
    assert data["statuses"]["200"] == 2


# ---- DESIGN §28: holds, first bytes, stream endings --------------------------

def _req(**fields):
    return {"logger": "quota_router.request", "route": "gemini-auto", "status": 200, **fields}


def _summary(tmp_path, lines, *args):
    log_file = tmp_path / "s28.jsonl"
    log_file.write_text("".join(json.dumps(l) + "\n" for l in lines))
    res = run_summarize(str(log_file), "--json", *args)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


_HOLD = {"model": "m", "limits": ["tpm"], "ms": 100}


@pytest.mark.parametrize("reason,status,outcome", [
    ("served", 200, "served"),
    ("stream_error", 200, "served"),          # a held stream that broke after it started
    ("client_disconnected", 200, "served"),   # its client left after it started
    ("passthrough", 400, "passthrough"),
    ("passthrough", 501, "passthrough"),      # status alone would say error
    ("quota", 429, "refused"),
    ("unavailable", 503, "refused"),
    ("context_length_exceeded", 400, "refused"),  # status alone would say passthrough
    ("model_not_found", 404, "refused"),
    ("no_enabled_model", 503, "refused"),
    ("client_disconnected", 499, "cancelled"),
    ("internal_error", 500, "error"),
    ("something_new", 200, "unknown"),
    ("stream_error", 502, "unknown"),
])
def test_hold_outcome_from_reason_and_status(tmp_path, reason, status, outcome):
    data = _summary(tmp_path, [_req(reason=reason, status=status, hold=_HOLD)])
    assert data["holds"]["outcomes"] == {outcome: 1}


def test_hold_counts_limits_and_wait(tmp_path):
    lines = [
        _req(reason="served", hold={"model": "a", "limits": ["tpm"], "ms": 300}),
        _req(reason="served", hold={"model": "a", "limits": ["rpm", "tpm"], "ms": 100}),
        _req(reason="quota", status=429, hold={"model": "b", "limits": ["rpm"], "ms": 200}),
        _req(reason="served"),  # not held
    ]
    holds = _summary(tmp_path, lines)["holds"]
    assert holds["count"] == 3
    assert holds["ms"] == {"min": 100, "median": 200, "max": 300}
    assert holds["limits"] == {"rpm": 2, "tpm": 2}
    assert holds["outcomes"] == {"refused": 1, "served": 2}
    assert holds["skipped"] == 0


@pytest.mark.parametrize("values,median,p90", [
    ([500], 500, 500),
    ([100, 900], 100, 900),
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 50, 90),
])
def test_first_byte_nearest_rank(tmp_path, values, median, p90):
    lines = [_req(reason="served", served_model="m", first_byte_ms=v) for v in reversed(values)]
    model = _summary(tmp_path, lines)["first_byte"]["models"]["m"]
    assert model == {"count": len(values), "min": min(values), "median": median, "p90": p90,
                     "max": max(values)}


def test_first_byte_per_model_sorted(tmp_path):
    lines = [
        _req(served_model="zeta", first_byte_ms=7),
        _req(served_model="alpha", first_byte_ms=3),
        _req(served_model="alpha", first_byte_ms=None),  # no first byte: not counted, not skipped
        _req(served_model="alpha"),
    ]
    first = _summary(tmp_path, lines)["first_byte"]
    assert list(first["models"]) == ["alpha", "zeta"]
    assert first["models"]["alpha"]["count"] == 1
    assert first["skipped"] == 0


def test_stream_end_counts_and_not_done_list(tmp_path):
    def end(e, fr, **kw):
        return {"end": e, "chunks": 3, "finish_reason": fr, "usage_seen": True, "idle_ms": 0, **kw}
    lines = [
        _req(stream_end=end("done", "stop")),
        _req(stream_end=end("done", "stop")),
        _req(stream_end=end("done", "tool_calls")),
        _req(stream_end=end("eof", None), ts="t1", request_id="r1", served_model="m",
             reason="stream_error"),
        _req(stream_end=end("read_error", "length", error="ReadTimeout"), ts="t2",
             request_id="r2", served_model="m", reason="stream_error"),
    ]
    ends = _summary(tmp_path, lines, "--top", "1")["stream_end"]
    assert ends["counts"] == [
        {"end": "done", "finish_reason": "stop", "count": 2},
        {"end": "done", "finish_reason": "tool_calls", "count": 1},
        {"end": "eof", "finish_reason": "none", "count": 1},
        {"end": "read_error", "finish_reason": "length", "count": 1},
    ]
    assert len(ends["not_done"]) == 2  # --top cuts only the route table
    assert ends["not_done"][0] == {"ts": "t1", "request_id": "r1", "served_model": "m",
                                   "end": "eof", "error": None, "chunks": 3,
                                   "finish_reason": None, "usage_seen": True, "idle_ms": 0}
    assert ends["not_done"][1]["error"] == "ReadTimeout"
    assert ends["skipped"] == 0


def test_old_format_log_gives_empty_sections(tmp_path):
    lines = [_req(route_reason="default", route_tier="fast", route_rule="none"),
             _req(status=429, retry_after_s=5)]
    data = _summary(tmp_path, lines)
    assert data["holds"] == {"count": 0, "ms": None, "limits": {}, "outcomes": {}, "skipped": 0}
    assert data["first_byte"] == {"models": {}, "skipped": 0}
    assert data["stream_end"] == {"counts": [], "not_done": [], "skipped": 0}
    assert sum(r["total"] for r in data["routes"]) == 2
    assert data["retry_after"]["429"]["with_retry_after"] == 1


def test_malformed_values_beside_valid_rows(tmp_path):
    valid = [
        _req(reason="served", served_model="m", first_byte_ms=100,
             hold={"model": "m", "limits": ["tpm"], "ms": 50},
             stream_end={"end": "done", "finish_reason": "stop"}),
        _req(reason="stream_error", served_model="m", first_byte_ms=300,
             stream_end={"end": "eof", "finish_reason": None}),
    ]
    bad = [
        _req(hold="held"),                                         # hold not an object
        _req(hold=["x"]),
        _req(reason="served", hold={"limits": [["tpm"], 3, "rpm"], "ms": True}),  # counted
        _req(stream_end="done"),                                   # not an object
        _req(stream_end={"end": "done", "finish_reason": ["stop"]}),  # list-valued
        _req(stream_end={"end": {"x": 1}, "finish_reason": "stop"}),  # object-valued end
        _req(stream_end={"end": "", "finish_reason": "stop"}),
        _req(served_model=None, first_byte_ms=10),                 # null model
        _req(served_model=7, first_byte_ms=10),                    # numeric model
        _req(served_model="m", first_byte_ms=True),                # boolean
        _req(served_model="m", first_byte_ms="12"),
        [1, 2, 3],                                                 # not an object at all
    ]
    clean = _summary(tmp_path, valid)
    mixed = _summary(tmp_path, valid + bad)

    assert mixed["first_byte"]["models"] == clean["first_byte"]["models"]
    assert mixed["first_byte"]["skipped"] == 4
    assert mixed["stream_end"]["counts"] == clean["stream_end"]["counts"]
    assert mixed["stream_end"]["not_done"] == clean["stream_end"]["not_done"]
    assert mixed["stream_end"]["skipped"] == 4
    # The third bad hold is still counted with its outcome; its boolean ms and bad limits are not.
    assert mixed["holds"]["count"] == 2
    assert mixed["holds"]["outcomes"] == {"served": 2}
    assert mixed["holds"]["ms"] == clean["holds"]["ms"]
    assert mixed["holds"]["limits"] == {"rpm": 1, "tpm": 1}
    assert mixed["holds"]["skipped"] == 2


def test_sections_in_text_output(tmp_path):
    log_file = tmp_path / "t.jsonl"
    lines = [_req(reason="served", served_model="m", first_byte_ms=100, hold=_HOLD,
                  stream_end={"end": "eof", "finish_reason": None}, request_id="r9")]
    log_file.write_text("".join(json.dumps(l) + "\n" for l in lines))
    res = run_summarize(str(log_file))
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert out.index("=== Retry-After Metrics ===") < out.index("=== Holds ===")
    assert "Outcomes: served 1" in out
    assert "m: 1 × min 100 / median 100 / p90 100 / max 100 ms" in out
    assert "eof / none: 1" in out
    assert "request_id=r9" in out


def test_empty_sections_in_text_output(tmp_path):
    log_file = tmp_path / "t.jsonl"
    log_file.write_text(json.dumps(_req()) + "\n")
    out = run_summarize(str(log_file)).stdout
    tail = out[out.index("=== Holds ==="):]
    assert tail.count("none") == 3
