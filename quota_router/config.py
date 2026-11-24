"""Load and validate config.yaml (DESIGN §12).

Any problem is a startup error (ConfigError). Secrets never live here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (BaseModel, ConfigDict, Field, PositiveInt, StrictInt, StrictStr,
                      ValidationError, model_validator)

GOOGLE_UPSTREAM_HOST = "generativelanguage.googleapis.com"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

PAID_MESSAGE = ("billing: paid is not supported. The proxy only runs on a Gemini API project "
                "without billing, because on a billed project every call it routes is charged "
                "(DESIGN §19.1). Use a key from a project that shows 'Free tier' in AI Studio.")


# Request-body memory estimate (DESIGN §20.3), used by app.parsed_memory_estimate: raw bytes,
# parsed strings and the payload re-encoded for upstream, plus a fixed overhead.
# Per raw byte, 3 + 3 × the widest character's width (1, 2 or 4 bytes): the bytes, the decoded
# document, the parsed strings and the upstream payload (built as a str, then encoded) all
# take memory at once. Fitted to tracemalloc peaks of 4.25×, 8× and 14× (2026-11-21).
PARSED_WIDTH_FACTOR = 3
PARSED_BASE_FACTOR = 3 + PARSED_WIDTH_FACTOR  # the ASCII and Latin-1 case
PARSED_OVERHEAD = 1024
DEFAULT_PARSED_BUDGET = 128 * 1024 * 1024
MIN_CHAT_STRUCTURE = 4096  # keys of a plain chat body (system + user, stream options: ~2.2 KB as estimated)


log = logging.getLogger("quota_router")


def min_parsed_estimate(size: int) -> int:
    """The smallest estimate a chat body of `size` bytes can get (ASCII text), so a budget of
    at least this always accepts the largest allowed plain-text request."""
    return PARSED_BASE_FACTOR * size + PARSED_OVERHEAD + MIN_CHAT_STRUCTURE


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
    # Optional wait for the first byte, instead of 120 s (DESIGN §24.2). A timed-out request
    # keeps its RPD reservation, so set it only well above the model's real first-byte time.
    first_byte_timeout_s: float | None = Field(default=None, ge=1, le=120)


_TIER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_WORKING_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def parse_working_hours(text: str) -> tuple[int, int]:
    """`HH:MM-HH:MM` as (start, end) minutes after local midnight. The end may be earlier than
    the start (the hours wrap past midnight), but not equal to it."""
    m = _WORKING_HOURS.match(text)
    if not m:
        raise ValueError(f"working_hours {text!r} must be HH:MM-HH:MM (24-hour clock)")
    start = int(m[1]) * 60 + int(m[2])
    end = int(m[3]) * 60 + int(m[4])
    if start == end:
        raise ValueError(f"working_hours {text!r} must not start and end at the same time")
    return start, end


class PacingConfig(_Strict):
    """Pacing of one tier (DESIGN §16.9.2)."""
    tier: str
    # Requests kept back for explicit use, shrinking to 0 as the day's demand ends.
    reserve: int = Field(default=5, ge=0)
    # Used until 7 days of demand are measured (DESIGN §16.9.3).
    working_hours: str | None = None
    timezone: str | None = None


# gemini-auto's own x-route-reason values (DESIGN §16.9.4, §16.10.2), and reasons the decision
# log uses; a rule may not take one of these names, since its name is sent as a reason too.
RESERVED_RULE_NAMES = frozenset({"default", "pace-upgrade", "spill", "paced", "pinned", "handoff",
                                 "handback", "unknown", "first", "mixed"})
_Count = Annotated[StrictInt, Field(ge=0)]


class RuleWhen(_Strict):
    """A rule's conditions (DESIGN §16.10.2); all that are set must hold."""
    system_line_prefix: list[str] | None = None
    min_tools: _Count | None = None
    max_tools: _Count | None = None
    min_input_tokens: _Count | None = None
    max_input_tokens: _Count | None = None
    reasoning_effort: list[StrictStr] | None = None


class AutoRule(_Strict):
    name: str
    when: RuleWhen
    tier: str


