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

Done 2026-11-21: growing 5xx cooldown for overloaded models (DESIGN §22, approved 2026-11-21 with adjustments),
found in the step 5 live check: per-model failure streak and epoch, cooldown 30 s × 4 up to
15 min, hard for 30 s then soft with a last-resort pass and one recovery attempt per model,
`unavailable_streak` in `/status`, `attempts` in the log line, with the offline tests in §22.5.
Offline validation used `.venv/bin/python -m pytest -q` and `docker compose run --build --rm test`.
Live validation is pending. During the next observed overload, use `attempts` in the request log
to distinguish an upstream 503 from a timeout and check that later requests follow the cooldown.
Partial evidence (2026-11-21, during the first Copilot Chat live test on `gemini-smart`):
`gemini-4-hydrogen` answered one request with a 503 after 17 s and hung on another until the
120 s timeout. `attempts` told the two apart, both counted, and the cooldown held it at streak 2
(2 min). Still open: seeing later requests skip it during the cooldown and its recovery.

Done 2026-11-22 (steps 1–7): restoring thought signatures
that clients drop (DESIGN §23, approved 2026-11-21), found in the first Copilot Chat live test
(every `gemini-smart` tool turn got a 400 for a missing signature). Offline validation used
`.venv/bin/python -m pytest -q` and `docker compose run --build --rm test`, then
`code-reviewer-deep`. The review fixes and three amendments (DESIGN §23.8, approved 2026-11-22)
are implemented too. In order:

1. Schema migration 2→3 (`affinity.signature`), the limits (16,384 characters each, 16 MiB in
   total), database file `0600`.
2. Capture: non-streamed in `app.py`; streamed in `StreamSession` by `(choice, tool_call)`
   index, with the settling write before the stream's last bytes.
3. Restore in the router: one copy-on-write rewrite that replaces `with_placeholder_signatures`
   (stored signature for the serving model's own calls, placeholder otherwise), with the
   estimate from the body sent.
4. `qr_thought_signatures_total{model, action}`, `signatures` in `attempts` and `/status`.
5. Offline tests from DESIGN §23.7, including the Copilot-shaped regression test.
6. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer-deep` (two-step stream write, ID reuse).
7. Live check: the Copilot agent-mode request on `gemini-smart` from DESIGN §23.7, then a short
   Zoo Code task. Results go into probe-results.md. Done 2026-11-22: both passed with no 400;
   Zoo Code also had a signature restored, so it doesn't always keep them (DESIGN §23.8).

Step 2 (DESIGN §16.9, approved 2026-11-22 with the recommended choices: the smart tier paced
as one pool, the demand curve read from the decision log, the measured curve after 7 days,
`reserve: 5`, and a top-level `auto:` block), in order:

1. Config: the `auto:` block and its validation (§16.9.1), and the block commented out in
   `config.example.yaml` and `scripts/init_config.py`.
2. Demand curve (§16.9.3): measured from `turns`, then `working_hours`, then flat. It is cached
   per PT hour, keeps its last value when a query fails, and never goes down on the fall-back day.
3. Pool pacing and the upgrade test (§16.9.2), with candidate order and handoff targets taken
   from the owner's tier first (§16.9.4).
4. Schema migration 3→4 (`turns.tier`) and the new `reason` values `default` and `pace-upgrade`.
5. Visibility (§16.9.5): `x-route-tier` and `x-route-reason`, the `auto` object in `/status`,
   `qr_route_decisions_total`, `qr_pace_left` and `qr_pace_upgrade_above`, and a dashboard pace row.
   README: a Zoo Code profile on `gemini-auto` and a note to set `working_hours`.
6. Offline tests from DESIGN §16.9.7.
7. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer` (routing, a migration, and unlocked concurrent reads of quota state).
8. Live check: one working day of Zoo Code on `gemini-auto`. Check the headers, see `/status`
   `left` fall with `share`, and see most of the smart pool used by the end of working hours.

Done 2026-11-22: steps 1–7. Offline validation used `.venv/bin/python -m pytest -q` and
`docker compose run --build --rm test` (566 passed in both), then `code-reviewer`. The review
found no bugs. It raised one risk, which is now fixed with a regression test: the monotone share
was held across a change of source, after a failed curve read. The no-tier owner case is now
documented (DESIGN §16.9.5). A second review found three more problems, now fixed, each with a
regression test that fails on the old code:
- Tier names must be header-safe: a non-ASCII name made a served request a 500, after its quota
  was used.
- The measured share now depends only on the time, so a restart in the repeated fall-back hour
  no longer lowers it. This replaces the in-memory high-water mark.
- `/status` leaves follow-up rows out of today's turn starts.

The dashboard's pace line was checked visually on 2026-11-24: it read "80 of 80 left · upgrades
while above 15.9 · 81% of the day's demand gone (flat) · upgrading now", which matches the
formula. Step 8 (a day of pacing) is pending.

Live run 1 (2026-11-22), and what followed from it:
- **The run:** 4 Hydrogen timed out at 120 s twice. A fast turn was handed off to 3 Flash after
  both Lite models returned 503, and stayed there for 20 steps, with ten 429s from its 5 RPM.
- **Config:** on team feedback, 3.6 Flash now goes before 4 Hydrogen in the live `gemini-auto`
  smart tier. This is in the local config only.
- **Handoff:** the hand-back amendment (DESIGN §16.9.8) is deferred until the logs show whether
  Zoo Code honors `Retry-After`.

Done 2026-11-22: DESIGN §24 (approved 2026-11-22):
- **Request log:** route fields, plus `retry_after_s` and per-candidate `blocked` limits on the
  proxy's 429s and 503s.
- **Timeout:** an optional per-model `first_byte_timeout_s`.
- **Validation:** `.venv/bin/python -m pytest -q` and `docker compose run --build --rm test`
  (588 passed in both), then `code-reviewer`. The review found no bugs. Its fixes, each with a
  regression test:
  - A budget of 5 s or less cut short by the deadline is no longer cooled down.
  - The "ran out" tolerance scales with the budget.
  - Stale `limits` are cleared after a last-resort call or a 404.
- **Retry timing, checked 2026-11-23 (live run 2):** Zoo Code honors `Retry-After` exactly. A
  10-minute Code task on `gemini-auto` got 19 of the proxy's 429s (14 RPM on 3.6 Flash and
  2.5 Flash, 5 TPM on 3.5 Flash Lite at 40K-token prompts), with hints of 2–40 s. Each time, its
  next request came 10–29 ms after the hint ran out. The waits added up to 223 s.
