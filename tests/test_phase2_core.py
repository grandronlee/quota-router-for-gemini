"""Phase 2 changes below the HTTP layer: empty limits, billing, settings, housekeeping, captures."""

from __future__ import annotations

import json
import sqlite3

import pytest

from apphelpers import KEY, TOKEN, Upstream, chat_body, make_proxy, mocked, rate_limited, running, AUTH
from helpers import Env, FailingConn, T0, make_config, model_dict, pt
from quota_router.classify import Classification
from quota_router.config import ConfigError, parse_config
from quota_router.db import SCHEMA, SCHEMA_VERSION, StateDBError, connect
from quota_router.scrub import Scrubber
from quota_router.settings import Settings, SettingsError, load_settings, normalize_host, parse_env_file
from quota_router.upstream import CAPTURE_MAX_FILES


# ---- config: empty limits and billing ---------------------------------------

def test_empty_limits_parse_as_none_but_keys_are_required():
    cfg = make_config({"a": model_dict("model-a", rpm=None, tpm_input=None, rpd=None)})
    m = cfg.models["a"]
    assert (m.rpm, m.tpm_input, m.rpd) == (None, None, None)
    d = model_dict("model-a")
    del d["rpd"]
    with pytest.raises(ConfigError, match="rpd"):
        parse_config({"models": {"a": d}, "routes": {"r": ["a"]}})


def test_billing_paid_is_a_startup_error_and_free_is_default():
    assert make_config().billing == "free"
    with pytest.raises(ConfigError, match="billing: paid is not supported"):
        make_config(billing="paid")
    with pytest.raises(ConfigError):
        make_config(billing="other")


def test_empty_limits_are_not_checked():
    e = Env(make_config({"a": model_dict("model-a", rpm=None, rpd=None, tpm_input=None)}))
    m = e.model("a")
    for _ in range(50):
        assert e.store.try_reserve(m, 400_000).__class__.__name__ == "Reservation"
    assert e.store.check(m, 900_000) is None
    snap = e.store.snapshot()["model-a"]
    assert snap["rpd_used"] == 50 and snap["rpd_limit"] is None and snap["rpm_limit"] is None


def test_tpm_429_still_blocks_a_model_without_a_tpm_limit():
    e = Env(make_config({"a": model_dict("model-a", tpm_input=None)}))
    m = e.model("a")
    e.store.on_rate_limited(m, Classification("tpm", 1.0))
    e.clock.advance(5)  # past the 1 s cooldown, still inside the TPM window
    blocked = e.store.check(m, 10)
    assert blocked is not None and "tpm" in blocked.detail


def test_context_fit_without_tpm_limit_uses_input_limit():
    from quota_router.router import fits
    m = make_config({"a": model_dict("model-a", tpm_input=None, input_limit=1000)}).models["a"]
    assert fits(m, 1000, None) and not fits(m, 1001, None)


@pytest.mark.parametrize("rpm,rpd", [(15, 500), (5, 20), (None, 500), (15, None), (None, None), (50, 500)])
def test_midnight_grace_applies_to_every_model(rpm, rpd):
    # Changed after the Phase 4 review: a 5 RPM / 20 RPD model no longer loses the whole new day.
    e = Env(make_config({"a": model_dict("model-a", rpm=rpm, rpd=rpd)}), start=pt(2026, 11, 21, 0, 0, 5))
    e.store.on_rate_limited(e.model("a"), Classification("rpd", None), sent_at=e.clock.now())
    snap = e.store.snapshot()["model-a"]
    assert snap["exhausted_until"] is None and snap["cooldown_until"] == e.clock.now() + 60


def test_per_day_429_after_the_grace_window_still_exhausts_a_small_model():
    e = Env(make_config({"a": model_dict("model-a", rpm=5, rpd=20)}), start=pt(2026, 11, 21, 0, 10, 0))
    e.store.on_rate_limited(e.model("a"), Classification("rpd", None), sent_at=e.clock.now())
    assert e.store.snapshot()["model-a"]["exhausted_until"] == pt(2026, 11, 22)


def test_daily_429_just_after_midnight_with_no_daily_limit_cools_down():
    e = Env(make_config({"a": model_dict("model-a", rpd=None)}), start=pt(2026, 11, 21, 0, 3))
    m = e.model("a")
    e.store.on_rate_limited(m, Classification("rpd", None), sent_at=e.clock.now())
    snap = e.store.snapshot()["model-a"]
    assert snap["exhausted_until"] is None and snap["cooldown_until"] is not None