class AutoConfig(_Strict):
    """The automatic route (DESIGN §16.9.1). `tiers` is best first within each tier."""
    name: str = Field(min_length=1)
    tiers: dict[str, list[str]]
    default_tier: str
    pacing: PacingConfig
    # Ordered; the first match decides a turn start's tier (DESIGN §16.10.2).
    rules: list[AutoRule] = Field(default_factory=list)


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
    # Estimated memory of all request bodies in progress at once (DESIGN §20.3).
    max_inflight_parsed_bytes: PositiveInt = DEFAULT_PARSED_BUDGET
    # Before 2026-11-21 the budget counted raw upload bytes under this name (example: 64 MiB).
    # Still accepted, so older config files keep loading, but ignored with a warning: read as
    # parsed memory, the old example's value would be too small to start (found in review).
    max_inflight_body_bytes: PositiveInt | None = None
    models: dict[str, ModelConfig]
    routes: dict[str, list[str]]
    # Superseded by handoff_targets (DESIGN §16.8); still accepted when empty, so older
    # config files keep loading.
    handoffs: list[Any] = Field(default_factory=list)
    # Models that may take over a tool turn from another model, with placeholder signatures.
    # List only models whose multi-step handoff probe passed (DESIGN §16.8 1a).
    handoff_targets: list[str] = Field(default_factory=list)
    handoff_after_s: float = Field(default=60, ge=0)
    # The automatic route (DESIGN §16.9). After validation it is also in `routes`, its tiers
    # in config order, so /v1/models, handoffs, /status and the dashboard see it as a route.
    auto: AutoConfig | None = None

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
            raise ValueError("handoffs must be empty: per-pair handoffs were replaced by "
                             "handoff_targets (DESIGN §16.8)")
        unknown = [k for k in self.handoff_targets if k not in self.models]
        if unknown:
            raise ValueError(f"handoff_targets uses unknown model(s): {', '.join(unknown)}")
        if len(set(self.handoff_targets)) != len(self.handoff_targets):
            raise ValueError("handoff_targets lists a model twice")
        minimum = min_parsed_estimate(self.max_body_bytes)
        if self.max_inflight_body_bytes is not None:
            if "max_inflight_parsed_bytes" not in self.model_fields_set:
                # An older config with a larger max_body_bytes keeps starting: the budget is
                # derived from it, not left at a default it can't meet (found in review).
                object.__setattr__(self, "max_inflight_parsed_bytes",
                                   max(DEFAULT_PARSED_BUDGET, minimum))
            advice = ("Remove the old line" if "max_inflight_parsed_bytes" in self.model_fields_set
                      else "Replace the old line with that one")
            log.warning("config: max_inflight_body_bytes is no longer used and is ignored. The "
                        "in-flight budget now counts memory once parsed; using "
                        "max_inflight_parsed_bytes: %d (at least %d for this max_body_bytes). "
                        "%s (DESIGN §20.3).", self.max_inflight_parsed_bytes, minimum, advice)
        if self.max_inflight_parsed_bytes < minimum:
            raise ValueError(f"max_inflight_parsed_bytes must be at least {minimum} "
                             f"({PARSED_BASE_FACTOR}× max_body_bytes, plus "
                             f"{PARSED_OVERHEAD + MIN_CHAT_STRUCTURE}): it counts the memory a "
                             f"body takes once parsed, so a smaller budget could never accept a "
                             f"request of the largest allowed size. Set "
                             f"'max_inflight_parsed_bytes: {minimum}' or more in config.yaml, or "
                             f"lower max_body_bytes (DESIGN §20.3)")
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
        if self.auto is not None:
            self._check_auto(self.auto, ids)
            object.__setattr__(self, "routes", {**self.routes, self.auto.name: [
                k for members in self.auto.tiers.values() for k in members]})
        return self

    def _check_auto(self, auto: AutoConfig, ids: dict[str, str]) -> None:
        """DESIGN §16.9.1."""
        if auto.name in self.routes:
            raise ValueError(f"auto route {auto.name!r} has the same name as a route")
        if auto.name in ids:
            raise ValueError(f"auto route {auto.name!r} has the same name as a model ID")
        if len(auto.tiers) < 2:
            raise ValueError("auto needs at least two tiers")
        seen: dict[str, str] = {}
        for tier, members in auto.tiers.items():
            # It goes into the x-route-tier header after the model has been called, so a name
            # the header can't carry would fail a served request (found in review).
            if not _TIER_NAME.match(tier):
                raise ValueError(f"auto tier name {tier!r} must be letters, digits, '.', '_' or '-' "
                                 "(ASCII, starting with a letter or digit): it is sent in the "
                                 "x-route-tier header")
            if not members:
                raise ValueError(f"auto tier {tier!r} has no models")
            unknown = [k for k in members if k not in self.models]
            if unknown:
                raise ValueError(f"auto tier {tier!r} uses unknown model(s): {', '.join(unknown)}")
            for k in members:
                if k in seen:
                    raise ValueError(f"model {k!r} is in auto tiers {seen[k]!r} and {tier!r}; "
                                     "a model may be in one tier only")
                seen[k] = tier
        if auto.default_tier not in auto.tiers:
            raise ValueError(f"auto default_tier {auto.default_tier!r} is not a tier")
        pacing = auto.pacing
        if pacing.tier not in auto.tiers:
            raise ValueError(f"auto pacing tier {pacing.tier!r} is not a tier")
        if pacing.tier == auto.default_tier:
            raise ValueError("auto pacing tier must differ from default_tier: turns are "
                             "upgraded from the default tier to the paced one")
        unpaced = [k for k in auto.tiers[pacing.tier] if self.models[k].rpd is None]
        if unpaced:
            raise ValueError(f"paced model(s) {', '.join(unpaced)} need a numeric rpd: a model "
                             "without a daily limit can't be paced")
        if pacing.working_hours is not None:
            parse_working_hours(pacing.working_hours)
            if not pacing.timezone:
                raise ValueError("auto pacing working_hours needs a timezone (an IANA name)")
        if pacing.timezone is not None:
            try:
                ZoneInfo(pacing.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"auto pacing timezone {pacing.timezone!r} is not a known "
                                 "IANA time zone") from None
        self._check_rules(auto)

    @staticmethod
    def _check_rules(auto: AutoConfig) -> None:
        """DESIGN §16.10.2: every problem is a startup error, never a request-time one."""
        names: set[str] = set()
        for i, rule in enumerate(auto.rules):
            where = f"auto rule {rule.name!r}" if rule.name else f"auto rule #{i + 1}"
            # Sent in the x-route-reason header, like a tier name.
            if not _TIER_NAME.match(rule.name):
                raise ValueError(f"{where}: name must be letters, digits, '.', '_' or '-' (ASCII, "
                                 "starting with a letter or digit): it is sent in the "
                                 "x-route-reason header")
            if rule.name in RESERVED_RULE_NAMES:
                raise ValueError(f"{where}: {rule.name!r} is a reserved route reason; reserved: "
                                 f"{', '.join(sorted(RESERVED_RULE_NAMES))}")
            if rule.name in names:
                raise ValueError(f"{where}: two rules have this name")
            names.add(rule.name)
            if rule.tier not in auto.tiers:
                raise ValueError(f"{where}: tier {rule.tier!r} is not a tier")
            w = rule.when
            if not w.model_fields_set or all(getattr(w, k) is None for k in RuleWhen.model_fields):
                raise ValueError(f"{where}: 'when' needs at least one condition")
            if w.system_line_prefix is not None:
                if not w.system_line_prefix:
                    raise ValueError(f"{where}: system_line_prefix must list at least one prefix")
                for p in w.system_line_prefix:
                    if not p or "\n" in p:
                        raise ValueError(f"{where}: each system_line_prefix must be a non-empty "
                                         "string without a newline")
            for lo, hi in (("min_tools", "max_tools"), ("min_input_tokens", "max_input_tokens")):
                a, b = getattr(w, lo), getattr(w, hi)
                if a is not None and b is not None and a > b:
                    raise ValueError(f"{where}: {lo} ({a}) is above {hi} ({b})")
            if w.reasoning_effort is not None and not w.reasoning_effort:
                raise ValueError(f"{where}: reasoning_effort must list at least one value")

    def tier_of(self, model_id: str) -> str | None:
        """The auto tier of a model (by its real ID), or None."""
        if self.auto is None:
            return None
        for tier, members in self.auto.tiers.items():
            if any(self.models[k].id == model_id for k in members):
                return tier
        return None

    def tier_models(self, first: str) -> list[ModelConfig]:
        """Every auto tier's models, tier `first` first, then the others in config order."""
        assert self.auto is not None
        order = [first, *(t for t in self.auto.tiers if t != first)]
        return [self.models[k] for t in order for k in self.auto.tiers[t]]

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
