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

Exit criteria: every model in the inventory has a confirmed ID or is dropped, and at
least one recorded per-minute 429 body exists.

## Phase 1: Core logic (no HTTP)

- `config.py`: load and validate `config.yaml` (unknown model in a route → startup error).
- `clock.py`: injectable clock; `next_pt_midnight(now)`.
- `quota.py`: rolling windows, RPD counter, cooldown and exhausted state, reserve, refund
  and settle, SQLite persistence.
- `classify.py`: 429 body → {rpm, tpm, rpd, unknown} + retry delay.
- `router.py`: eligibility check and candidate selection.

Tests (`pytest`, injected clock, no network):
- PT midnight across both daylight-saving changes, and at 23:59:59 / 00:00:00 PT
- reserve under parallel `asyncio.gather` never goes over RPM or RPD
- TPM check skips a model whose free TPM is smaller than the request
- RPD state survives a simulated restart; it resets on the next PT date
- classifier against the recorded Phase 0 bodies, plus an unknown body

## Phase 2: HTTP layer

- FastAPI app: auth, `/v1/chat/completions` (streaming and non-streaming), `/v1/models`, `/status`, `/healthz`.
- Fallback loop per DESIGN §5; streaming rules per §8; response headers.
- Removing keys from logs and errors.

Tests (`respx` mocking upstream):
- 429 → next model; 400 → returned unchanged; 503 → next model plus a cooldown
- every model out → 429 with the correct `Retry-After`
- streaming: 429 before the first byte falls back; an error mid-stream sends an SSE error and no fallback
- missing or wrong token → 401; the key never appears in any response or log line

## Phase 3: Packaging and docs

- `Dockerfile` (non-root, pinned base image digest), `docker-compose.yml` bound to `127.0.0.1`.
- `requirements.txt` with hashes. `.env.example`.
- README: quick start, Zoo Code / Copilot / Cline setup (see DESIGN §15), `/status`, limits and disclaimers.

## Phase 4: Review and live check

- Deep code review (retries, reservations and parallel access are the risky areas). Fix the findings.
- Live smoke test: one request per route, check `x-served-model`. Force fallback by
  marking the first candidate exhausted. Run one real Zoo Code task (two profiles, modes mapped) and one Copilot Chat agent-mode task
  (Custom Endpoint, Chat Completions).
- Pre-publish checklist (below).

## Validation commands (to be fixed in Phase 1)

- Unit and integration: `docker compose run --rm test` (runs `pytest -q`)
- Live smoke test: `python -I scripts/smoke.py` (uses quota; run by hand only)

## Pre-publish checklist

- [ ] `git log -p | grep -iE 'AIza|api[_-]?key\s*='` finds nothing; `.env` and `data/` are ignored
- [ ] No screenshots or exported chats containing account details
- [ ] README states it is not affiliated with Google, needs your own key, and makes no "bypass/unlimited" claims
- [ ] LICENSE file present
- [ ] COMPLIANCE.md reviewed against the current Google terms

## Decisions (2026-11-20)

1. **Repo name:** `quota-router-for-gemini` (descriptive; "Gemini" used only to say what it works with).
2. **License:** MIT.
3. **Gemma:** probe in Phase 0; add the `gemma-small` route after v1 if tool calls and quality are good enough.
4. **Robotics ER 2:** left out of all routes; probed in Phase 0 for information only.