- **Live run 2, other findings:** the upgraded turn moved 3.6 Flash → 2.5 Flash (an upstream
  429) → 3.5 Flash Lite (a later handoff), so 4 Hydrogen, outside `handoff_targets` and second
  in the tier, got no requests and its `first_byte_ms` is still unknown. Two task-title requests
  (54 and 88 prompt tokens) were upgraded to 3.6 Flash, waited 19 s and 31 s for a first byte,
  and took 2 of its 5 RPM while the task's own turn needed them. That is the case the
  `task-title` rule (§16.10) is for.

Step 3 (DESIGN §16.10, approved 2026-11-23 with the recommended choices: a paced-tier rule may
run `reserve` ahead of the line, a non-paced rule blocks upgrades, a `turns.rule` column, title
requests count in the demand curve, `user_matches` decided after the probe), in order:

1. `scripts/capture_client.py` (§16.10.1) with its offline tests.
2. Client probe by hand: one task per Zoo Code mode, optionally Copilot. Results (shape and
   marker rows only) go into probe-results.md; then run `capture_client.py --clean`. Set the prefixes,
   and decide `user_matches` (§16.10.2) from what the probe shows.
   Done 2026-11-23: mode found in the system prompt's role line; no `user_matches`.
   Amendment approved 2026-11-24: `system_line_prefix` (literal) replaces `system_matches`.
3. Config: `auto.rules` and its validation, commented examples in `config.example.yaml` and
   `scripts/init_config.py`.
4. Router: rules at a turn start, the paced-tier rule line, `paced`, and spill for rule turns.
5. Schema migration 4→5 (`turns.rule`); `route_rule` in the request log; `rule_above` in
   `/status`; `qr_pace_rule_above`.