# ---- settings -----------------------------------------------------------

def test_settings_reject_missing_and_placeholder_secrets():
    with pytest.raises(SettingsError, match="GEMINI_API_KEY"):
        Settings(gemini_api_key="", proxy_token=TOKEN)
    for bad in ("", "changeme", "short-token", "CHANGEME"):
        with pytest.raises(SettingsError, match="PROXY_TOKEN"):
            Settings(gemini_api_key=KEY, proxy_token=bad)
    with pytest.raises(SettingsError):
        Settings(gemini_api_key=TOKEN, proxy_token=TOKEN)
    assert KEY not in repr(Settings(gemini_api_key=KEY, proxy_token=TOKEN))


def test_env_file_parsing_and_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"# comment\nGEMINI_API_KEY={KEY}\nexport PROXY_TOKEN='{TOKEN}'\n"
                        "PROXY_PORT=4100 # inline\nPROXY_ALLOWED_HOSTS=Box.ts.net:443, [::2]\n")
    s = load_settings({"PROXY_PORT": "4200"}, env_file)
    assert s.gemini_api_key == KEY and s.proxy_token == TOKEN and s.port == 4200
    assert {"box.ts.net", "[::2]", "localhost"} <= s.allowed_hosts
    assert parse_env_file("A=1 # x\nB=\"q # not comment\"")["B"] == "q # not comment"


def test_normalize_host():
    assert normalize_host("LOCALHOST:4000") == "localhost"
    assert normalize_host("[::1]:4000") == "[::1]"
    assert normalize_host("::1") == "[::1]"
    assert normalize_host("127.0.0.1") == "127.0.0.1"


def test_scrubber():
    s = Scrubber([KEY, TOKEN, "short"])
    out = s.text(f"{KEY} {TOKEN} AIzaOTHERKEY_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa short")
    assert KEY not in out and TOKEN not in out and "AIzaOTHER" not in out
    assert out.endswith("short")  # under 8 characters: not treated as a secret
    assert "projects/<redacted>" in s.capture("projects/1234567/x")


# ---- database housekeeping -------------------------------------------------

def test_new_and_phase1_databases_get_the_current_schema_version(tmp_path):
    fresh = connect(tmp_path / "new.sqlite3")
    assert fresh.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
    fresh.close()
    old = sqlite3.connect(tmp_path / "old.sqlite3")
    old.executescript(SCHEMA)
    old.execute("INSERT INTO rpd VALUES ('m', '2026-11-20', 3)")
    old.commit()
    old.close()
    conn = connect(tmp_path / "old.sqlite3")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT count FROM rpd").fetchone()[0] == 3


def test_newer_schema_version_is_a_startup_error(tmp_path):
    path = tmp_path / "q.sqlite3"
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version = 99")
    c.close()
    with pytest.raises(StateDBError, match=r"q\.sqlite3.*version 99.*Deleting it is safe"):
        connect(path)


def test_non_database_file_is_a_startup_error(tmp_path):
    path = tmp_path / "q.sqlite3"
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(StateDBError, match=r"q\.sqlite3.*Deleting it is safe"):
        connect(path)


def test_damaged_database_is_a_startup_error(tmp_path):
    path = tmp_path / "q.sqlite3"
    conn = connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executemany("INSERT INTO affinity (tool_call_id, model_id, created_at) VALUES (?, 'm', 0)", [(f"id-{i:05d}-" + "x" * 200,) for i in range(400)])
    conn.close()
    data = bytearray(path.read_bytes())
    page = 4096
    for off in range(page * 2 + 100, min(len(data), page * 6), 7):  # scribble over interior pages
        data[off] = 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(StateDBError, match=r"q\.sqlite3.*(damaged|not a database).*Deleting it is safe"):
        connect(path)


