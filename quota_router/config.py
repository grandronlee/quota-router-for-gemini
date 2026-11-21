"""Load and validate config.yaml (DESIGN §12).

Any problem is a startup error (ConfigError). Secrets never live here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator


class ConfigError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelConfig(_Strict):
    key: str = ""  # filled from the mapping key in Config
    id: str = Field(min_length=1)
    rpm: PositiveInt
    tpm_input: PositiveInt
    rpd: PositiveInt
    input_limit: PositiveInt
    output_limit: PositiveInt


class Config(_Strict):
    upstream_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    request_deadline_s: float = Field(default=180, gt=0)
    max_attempts: PositiveInt = 3
    db_path: str = "data/quota.sqlite3"
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
        if not self.models:
            raise ValueError("no models configured")
        if not self.routes:
            raise ValueError("no routes configured")
        if self.handoffs:
            raise ValueError("handoffs must be empty in v1 (DESIGN §5.1)")
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

    def model_by_id(self, model_id: str) -> ModelConfig | None:
        for m in self.models.values():
            if m.id == model_id:
                return m
        return None

    def route_models(self, route: str) -> list[ModelConfig]:
        return [self.models[k] for k in self.routes[route]]


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid config: {e}") from None


def load_config(path: str | Path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    return parse_config(data)
