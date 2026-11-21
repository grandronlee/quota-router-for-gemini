"""Load and validate config.yaml (DESIGN §12).

Any problem is a startup error (ConfigError). Secrets never live here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator

GOOGLE_UPSTREAM_HOST = "generativelanguage.googleapis.com"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

PAID_MESSAGE = ("billing: paid is not supported. The proxy only runs on a Gemini API project "
                "without billing, because on a billed project every call it routes is charged "
                "(DESIGN §19.1). Use a key from a project that shows 'Free tier' in AI Studio.")


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelConfig(_Strict):
    key: str = ""  # filled from the mapping key in Config
    id: str = Field(min_length=1)
    # Rate limits must be present; an empty value (YAML null) means "no limit" (DESIGN §12, §20.2).
    rpm: PositiveInt | None
    tpm_input: PositiveInt | None
    rpd: PositiveInt | None
    input_limit: PositiveInt
    output_limit: PositiveInt


class Config(_Strict):
    upstream_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # The API key goes to upstream_base_url. Anything other than Google over HTTPS needs this
    # explicit opt-in, so a typo can't send the key elsewhere (DESIGN §12).
    custom_upstream: bool = False
    request_deadline_s: float = Field(default=180, gt=0)
    max_attempts: PositiveInt = 3
    db_path: str = "data/quota.sqlite3"
    billing: Literal["free", "paid"] = "free"
    max_body_bytes: PositiveInt = 20 * 1024 * 1024
    # Shared by all request bodies in use at once (DESIGN §20.3).
    max_inflight_body_bytes: PositiveInt = 64 * 1024 * 1024
    models: dict[str, ModelConfig]
    routes: dict[str, list[str]]
    handoffs: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fill_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
            models = {}
            for key, m in data["models"].items():
                if isinstance(m, dict):
                    if "key" in m:
                        raise ValueError(f"model {key!r}: 'key' is not a config field")
                    m = {**m, "key": key}
                models[key] = m
            data = {**data, "models": models}
        return data

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if self.billing == "paid":
            raise ValueError(PAID_MESSAGE)
        self._check_upstream()
        if not self.models:
            raise ValueError("no models configured")
        if not self.routes:
            raise ValueError("no routes configured")
        if self.handoffs:
            raise ValueError("handoffs must be empty in v1 (DESIGN §5.1)")
        if self.max_inflight_body_bytes < self.max_body_bytes:
            raise ValueError("max_inflight_body_bytes must be at least max_body_bytes, or no "
                             "request of the largest allowed size could ever be accepted")
        ids: dict[str, str] = {}
        for key, m in self.models.items():
            # Two keys for one real model would each believe they had its full quota.
            if m.id in ids:
                raise ValueError(f"models {ids[m.id]!r} and {key!r} share model ID {m.id!r}")
            ids[m.id] = key
        for route, members in self.routes.items():
            if not members:
                raise ValueError(f"route {route!r} has no models")
            if route in ids:
                raise ValueError(f"route {route!r} has the same name as a model ID")
            unknown = [k for k in members if k not in self.models]
            if unknown:
                raise ValueError(f"route {route!r} uses unknown model(s): {', '.join(unknown)}")
            if len(set(members)) != len(members):
                raise ValueError(f"route {route!r} lists a model twice")
        return self

    def _check_upstream(self) -> None:
        url = self.upstream_base_url
        if url != url.strip() or any(ord(ch) < 0x21 or ch == "\x7f" for ch in url):
            # urlsplit drops tabs and newlines that httpx rejects: catch them at startup.
            raise ValueError("upstream_base_url must not contain spaces or control characters")
        try:
            u = urlsplit(self.upstream_base_url)
            host, port = u.hostname, u.port
        except ValueError as e:
            raise ValueError(f"upstream_base_url is not a valid URL: {e}") from None
        if u.scheme not in ("https", "http") or not host or u.username or u.password or u.query or u.fragment:
            raise ValueError("upstream_base_url must be an http(s) URL with a host and no "
                             "credentials, query or fragment")
        if not self.custom_upstream:
            if u.scheme != "https" or host != GOOGLE_UPSTREAM_HOST or port not in (None, 443):
                raise ValueError(f"upstream_base_url must be https://{GOOGLE_UPSTREAM_HOST}/...; the "
                                 "API key is sent there. Set custom_upstream: true only for an "
                                 "endpoint you trust with the key")
        elif u.scheme != "https" and host not in _LOOPBACK:
            raise ValueError("a custom upstream must use https (plain http only to localhost)")

    def model_by_id(self, model_id: str) -> ModelConfig | None:
        for m in self.models.values():
            if m.id == model_id:
                return m
        return None

    def route_models(self, route: str) -> list[ModelConfig]:
        return [self.models[k] for k in self.routes[route]]


FILL_ME = "FILL_ME"  # written by scripts/init_config.py where only the user knows the value


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    models = data.get("models")
    if isinstance(models, dict):
        missing = [f"{key}.{field}" for key, m in models.items() if isinstance(m, dict)
                   for field, value in m.items() if value == FILL_ME]
        if missing:
            raise ConfigError(f"fill in {', '.join(missing)} from AI Studio → Rate Limit "
                              "(leave a value empty only if AI Studio shows no limit for it)")
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid config: {e}") from None


def load_config(path: str | Path) -> Config:
    if Path(path).is_dir():
        # Docker creates an empty folder when a bind-mounted file is missing on the host.
        raise ConfigError(f"config {path} is a folder, not a file. If you run Docker, config.yaml "
                          "was missing on the host and Docker created an empty folder in its place: "
                          "run `rmdir config.yaml`, then `cp config.example.yaml config.yaml` and "
                          "fill in your limits (or use scripts/init_config.py)")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    return parse_config(data)