def test_prune_removes_expired_rows_and_survives_errors():
    p = make_proxy(models={"a": model_dict("model-a")})
    conn = p.conn
    conn.execute("INSERT INTO rpd VALUES ('model-a', '2026-11-01', 5)")
    conn.execute("INSERT INTO rpd VALUES ('model-a', '2026-11-19', 5)")
    conn.execute("INSERT INTO exhausted VALUES ('model-a', ?)", (T0 - 1,))
    conn.execute("INSERT INTO affinity (tool_call_id, model_id, created_at) VALUES ('old', 'model-a', ?)",
                 (T0 - 25 * 3600,))
    conn.execute("INSERT INTO affinity (tool_call_id, model_id, created_at) VALUES ('new', 'model-a', ?)",
                 (T0 - 3600,))
    # T0 is 2026-11-20 PT: rows from 30 days back (2026-10-21) are kept, older ones go.
    for turn_id, day in (("t-old", "2026-10-20"), ("t-edge", "2026-10-21"), ("t-new", "2026-11-20")):
        conn.execute("INSERT INTO turns VALUES (?, ?, 9, 'fast', 'model-a', 'model-a', 'first', 0, "
                     "0, 0, 1, 0, 0, 200, NULL, NULL)", (turn_id, day))
    p.prune_once()
    assert [r[0] for r in conn.execute("SELECT day FROM rpd")] == ["2026-11-19"]
    assert conn.execute("SELECT COUNT(*) FROM exhausted").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT tool_call_id FROM affinity")] == ["new"]
    assert sorted(r[0] for r in conn.execute("SELECT turn_id FROM turns")) == ["t-edge", "t-new"]

    p.store._conn = FailingConn(conn, fail_on="DELETE FROM rpd")
    p.prune_once()  # logged and counted, not raised
    assert p.router.state_errors == 1
    p.store._conn = conn
    p.prune_once()
    assert p.router.state_errors == 1


# ---- 429 captures --------------------------------------------------------

def test_429_bodies_are_captured_scrubbed_and_capped(tmp_path):
    db = str(tmp_path / "data" / "quota.sqlite3")
    cap_dir = tmp_path / "data" / "429"
    cap_dir.mkdir(parents=True)
    for i in range(CAPTURE_MAX_FILES - 1):
        (cap_dir / f"old-{i}.json").write_text("{}")
    leaky = rate_limited("minute")
    body = json.loads(leaky.content)
    body["error"]["message"] = f"quota for {KEY} on projects/987654321"
    import httpx
    up = Upstream({"model-a": [httpx.Response(429, json=body, headers={"retry-after": "7"}),
                               rate_limited("minute")]})
    p = make_proxy(models={"a": model_dict("model-a")}, db=db)
    with mocked(up), running(p) as c:
        post1 = c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
        p.clock.advance(120)
        c.post("/v1/chat/completions", json=chat_body(), headers=AUTH)
    assert post1.status_code == 429
    new = [f for f in cap_dir.glob("*.json") if not f.name.startswith("old-")]
    assert len(new) == 1  # the second 429 hit the cap
    saved = json.loads(new[0].read_text())
    assert saved["model"] == "model-a" and saved["headers"] == {"retry-after": "7"}
    text = new[0].read_text()
    assert KEY not in text and "987654321" not in text


def test_access_problems_are_not_reported_as_damage(tmp_path):
    """Review finding 5: a read-only or unreachable file must not say 'deleting it is safe'."""
    import os
    path = tmp_path / "q.sqlite3"
    connect(path).close()
    for f in tmp_path.iterdir():
        os.chmod(f, 0o444)
    os.chmod(tmp_path, 0o555)
    try:
        with pytest.raises(StateDBError) as e:
            connect(path)
        assert "Check its permissions" in str(e.value) and "Deleting it is safe" not in str(e.value)
    finally:
        os.chmod(tmp_path, 0o755)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(StateDBError, match="cannot open state database"):
        connect(blocker / "sub" / "q.sqlite3")


# ---- pasted review findings (2026-11-21) -------------------------------------

