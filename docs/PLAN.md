# Implementation plan

Read with [DESIGN.md](DESIGN.md). Each phase ends with a check before the next one starts.

## Phase 0: Probe (about 30 requests from the daily quota)

Goal: replace assumptions with recorded facts before writing the router.

- `scripts/probe.py`, run with `python -I`, reading the key from the environment:
  1. `GET models` (native and OpenAI-compatible). Map each dashboard display name to its real model ID.
  2. For each candidate model: one tiny non-streaming call, one streaming call with
     `stream_options.include_usage`, and one tool-call request. Record success, the
     `usage` fields and whether tool calls work. Tool calls on Gemma and the Lite models are the main unknowns.
     Also probe Gemma 4 31B/26B and Robotics ER 2 (for information only; they stay out of v1 routes).
  3. Trigger one **RPM** 429 on a 500-RPD model by sending 16 quick tiny calls to 3.1 Flash Lite.
     Save the raw body with the key removed.
  4. A daily (RPD) 429 can't be triggered cheaply. Capture it whenever one happens naturally.
- Output: `docs/probe-results.md` (IDs, capabilities, 429 samples), with no secrets.

Added after review (2026-11-20):

- 5. Strict pass/fail. These count as FAIL: a truncated stream, any finish reason other
  than `stop`/`tool_calls` (e.g. `length`), an empty reply, or tool arguments whose `city` isn't
  the requested city. These count as UNAVAILABLE (untested, not incapable): 503s, 429s
  and timeouts, including a read timeout after HTTP 200.
- 6. Tool round trip on every model: send the tool result back with its thought signature.
  It passes only if the reply quotes a value (`report_id`) that appears only in the tool result.
- 7. Tool-loop handoff experiments (DESIGN §5.1): signature removed, handoff between the v1
  pair with the signature as received and with the placeholder, and a 2.5 call into Gemini 3.
  A trial is INCONCLUSIVE when the replayed call carried no signature. These are
  single-step evidence only; v1 enables no handoff.
