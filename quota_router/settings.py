"""Process settings from the environment and `.env` (DESIGN §10, §20.1).

Secrets live only here, never in config.yaml. Variables already set in the
environment win over `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

MIN_TOKEN_LENGTH = 16
PLACEHOLDERS = {
    "changeme", "change-me", "change_me", "changethis", "placeholder", "token", "secret",
    "password", "your-token", "your_token", "your-token-here", "your_token_here",
    "proxy-token", "proxy_token", "xxx", "todo",
}
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    proxy_token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 4000
    config_path: str = "config.yaml"
    allowed_hosts: frozenset[str] = LOCAL_HOSTS

    def __post_init__(self) -> None:
        if not self.gemini_api_key:
            raise SettingsError("GEMINI_API_KEY is not set (put it in .env or the environment)")
        token = self.proxy_token
        if not token:
            raise SettingsError("PROXY_TOKEN is not set. Generate one with: "
                                "python3 -c 'import secrets;print(secrets.token_urlsafe(32))'")
        if token.lower() in PLACEHOLDERS or len(token) < MIN_TOKEN_LENGTH:
            raise SettingsError(f"PROXY_TOKEN is a placeholder or shorter than {MIN_TOKEN_LENGTH} "
                                "characters; generate a random one")
        if token == self.gemini_api_key:
            raise SettingsError("PROXY_TOKEN must not be the Gemini API key")


def parse_env_file(text: str) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, `#` comments, optional `export`, optional quotes."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def normalize_host(host: str) -> str:
    """Host name in the form used for comparison: lower case, no port, IPv6 in brackets."""
    h = host.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[:end + 1] if end != -1 else h
    if h.count(":") > 1:  # bare IPv6 address
        return f"[{h}]"
    return h.split(":", 1)[0]


def load_settings(environ: Mapping[str, str] | None = None, env_file: str | Path | None = ".env") -> Settings:
    env: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        env.update(parse_env_file(Path(env_file).read_text(encoding="utf-8")))
    env.update(os.environ if environ is None else environ)
    port_text = env.get("PROXY_PORT") or "4000"
    try:
        port = int(port_text)
    except ValueError:
        raise SettingsError(f"PROXY_PORT must be a number, not {port_text!r}") from None
    extra = {normalize_host(h) for h in (env.get("PROXY_ALLOWED_HOSTS") or "").split(",") if h.strip()}
    return Settings(
        gemini_api_key=env.get("GEMINI_API_KEY", "").strip(),
        proxy_token=env.get("PROXY_TOKEN", "").strip(),
        host=env.get("PROXY_HOST") or "127.0.0.1",
        port=port,
        config_path=env.get("QR_CONFIG") or "config.yaml",
        allowed_hosts=LOCAL_HOSTS | frozenset(extra),
    )
