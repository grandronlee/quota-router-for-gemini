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
