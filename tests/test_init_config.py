"""Offline tests for scripts/init_config.py (DESIGN §19.2, §21.7). No network."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import init_config as ic  # noqa: E402

from quota_router.config import ConfigError, load_config  # noqa: E402

KEY = "AIzaSyTEST-key-000000000000000000000000000"


def native(mid, methods=("generateContent",), inp=1048576, out=65536, display=None):
    return {"name": f"models/{mid}", "supportedGenerationMethods": list(methods),
            "inputTokenLimit": inp, "outputTokenLimit": out, "displayName": display or mid}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_for(pages):
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return FakeResp(json.dumps(pages[len(seen) - 1]).encode())
    return opener, seen


def test_list_models_pages_and_keeps_key_out_of_the_url():
    opener, seen = opener_for([{"models": [native("a")], "nextPageToken": "t2"}, {"models": [native("b")]}])
    models = ic.list_models(KEY, opener=opener)
    assert [m["name"] for m in models] == ["models/a", "models/b"]
    assert "pageToken=t2" in seen[1].full_url
    assert all(KEY not in r.full_url and r.get_header("X-goog-api-key") == KEY for r in seen)


def test_generation_models_filters_and_sorts():
    models = ic.generation_models([
        native("gemini-3.5-flash-lite"), native("text-embedding-004", methods=("embedContent",)),
        native("gemini-2-flash"), native("weird id"), native("no-limits", inp=None),
    ])
    assert [m["id"] for m in models] == ["gemini-2-flash", "gemini-3.5-flash-lite"]


def test_rendered_config_needs_fill_me_then_loads(tmp_path):
    models = ic.generation_models([native("gemini-3.5-flash-lite", out=65536), native("gemini-3.1-flash-lite"),
                                   native("gemma-small", inp=32768, out=8192)])
    text = ic.render(models, ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=r"fill in gemini-3\.5-flash-lite\.rpm.*AI Studio"):
        load_config(path)
    path.write_text(text.replace("rpm: FILL_ME", "rpm: 15").replace("tpm_input: FILL_ME", "tpm_input: 250000")
                    .replace("rpd: FILL_ME", "rpd: 500"))
    cfg = load_config(path)
    assert cfg.routes == {"gemini-fast": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]}
    assert cfg.models["gemini-3.5-flash-lite"].output_limit == 65536
    assert cfg.billing == "free"
    assert "gemma-small" not in cfg.models and "#  gemma-small" in text.replace("  #  ", "#  ")


def test_nothing_chosen_writes_only_comments_for_models(tmp_path):
    text = ic.render(ic.generation_models([native("gemini-a")]), [])
    assert "  # gemini-fast:" in text and "gemini-a: {" in text
    assert all(line.lstrip().startswith("#") for line in text.splitlines() if "gemini-a" in line)


def test_read_key_prefers_environment_then_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nexport GEMINI_API_KEY="from-file"  \nPROXY_TOKEN=x\n')
    assert ic.read_key({"GEMINI_API_KEY": "from-env"}, env) == "from-env"
    assert ic.read_key({}, env) == "from-file"
    assert ic.read_key({}, tmp_path / "missing") == ""


def test_main_refuses_to_overwrite_and_scrubs_errors(tmp_path, monkeypatch, capsys):
    out = tmp_path / "config.yaml"
    out.write_text("keep me")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    assert ic.main(["--output", str(out)]) == 2
    assert out.read_text() == "keep me"

    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(f'{{"error": "{KEY}"}}'.encode()))
    monkeypatch.setattr(ic, "_no_redirect_open", fail)
    assert ic.main(["--stdout"]) == 1
    err = capsys.readouterr().err
    assert KEY not in err and "<redacted>" in err


def test_main_writes_file_and_rejects_unknown_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    opener, _ = opener_for([{"models": [native("gemini-a")]}] * 3)
    monkeypatch.setattr(ic, "_no_redirect_open", opener)
    out = tmp_path / "config.yaml"
    assert ic.main(["--output", str(out), "--models", "gemini-b"]) == 2
    assert not out.exists()
    assert ic.main(["--output", str(out), "--models", "gemini-a"]) == 0
    assert "gemini-fast: [gemini-a]" in out.read_text()
    assert KEY not in out.read_text() and KEY not in capsys.readouterr().err


def test_redirects_are_refused_so_the_key_never_follows_one():
    """Second review, finding 4: urllib would copy x-goog-api-key onto the redirected request."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, self.headers.get("x-goog-api-key")))
            if self.path.startswith("/models"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"models": []}')

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        old = ic.BASE
        ic.BASE = f"http://127.0.0.1:{srv.server_port}"
        with pytest.raises(urllib.error.HTTPError, match="redirect"):
            ic.list_models(KEY)
    finally:
        ic.BASE = old
        srv.shutdown()
    assert [p for p, _ in hits] == ["/models?pageSize=1000"]  # /elsewhere never requested
