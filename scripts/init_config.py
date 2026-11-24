#!/usr/bin/env python3
"""Write a config.yaml skeleton for your own account (DESIGN §19.2, §21.7).

models.list gives model IDs and token limits but no rate limits. This script fills in
`input_limit` and `output_limit`, and writes FILL_ME for `rpm`, `tpm_input` and `rpd`:
copy those from AI Studio → Rate Limit. The proxy refuses to start while any FILL_ME
is left, so a forgotten value can't silently remove a limit.

Standard library only, so it runs without the virtualenv:

    python3 -I scripts/init_config.py                         # list models, write config.yaml
    python3 -I scripts/init_config.py --models gemini-3.5-flash-lite,gemini-3.1-flash-lite
    python3 -I scripts/init_config.py --stdout                # print instead of writing

Reads GEMINI_API_KEY from the environment, or else from .env in the repo root. Calls only
models.list, which uses no generation quota.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://generativelanguage.googleapis.com/v1beta"
ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 30
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
FILL_ME = "FILL_ME"
# gemini-smart in DESIGN §4 order, limited to models whose tool call passed the probe
# (docs/probe-results.md); 3.8 Flash and 3.1 Flash Lite join once theirs pass.
SMART_ORDER = ("gemini-4-hydrogen", "gemini-3.6-flash", "gemini-3-flash-preview",
               "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite")
# Passed the multi-step handoff probe with the placeholder signature (DESIGN §16.8 1a).
HANDOFF_PASSED = ("gemini-3-flash-preview", "gemini-2.5-flash", "gemini-3.5-flash-lite",
                  "gemini-2.5-flash-lite")
# gemini-auto's tiers (DESIGN §16.9.1), best first, from the same probed models.
AUTO_TIERS = {"smart": ("gemini-4-hydrogen", "gemini-3.6-flash", "gemini-3-flash-preview",
                        "gemini-2.5-flash"),
              "fast": ("gemini-3.5-flash-lite", "gemini-2.5-flash-lite")}


def read_key(environ=os.environ, env_file: Path = ROOT / ".env") -> str:
    key = environ.get("GEMINI_API_KEY", "").strip()
    if key or not env_file.is_file():
        return key
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == "GEMINI_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            return value.strip()
    return ""


def scrub(text: str, key: str) -> str:
    text = text.replace(key, "<redacted>") if key else text
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "<redacted>", text)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies headers (the API key) onto a redirect, possibly to another host.
    Google's API doesn't redirect, so any redirect is treated as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"refusing to follow a redirect ({code})", headers, fp)


def _no_redirect_open(req, timeout):
    return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)


def list_models(key: str, opener=None) -> list[dict]:
    """All models from models.list (paged). The key goes in a header, never the URL,
    and redirects are never followed."""
    opener = opener or _no_redirect_open  # looked up per call, so tests can replace it
    models: list[dict] = []
    token = ""
    for _ in range(20):
        url = BASE + "/models?pageSize=1000" + (f"&pageToken={urllib.parse.quote(token, safe='')}" if token else "")
        req = urllib.request.Request(url, headers={"x-goog-api-key": key})
        with opener(req, timeout=TIMEOUT_S) as r:
            page = json.loads(r.read().decode("utf-8"))
        models += page.get("models", [])
        token = page.get("nextPageToken", "")
        if not token:
            break
    return models


def generation_models(models: list[dict]) -> list[dict]:
    """Models that support generateContent and have usable IDs and token limits, sorted by ID."""
    out = []
    for m in models:
        mid = str(m.get("name", "")).removeprefix("models/")
        if "generateContent" not in m.get("supportedGenerationMethods", []) or not SAFE_ID.match(mid):
            continue
        inp, outp = m.get("inputTokenLimit"), m.get("outputTokenLimit")
        if not (isinstance(inp, int) and inp > 0 and isinstance(outp, int) and outp > 0):
            continue
        out.append({"id": mid, "input_limit": inp, "output_limit": outp,
                    "display": str(m.get("displayName", ""))})
    return sorted(out, key=lambda m: m["id"])


def render(models: list[dict], chosen: list[str]) -> str:
    """config.yaml text. Chosen models are active; the rest are listed as comments."""
    by_id = {m["id"]: m for m in models}
    lines = [
        "# Written by scripts/init_config.py from your account's models.list.",
        "# Fill in every FILL_ME from AI Studio → Rate Limit (the proxy won't start until you do).",
        "# Leave a rate limit empty (e.g. `rpd:`) only if AI Studio shows no limit for it.",
        "# Use a key from a project WITHOUT billing: on a billed project every call costs money.",
        "",
        "upstream_base_url: https://generativelanguage.googleapis.com/v1beta/openai/",
        "custom_upstream: false",
        "request_deadline_s: 180",
        "max_attempts: 3",
        "db_path: data/quota.sqlite3",
        "billing: free                 # the only supported value",
        "max_body_bytes: 20971520      # 20 MB",
        "",
        "models:",
    ]

    def entry(m: dict, prefix: str = "") -> list[str]:
        return [f"{prefix}  {m['id']}: {{ id: {m['id']}, rpm: {FILL_ME}, tpm_input: {FILL_ME}, rpd: {FILL_ME},",
                f"{prefix}    input_limit: {m['input_limit']}, output_limit: {m['output_limit']} }}"]

    if not chosen:
        lines.append("  # none chosen yet: uncomment at least one below")
    for mid in chosen:
        lines += entry(by_id[mid])
    others = [m for m in models if m["id"] not in chosen]
    if others:
        lines.append("  # Other models on this account (uncomment to use, then fill in its limits):")
        for m in others:
            lines += entry(m, "  #")
    lines += ["", "routes:"]
    if chosen:
        lines.append(f"  gemini-fast: [{', '.join(chosen)}]   # tried in this order")
    else:
        lines.append("  # gemini-fast: [model-key, other-model-key]   # tried in this order")
    smart = [mid for mid in SMART_ORDER if mid in by_id]
    handoff = [mid for mid in HANDOFF_PASSED if mid in smart]
    if smart:
        lines += ["  # Planning and debugging (Zoo Code Architect/Debug), DESIGN §4. To use it, uncomment",
                  "  # these models above, fill in their limits, uncomment the route, then set",
                  "  # handoff_targets to the list in its comment.",
                  f"  # gemini-smart: [{', '.join(smart)}]"]
    hint = (f"   # with gemini-smart: [{', '.join(handoff)}] (passed the handoff probe, DESIGN §16.8)"
            if handoff
            else "           # models whose handoff probe passed (DESIGN §16.8)")
    lines += ["", f"handoff_targets: []{hint}",
              "handoff_after_s: 60           # hand a tool turn off only if its model is blocked longer",
              "# Hold a request for up to this many seconds when only the proxy's own per-minute count",
              "# (RPM or TPM) blocks it, instead of answering 429 (DESIGN §25). Off by default: it makes",
              "# first bytes slower for every client of the proxy. Needs hold_max_s + 5 <= request_deadline_s.",
              "# hold_max_s: 60"]
    tiers = {t: [mid for mid in ms if mid in by_id] for t, ms in AUTO_TIERS.items()}
    if all(tiers.values()):
        lines += ["",
                  "# One route that picks the tier for each turn (DESIGN §16.9). To use it, uncomment",
                  "# these models above, fill in their limits, uncomment the block and set your timezone.",
                  "# auto:",
                  "#   name: gemini-auto",
                  "#   tiers:                     # best first within each tier",
                  *(f"#     {t}: [{', '.join(ms)}]" for t, ms in tiers.items()),
                  "#   default_tier: fast",
                  "#   pacing:",
                  "#     tier: smart",
                  "#     reserve: 5               # requests kept back, shrinking to 0 as the day's demand ends",
                  '#     working_hours: "09:00-18:00"   # used until 7 days of demand are measured',
                  f'#     timezone: "{FILL_ME}"           # your IANA time zone, e.g. "Europe/Paris"',
                  "#   # Optional rules, first match wins (DESIGN §16.10.2). These match Zoo Code",
                  "#   # 3.98.2; remove the inner '# ' to use them.",
                  "#   # rules:",
                  "#   #   - name: task-title",
                  "#   #     when: { max_tools: 0, max_input_tokens: 500 }",
                  "#   #     tier: fast",
                  "#   #   - name: planning-mode",
                  "#   #     when:",
                  "#   #       system_line_prefix:",
                  '#   #         - "You are Zoo, an experienced technical leader"',
                  '#   #         - "You are Zoo, a strategic workflow orchestrator"',
                  "#   #     tier: smart"]
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", default="", help="comma-separated model IDs to enable, in route order")
    ap.add_argument("--stdout", action="store_true", help="print the config instead of writing it")
    ap.add_argument("--output", default=str(ROOT / "config.yaml"), help="file to write (default: config.yaml)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)

    key = read_key()
    if not key:
        print("GEMINI_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2
    out = Path(args.output)
    if not args.stdout and out.exists() and not args.force:
        print(f"{out} already exists; use --force to overwrite or --stdout to print.", file=sys.stderr)
        return 2
    try:
        models = generation_models(list_models(key))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:2000]
        print(f"models.list failed with HTTP {e.code}: {scrub(body, key)}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"models.list failed: {scrub(str(e), key)}", file=sys.stderr)
        return 1
    if not models:
        print("models.list returned no models that support generateContent.", file=sys.stderr)
        return 1

    chosen = [m.strip() for m in args.models.split(",") if m.strip()]
    known = {m["id"] for m in models}
    unknown = [m for m in chosen if m not in known]
    if unknown:
        print(f"not on this account: {', '.join(unknown)}", file=sys.stderr)
        return 2
    if len(set(chosen)) != len(chosen):
        print("--models lists a model twice", file=sys.stderr)
        return 2

    text = render(models, chosen)
    if args.stdout:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    print(f"{len(models)} models support generateContent"
          + ("" if chosen else "; none enabled yet (use --models, or uncomment some)"), file=sys.stderr)
    for m in models:
        mark = "*" if m["id"] in chosen else " "
        print(f" {mark} {m['id']:40} in {m['input_limit']:>8}  out {m['output_limit']:>6}  {m['display']}",
              file=sys.stderr)
    print("Next: fill in every FILL_ME from AI Studio → Rate Limit.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