6. Offline tests from DESIGN §16.10.5.
7. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer` (routing, a migration).
8. Live check: one working day on `gemini-auto` with the rules set.
   Done 2026-11-23 20:49–21:06 PT (probe-results.md, "Live run 3"), a short session rather than a
   day: checks 1, 3, 4 and 5 pass and both planning-mode turns matched. The switch to `paced` was
   not reached (`left` 25 against `rule_above` 4.6), so it stays to be seen on a heavier day. One
   Copilot turn failed on a `tpm` block shorter than `handoff_after_s`. Proposal written 2026-11-24:
   DESIGN §25 (hold short waits in the proxy), approved 2026-11-24.

Steps 3–7 done 2026-11-24 (branch `step3b-rules`): `.venv/bin/python -m pytest -q` and
`docker compose run --build --rm test` pass. The readings settled while implementing are in
DESIGN §16.10.5, "3b implemented".

Short-wait holds (DESIGN §25, approved 2026-11-24 with the listed choices: hold instead of a
tier-aware handoff; local `rpm`/`tpm` blocks before any upstream call only; `hold_max_s` default 0;
one routing context and one hold per request; no decision-log column), in order:

1. Config: `hold_max_s` and its validation (0 always valid; a positive value needs
   `hold_max_s + 5 ≤ request_deadline_s`); commented example in `config.example.yaml` and
   `scripts/init_config.py`.
2. Router: the hold after the normal and last-resort passes, under §25.4's conditions; one
   routing context per request; the five-second margin checked again after waking and after
   reserving.
3. HTTP layer: `hold` in request scope for the request log, the 499 branch and the stream
   finaliser; `qr_holds_total{result}`; `/status` `holds`.
4. Offline tests from DESIGN §25.9.
5. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer-deep` (concurrency, a retry-like second walk, cancellation) with a Codex
   adversarial review alongside.
6. Live check: save the log, set `hold_max_s: 60` in `config.yaml`, recreate, then one Copilot
   agent-mode turn that reaches a Lite model, and a Zoo Code task.

Steps 1–5 done 2026-11-24 (branch `short-wait-holds`): `.venv/bin/python -m pytest -q` and
`docker compose run --build --rm test` pass (742). The readings and two amendments are in
DESIGN §25.10. Step 6 done 2026-11-24 (probe-results "Live run 4"): 8 holds, all served, no 429
to either client; Copilot waited through 52 s first bytes. Its turn ended on an upstream
mid-stream truncation, not a block, so a fully finished Copilot turn is not yet seen.

Stream-ending detail (DESIGN §26, approved 2026-11-24: `stream_end` on every stream line, no
metric, Gemini's `finish_reason` values checked live), in order:

1. HTTP layer: `stream_end` (`end`, `error`, `chunks`, `finish_reason`, `usage_seen`, `idle_ms`)
   tracked in `StreamSession` and written by its finaliser.
2. Offline tests from DESIGN §26.4.
3. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer` (stream ingestion).
4. Live: save the log, recreate, and read `stream_end` over the next live runs (§26.5).

Steps 1–3 done 2026-11-24 (branch `short-wait-holds`, `tests/test_stream_end.py`):
`.venv/bin/python -m pytest -q` and `docker compose run --build --rm test` pass (754);
`code-reviewer` found nothing significant, and its two test gaps are closed.
Step 4, first reading 2026-11-24 (probe-results "Live run 5"): `stream_end` on all 32 streams
that reached a first byte, all `done`; Gemini sent `stop` and `tool_calls` only. Keep reading
for an ending other than `done`. The run also showed streamed output tokens are undercounted
(DESIGN §27).

Stream token counts (DESIGN §27, approved 2026-11-24: keep the last `usage` chunk; settling
unchanged), in order:

1. `StreamSession`: every `usage` chunk updates the counts; the finaliser adds the output
   difference to the turn row through `Turns.add_output`.
2. Docs: §7, §11 and §26.2 as §27.2 item 5 says.
3. Offline tests from DESIGN §27.4, with the regression test failing on the old code.
4. Validation: `.venv/bin/python -m pytest -q`, `docker compose run --build --rm test`, then
   `code-reviewer` (data integrity: request log, turn rows, metrics).
5. Live: save the log, recreate, and check one long streamed answer's `output_tokens` against
   its length.

Steps 1–4 done 2026-11-24 (branch `short-wait-holds`, `tests/test_stream_tokens.py`; the
regression test fails on the old code): `.venv/bin/python -m pytest -q` and
`docker compose run --build --rm test` pass (766); `code-reviewer` found no defect, and its test
gaps (`read_error`, a shared turn row, the metric on every ending) are closed.
Step 5 done 2026-11-24 (probe-results "Live check of DESIGN §27"): a 61-chunk answer logged
1,438 output tokens, and its turn row has the same.

Also after v1: alerts (DESIGN §17.2 layer 6), a remote-hosting design (DESIGN §18)
if the proxy is to run somewhere other than the user's machine.

## Validation commands

- Unit and integration, until Phase 3 adds Docker: `.venv/bin/python -m pytest -q`
  (set up once with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`).
  Offline: injected clock, in-memory or temporary SQLite, no network.
- From Phase 3: `docker compose run --build --rm test` (rebuilds the image and runs `pytest -q`)
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
