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


def fill_limits(text):
    return (text.replace("rpm: FILL_ME", "rpm: 5").replace("tpm_input: FILL_ME", "tpm_input: 250000")
            .replace("rpd: FILL_ME", "rpd: 20"))


def test_offered_smart_route_loads_once_uncommented(tmp_path):
    # Every probed model is on the account, plus 3.8 Flash, whose tool call hasn't passed.
    models = ic.generation_models([native(m) for m in ic.SMART_ORDER + ("gemini-3.8-flash",)])
    text = ic.render(models, ["gemini-3.5-flash-lite"])
    smart = "[gemini-4-hydrogen, gemini-3.6-flash, gemini-3-flash-preview, gemini-2.5-flash, " \
            "gemini-3.5-flash-lite, gemini-2.5-flash-lite]"
    assert f"  # gemini-smart: {smart}" in text
    handoff = "[gemini-3-flash-preview, gemini-2.5-flash, gemini-3.5-flash-lite, gemini-2.5-flash-lite]"
    assert f"handoff_targets: []   # with gemini-smart: {handoff}" in text
    # As rendered, the smart route stays off.
    path = tmp_path / "config.yaml"
    path.write_text(fill_limits(text))
    cfg = load_config(path)
    assert list(cfg.routes) == ["gemini-fast"] and cfg.handoff_targets == []
    # Follow the comment: uncomment the smart models and the route, set handoff_targets.
    lines, continuation = [], False
    for line in text.splitlines():
        entry = any(line.startswith(f"  #  {m}: ") for m in ic.SMART_ORDER)
        if entry or (continuation and line.startswith("  #    input_limit")):
            line = line.replace("  #", "", 1)
        continuation = entry
        if line.startswith("  # gemini-smart:"):
            line = "  gemini-smart:" + line.split(":", 1)[1]
        if line.startswith("handoff_targets: []"):
            line = f"handoff_targets: {handoff}"
        lines.append(line)
    path.write_text(fill_limits("\n".join(lines)))
    cfg = load_config(path)
    assert cfg.routes["gemini-smart"] == list(ic.SMART_ORDER)
    assert cfg.handoff_targets == list(ic.HANDOFF_PASSED)
    assert "gemini-3.8-flash" not in cfg.models


def test_smart_route_offers_only_models_on_the_account():
    text = ic.render(ic.generation_models([native("gemini-3.5-flash-lite"), native("gemini-2.5-flash")]), [])
    assert "  # gemini-smart: [gemini-2.5-flash, gemini-3.5-flash-lite]" in text
    assert "with gemini-smart: [gemini-2.5-flash, gemini-3.5-flash-lite]" in text
    text = ic.render(ic.generation_models([native("gemini-a")]), [])
    assert "gemini-smart" not in text and "handoff_targets: []  " in text


def test_example_config_loads_with_the_probed_smart_route():
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    ids = lambda keys: tuple(cfg.models[k].id for k in keys)  # noqa: E731
    assert ids(cfg.routes["gemini-smart"]) == ic.SMART_ORDER
    assert ids(cfg.handoff_targets) == ic.HANDOFF_PASSED
    assert ids(cfg.routes["gemini-fast"]) == ("gemini-3.5-flash-lite",)


def uncomment_auto(text: str, timezone: str = "UTC") -> str:
    """Uncomment the `# auto:` block (DESIGN §16.9.1) and set its timezone."""
    out, inside = [], False
    for line in text.splitlines():
        if line == "# auto:":
            inside = True
        elif inside and not line.startswith("#  "):
            inside = False
        if inside:
            line = line[2:]
            if line.lstrip().startswith("timezone:"):
                line = f'    timezone: "{timezone}"'
        out.append(line)
    return "\n".join(out)


def test_offered_auto_route_loads_once_uncommented(tmp_path):
    models = ic.generation_models([native(m) for m in ic.SMART_ORDER])
    text = ic.render(models, list(ic.SMART_ORDER))  # every model enabled, so only the block is off
    assert "#     smart: [gemini-4-hydrogen, gemini-3.6-flash, gemini-3-flash-preview, gemini-2.5-flash]" in text
    assert "#     fast: [gemini-3.5-flash-lite, gemini-2.5-flash-lite]" in text
    path = tmp_path / "config.yaml"
    path.write_text(fill_limits(text))
    assert load_config(path).auto is None
    path.write_text(fill_limits(uncomment_auto(text, timezone=ic.FILL_ME)))
    with pytest.raises(ConfigError, match="timezone 'FILL_ME' is not a known IANA"):
        load_config(path)
    path.write_text(fill_limits(uncomment_auto(text)))
    cfg = load_config(path)
    assert cfg.routes["gemini-auto"] == list(ic.AUTO_TIERS["smart"] + ic.AUTO_TIERS["fast"])


def test_auto_route_is_offered_only_with_both_tiers():
    text = ic.render(ic.generation_models([native("gemini-3.5-flash-lite")]), [])
    assert "# auto:" not in text


def test_example_config_auto_block_loads_once_uncommented(tmp_path):
    example = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text()
    assert load_config_text(tmp_path, example).auto is None
    cfg = load_config_text(tmp_path, uncomment_auto(example, "America/New_York"))
    ids = lambda keys: tuple(cfg.models[k].id for k in keys)  # noqa: E731
    assert ids(cfg.auto.tiers["smart"]) == ic.AUTO_TIERS["smart"]
    assert ids(cfg.auto.tiers["fast"]) == ic.AUTO_TIERS["fast"]


def load_config_text(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return load_config(path)


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