- 8. The burst is sent in parallel (run 1's sequential burst spread over 104 s and got no 429).
- 9. **Zoo Code check, by hand, before any router code** (no proxy involved): point a Zoo
  Code "OpenAI Compatible" profile straight at
  `https://generativelanguage.googleapis.com/v1beta/openai/` with `gemini-3.5-flash-lite`
  and run a small task that needs at least two tool calls (for example "read README.md and
  then list docs/"). Record:
  - Does it execute a tool call when the stream ends with `finish_reason: "stop"`?
  - Does the second tool step succeed? A 400 mentioning `thought_signature` means Zoo Code
    drops `extra_content`, and DESIGN §5.1's fallback plan (the proxy re-attaches signatures) is needed.
  - Zoo Code version, and the outcome, in `probe-results.md`.
- `--rescore` rebuilds the report from the raw runs without sending requests.
- Probe regression tests: `python3 -I -m unittest discover -s tests -p test_probe.py -v`
  (offline, standard library only; `pytest` also runs them).

Status (2026-11-20): runs at 04:43 PT (all models, 51 requests), 05:15 PT (v1 pair, 27
requests) and 05:28 PT (v1 pair, tools only, 8 requests) are done, and so is the Zoo Code check.
See [probe-results.md](probe-results.md). **Phase 0 is complete** under the revised exit criteria.

Exit criteria (revised 2026-11-20):
- [x] every model in the inventory has a confirmed ID or is dropped
- [x] the v1 model (3.5 Flash Lite) passes the tool round trip
- [x] Google is shown to enforce thought signatures on the v1 model (400 without one)
- [x] the Zoo Code check (step 9) is recorded: PASS, signatures preserved
- Replaced: "a recorded per-minute 429 body". Two bursts got none; the model returned
  503s first, and forcing one harder would go against the compliance stance. The
  classifier is built from mocked bodies, falls back to a 60 s cooldown for unknown
  429s, and the proxy saves the first real one under `data/` for later.
- Replaced: "both v1 models pass the round trip". v1 has one model; 3.1 Flash Lite is
  re-tested (`--only "Gemini 3.1 Flash Lite" --checks tools,roundtrip --no-burst`)
  before it joins the route.

## Phase 1: Core logic (no HTTP)

- `config.py`: load and validate `config.yaml` (unknown model in a route → startup error).
- `clock.py`: injectable clock; `next_pt_midnight(now)`.
- `quota.py`: rolling windows, RPD counter, cooldown and exhausted state, reserve, refund
  and settle, SQLite persistence.
- `classify.py`: 429 body → {rpm, tpm, rpd, unknown} + retry delay.
- `router.py`: eligibility check, context fit, candidate selection, tool-loop pinning
  (DESIGN §5.1), attempt and deadline limits, final status (429 / 503 / 400).

Tests (`pytest`, injected clock, no network):
- PT midnight across both daylight-saving changes, and at 23:59:59 / 00:00:00 PT
- reserve under parallel `asyncio.gather` never goes over RPM or RPD
- TPM check counts input tokens only, and skips a model whose free TPM is smaller than the input
- reported `prompt_tokens` replaces the estimate in the TPM window
- a request too large for every candidate → 400 `context_length_exceeded`, no reservation
- tool turn with known call IDs → only that model; model out → 429/503, no fallback
- multi-step turn (two assistant tool calls after the last user message) → pinned to the
  turn's model, not only the latest call's
- affinity survives a simulated restart (SQLite) and expires after 24 h
- unknown IDs → normal routing with `x-turn-affinity: unknown`; mixed IDs → latest call's
  model with `x-turn-affinity: mixed`
- a non-empty `handoffs:` in config is a startup error in v1
- stop after `max_attempts` calls or at the deadline; skipped candidates don't count
- final status table (DESIGN §5 step 5): all disabled → 503 `no_enabled_model` without
  `Retry-After`; any too large and the rest disabled → 400; disabled + quota → 429;
  quota + unavailable → 503; `Retry-After` comes only from candidates that can serve the
  request, and accounts for TPM room for this request's input
- RPD state survives a simulated restart; it resets on the next PT date
- classifier against the recorded Phase 0 bodies, plus an unknown body, and error
  bodies shaped as both an object and an array
  (Phase 0 recorded no 429, so the 429 bodies are synthetic, shaped like Google's RPC
  error format; the recorded 503 checks the array shape)

Status (2026-11-21): **Phase 1 is complete.** `quota_router/` implements the modules above
plus `db.py` and `affinity.py`; every test listed above exists, and `.venv/bin/python -m pytest -q`
passes (129 tests, including the probe's). A deep code review and a follow-up review of its fixes
are done, and their decisions are in DESIGN §5.2.

## Phase 2: HTTP layer

- FastAPI app: auth, `/v1/chat/completions` (streaming and non-streaming), `/v1/models`, `/status`, `/healthz`.
- Fallback loop per DESIGN §5; streaming rules per §8; response headers.
- Removing keys from logs and errors.
- Observability (DESIGN §17): the JSON log line, `/metrics` with bounded labels, and the
  richer `/status`.
- Config (DESIGN §12, §19.1): an empty `rpm`, `tpm_input` or `rpd` means no limit, and the
  quota store skips that check; `billing` defaults to `free`, and `paid` is a startup error.
- Contract with the Phase 1 router (from the Phase 1 review):
  - The `Upstream` callable maps every exception (httpx errors included) to an
    `UpstreamResult`; only cancellation propagates.
  - Call `Router.complete()` (which records tool-call affinity) before sending the final
    chunk or `[DONE]`, so a fast follow-up request finds its turn's model.
  - Record the tool-call ID format from real responses: affinity assumes IDs don't repeat
    across responses.

Tests (`respx` mocking upstream):
- 429 → next model; 400 → returned unchanged; 503 → next model plus a cooldown
- every model out → 429 with the correct `Retry-After`
- streaming: 429 before the first byte falls back; an error mid-stream sends an SSE error and no fallback
- missing or wrong token → 401; the key never appears in any response or log line
- foreign `Host` or `Origin` → 403 `forbidden_host` (also on `/healthz`); local hosts, any
  port, and requests without `Origin` pass; no response carries CORS headers
- housekeeping (DESIGN §20.10): the prune pass removes expired RPD, exhausted and affinity
  rows and survives a SQLite error; a version-0 database with the Phase 1 tables becomes
  version 1; a newer version, a damaged file or a non-database file is a startup error
  naming the file; 429 captures stop at 50 files

Status (2026-11-21): **Phase 2 is complete.** `quota_router/` adds `app.py`, `upstream.py`,
`settings.py`, `scrub.py`, `logs.py`, `metrics.py` and `__main__.py` (`python -m quota_router`);
every test listed above exists, and `.venv/bin/python -m pytest -q` passes (205 tests). A deep
code review and two follow-up reviews are done, then an external review's five security and
correctness findings were fixed and checked by a further code review (DESIGN §12, §20.3, §20.4,
§20.7). Every fix has a regression test; the router change the reviews led to (attempts near
the deadline) is in DESIGN §5.2. One live tool-call
request through the proxy recorded the tool-call ID format ([probe-results.md](probe-results.md)).
- log lines and metrics hold no prompt or completion text, tokens or tool-call IDs; every
  metric label value comes from config or a fixed list

## Phase 3: Packaging and docs

- `Dockerfile` (non-root, pinned base image digest), `docker-compose.yml` bound to `127.0.0.1`,
  with log rotation (`max-size: 10m`, `max-file: 3`) and `data/` on a named volume (DESIGN §20.10).
- Optional: Dependabot alerts on the pinned requirements.
- `requirements.txt` with hashes. `.env.example`.
- README: quick start, Zoo Code / Copilot / Cline setup (see DESIGN §15), `/status`, limits and disclaimers.
- `/dashboard` with its cookie login (DESIGN §17.2), and an optional `monitoring` compose
  profile (Prometheus and Grafana with a provisioned dashboard), off by default.
- README: reaching the proxy from other devices over Tailscale (DESIGN §18).
- `scripts/init_config.py` (DESIGN §19.2), and README text saying the model list and limits
  are per account, and that the key must come from a project without billing enabled.

## Phase 4: Review and live check

- Deep code review (retries, reservations and parallel access are the risky areas). Fix the findings.
- Live smoke test: one request on `gemini-fast`, check `x-served-model`. Mark its only
  candidate exhausted and check for a 429 with a `Retry-After` pointing at the next PT
  midnight (with one model, fallback itself is covered by the mocked Phase 2 tests). Run one real Zoo Code task that uses tools
  (one profile on `gemini-fast`), now through the proxy (the direct check was Phase 0 step 9).
  Include a restart of the proxy in the middle of the task.
  Copilot Chat and the second Zoo Code profile wait for `gemini-smart` after v1.
- Pre-publish checklist (below).

## After v1

Automatic routing (`gemini-auto`), proposed in DESIGN §16: `gemini-smart` and a decision log,
then tiers with spill and pacing, then rules (after a probe of the signals Zoo Code sends),
then automatic adaptations, then a suggestion report. Each step gets its own plan entry,
approved before code.

Step 1 (DESIGN §16.8, approved 2026-11-21), in order:

1. Handoff probe (`scripts/probe.py --handoffs`), run by hand. It also probes 3.8 Flash and
   3.1 Flash Lite tool calls again. Results go into probe-results.md.
2. Router handoffs (`handoff_targets`, `handoff_after_s`, placeholder rewrite, headers, metric)
   with offline tests.
3. Schema migration 1→2 (`affinity.turn_id`, `turns` table) and the decision log.
4. `gemini-smart` in config and `init_config.py`. README profile for Zoo Code Architect/Debug.
5. Live check: one Zoo Code Architect task on `gemini-smart` with a forced handoff.

Also after v1: alerts (DESIGN §17.2 layer 6), a remote-hosting design (DESIGN §18)
if the proxy is to run somewhere other than the user's machine.

## Validation commands

- Unit and integration, until Phase 3 adds Docker: `.venv/bin/python -m pytest -q`
  (set up once with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`).
  Offline: injected clock, in-memory or temporary SQLite, no network.
- From Phase 3: `docker compose run --rm test` (runs `pytest -q`)
- Live smoke test: by hand, as recorded in [probe-results.md](probe-results.md) (Phase 4); it uses quota

## Pre-publish checklist

- [ ] No key-shaped string in history apart from the test placeholders:
  `git log -p --all | grep -oE 'AIza[0-9A-Za-z_-]{35}' | grep -vxF -e AIzaSyTEST-key-000000000000000000000000 -e AIzaOTHERKEY_aaaaaaaaaaaaaaaaaaaaaaaaaa | wc -l`
  prints 0 (only the two exact test fixtures are skipped); `.env` and `data/` are ignored
- [ ] The exact key and token values find nothing either. Not every key starts with `AIza`:
  `set -a; . ./.env; set +a; git log -p --all | grep -cF -e "$GEMINI_API_KEY" -e "$PROXY_TOKEN"` prints 0
- [ ] No screenshots or exported chats containing account details
- [ ] README states it is not affiliated with Google, needs your own key, and makes no "bypass/unlimited" claims
- [ ] LICENSE file present
- [ ] COMPLIANCE.md reviewed against the current Google terms
- [x] The body-size estimate runs in bounded memory (DESIGN §20.3, open item from 2026-11-21; streamed in 4 KB windows)

## Decisions (2026-11-20)

1. **Repo name:** `quota-router-for-gemini` (descriptive; "Gemini" used only to say what it works with).
2. **License:** MIT.
3. **Gemma:** probe in Phase 0; add the `gemma-small` route after v1 if tool calls and quality are good enough.
4. **Robotics ER 2:** left out of all routes; probed in Phase 0 for information only.

## Decisions after review (2026-11-20, approved)

5. **v1 scope:** one route (`gemini-fast`: 3.5 Flash Lite only; 3.1 Flash Lite joins once
   its tool call passes, see Phase 0), one uvicorn
   worker, Zoo Code as the only tested client. `gemini-smart`, Copilot Chat and Gemma come after v1.
6. **TPM is input tokens.** Output and reasoning tokens are not reserved against it; context
   and output limits are checked separately.
7. **Limits:** a request deadline of 180 s and at most 3 attempts. Final status: 429 for quota,
   503 for outages, 400 `context_length_exceeded` when nothing can fit.
8. **Tool turns are pinned** to the model that made their calls, with affinity kept in
   SQLite for 24 h. No handoff in v1; one may be enabled per pair only after a multi-step
   tool loop passes across that pair.
10. **Zoo Code is checked against Google directly in Phase 0** (signature preservation and
    `finish_reason: "stop"`), before router code is written.
9. **Regions:** COMPLIANCE.md states the available-regions term; the project doesn't
   address region restrictions.