def test_upstream_url_must_be_google_https_unless_opted_in():
    """Finding 1: the API key goes to upstream_base_url."""
    assert make_config().upstream_base_url.startswith("https://generativelanguage.googleapis.com/")
    for bad in ("https://evil.example/v1beta/openai/", "http://generativelanguage.googleapis.com/v1beta/openai/",
                "https://generativelanguage.googleapis.com.evil.example/", "https://generativelanguage.googleapis.com:8443/",
                "https://user:pw@generativelanguage.googleapis.com/", "generativelanguage.googleapis.com/v1beta"):
        with pytest.raises(ConfigError):
            make_config(upstream_base_url=bad)
    assert make_config(upstream_base_url="https://my.gateway.example/v1/", custom_upstream=True)
    assert make_config(upstream_base_url="http://127.0.0.1:8080/v1/", custom_upstream=True)
    with pytest.raises(ConfigError, match="https"):
        make_config(upstream_base_url="http://my.gateway.example/v1/", custom_upstream=True)
    with pytest.raises(ConfigError):
        make_config(upstream_base_url="https://my.gateway.example/v1/?key=x", custom_upstream=True)


def test_log_lines_hide_secrets_that_json_would_escape():
    """Finding 2: a token with characters JSON escapes must still be removed."""
    import logging
    from quota_router.logs import JsonFormatter
    token = 'tok"en\\with-quote-0123456789'
    fmt = JsonFormatter(Scrubber([KEY, token]))
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "token was %s", (token,), None)
    line = fmt.format(rec)
    assert token not in json.loads(line)["msg"] and "<redacted>" in json.loads(line)["msg"]
    rec2 = logging.LogRecord("quota_router.request", logging.INFO, __file__, 1, "request", None, None)
    rec2.fields = {"event": "request", "note": f"a{token}b"}
    assert token not in json.loads(fmt.format(rec2))["note"]


def test_scrubber_bytes_handles_unicode_escaped_keys():
    """Finding 2: Google can escape characters as \\uXXXX."""
    escaped = KEY.replace("A", "\\u0041", 1).encode()
    body = b'{"error": {"message": "bad key ' + escaped + b'"}}'
    out = Scrubber([KEY]).bytes(body)
    assert KEY not in json.loads(out)["error"]["message"]
    assert Scrubber([KEY]).bytes(b"not json " + KEY.encode()) == b"not json <redacted>"


def test_capture_redacts_before_truncating_and_is_owner_only(tmp_path):
    """Finding 5."""
    import os
    import stat
    from quota_router.upstream import CAPTURE_MAX_BYTES, Captures
    cap = Captures(tmp_path / "429", Scrubber([KEY]), lambda: T0)
    body = ("x" * (CAPTURE_MAX_BYTES - 10) + KEY).encode()  # the key crosses the size limit
    cap.save("model-a", 429, {"retry-after": "5"}, body)
    files = list((tmp_path / "429").glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert KEY[:10] not in text
    assert json.loads(text)["truncated"] is False  # the redacted body fits
    assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "429").st_mode) == 0o700


def test_upstream_url_with_whitespace_or_control_characters_is_rejected():
    """Code review nit: urlsplit drops these, httpx then fails every request."""
    for bad in ("https://generativelanguage.googleapis.com\t/v1beta/openai/",
                "https://generativelanguage.googleapis.com\n/v1beta/openai/",
                " https://generativelanguage.googleapis.com/v1beta/openai/"):
        with pytest.raises(ConfigError, match="spaces or control"):
            make_config(upstream_base_url=bad)


def test_proxy_environment_is_ignored_for_plain_http_upstreams():
    """Code review nit: HTTP_PROXY must not see the key in cleartext."""
    import asyncio
    from quota_router.upstream import make_client

    async def check():
        plain, tls = make_client(trust_env=False), make_client()
        try:
            return plain.trust_env, tls.trust_env
        finally:
            await plain.aclose()
            await tls.aclose()

    assert asyncio.run(check()) == (False, True)
    p = make_proxy(models={"a": model_dict("model-a")})
    p2 = make_proxy(models={"a": model_dict("model-a")}, upstream_base_url="http://127.0.0.1:9/v1/")
    with running(p), running(p2):
        assert p.client.trust_env is True and p2.client.trust_env is False


def test_failed_capture_write_leaves_no_partial_file(tmp_path, monkeypatch):
    """Code review nit."""
    import os
    from quota_router.upstream import Captures
    cap = Captures(tmp_path / "429", Scrubber([KEY]), lambda: T0)
    real_fdopen = os.fdopen

    class Broken:
        def __init__(self, fd):
            self.f = real_fdopen(fd, "w")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, _):
            raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Broken(fd))
    cap.save("model-a", 429, {}, b"{}")
    assert list((tmp_path / "429").glob("*")) == []
