# quota-router-for-gemini

A local, OpenAI-compatible router that sends each request to a Gemini API model that still
has free-tier quota. It switches models automatically when one hits its rate limit, and stays
within Google's published limits for your own single project and key.

**Status: in development.** Two routes, both tested live with Zoo Code: `gemini-fast`, and
`gemini-smart`, which can hand a tool turn to another model when its own runs out. A third,
`gemini-auto`, picks the tier for each turn; it passes the offline tests but hasn't had its
live check yet. More clients come later ([plan](docs/PLAN.md)).

- [Design](docs/DESIGN.md) · [Implementation plan](docs/PLAN.md) · [Terms of service notes](docs/COMPLIANCE.md)
- License: [MIT](LICENSE)

## Important

- You need your own Gemini API key. Run it only for yourself; don't host it for others.
- Use a key from a project **without billing** (AI Studio shows "Free tier" on its Rate Limit
  page). On a billed project every call is charged, so the proxy refuses to start with
  `billing: paid`. Quota from paid Google plans (the Gemini app, Antigravity, the AI Studio
  web page) only works inside those apps; no API key, and so no proxy, can use it.
- The model list and limits belong to **your** account. They differ between accounts and
  change over time: copy them from AI Studio, don't trust the example values.
- Use it only from a region where the Gemini API is available
  ([list](https://aistudio.google.com/docs/available-regions)).
- On the free tier, Google may use your prompts and responses to improve its products,
  and human reviewers may read them. Don't send confidential code or personal data.
- This doesn't raise or get around any limit. It uses the separate quota each model already has.

## Quick start (Docker)

Needs Docker with Compose v2. On Windows, run it from WSL with the repo on the Linux
filesystem (for example `~/repo`), not under `/mnt/c`.

1. **Secrets.** Create `.env` readable only by you, then set `GEMINI_API_KEY` (from
   [AI Studio](https://aistudio.google.com/apikey), project without billing) and `PROXY_TOKEN`:

   ```sh
   install -m 600 .env.example .env      # not cp: with the usual umask, cp leaves it readable by others
   ```

   (Already have one? `chmod 600 .env`.) Generate the token with:

   ```sh
   python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
   ```

2. **Config.** Write `config.yaml` from your account's model list (uses no generation quota):

   ```sh
   python3 -I scripts/init_config.py                                   # lists your models
   python3 -I scripts/init_config.py --force --models gemini-3.5-flash-lite
   ```

   Then replace every `FILL_ME` with your project's RPM, input TPM and RPD from
   AI Studio → Rate Limit. The proxy won't start while a `FILL_ME` is left. Leave a value empty
   (`rpd:`) only if AI Studio shows no limit for it. `--models` takes IDs in route order;
   they become the `gemini-fast` route. The file also offers a `gemini-smart` route, commented
   out, for the models on your account whose tool calls passed the probe; its comment says how
   to turn it on. You can also start from `config.example.yaml`, which has both routes.

3. **Run.**

   ```sh
   docker compose up -d --build
   curl -s http://localhost:4000/healthz
   curl -s -H "Authorization: Bearer $PROXY_TOKEN" http://localhost:4000/v1/models
   ```

   (`$PROXY_TOKEN` there means your token; `set -a; . ./.env; set +a` loads it into the shell.)

The proxy listens on `127.0.0.1:4000` only. Its state (daily counts, exhausted models, tool-turn
records) is in the `qr-data` Docker volume and survives restarts and rebuilds. Logs go to
`docker compose logs quota-router`, rotated at 3 × 10 MB. The container is capped at 512 MB of
memory (it idles at about 50 MB); Docker Desktop's other figure is the machine's total.

- After editing `config.yaml`: `docker compose up -d --force-recreate quota-router`. The proxy
  reads it only at startup, and a plain `up -d` doesn't notice a changed file. `restart` isn't
  enough either if your editor saves by replacing the file: the container keeps the old one.
- After editing `.env`: `docker compose up -d`. After pulling new code: `docker compose up -d --build`.

### Without Docker

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m quota_router
```

It reads `.env` and `config.yaml` from the current folder and keeps state in `data/`. In WSL,
keep `data/` on the Linux filesystem: SQLite's locking isn't reliable under `/mnt/c`. The
proxy also refuses to start if it can't make its database file owner-only (`0600`), which
happens on filesystems that ignore Unix permissions, such as `/mnt/c` and some network mounts.

## Clients

Every client uses the OpenAI Chat Completions format with base URL `http://localhost:4000/v1`
and your `PROXY_TOKEN` as the API key. Set the context window to about **200000**, below the
250K input-tokens-per-minute limit: a larger request can't be sent to any model.

### Zoo Code

1. Settings → Providers → **+**, provider type **OpenAI Compatible** (not LiteLLM).
2. Base URL `http://localhost:4000/v1`, API key `PROXY_TOKEN`, model ID `gemini-fast`.
3. Context window about 200000; keep context condensing on.

4. If `config.yaml` has the `gemini-smart` route, add a second profile the same way with model
   ID `gemini-smart`.
5. Pick a profile in each mode (Zoo Code remembers the profile last used in each one):
   Architect, Debug and Orchestrator on `gemini-smart`; Code and Ask on `gemini-fast`.

Tool loops need Gemini's thought signatures. Zoo Code 3.98.0 doesn't always send them back
(seen in a live check, 2026-11-22); the proxy restores them, as for Copilot.
Both profiles were checked live. For `gemini-smart` that was one read-only Architect task with
a forced handoff from 3 Flash to 2.5 Flash; the later tool turns stayed on 2.5 Flash with no
error ([probe results](docs/probe-results.md)). Debug and Orchestrator modes weren't tried.

The smart models allow about 20 requests a day each, and a tool turn stays on the model that
started it. When that model runs out, or is blocked for longer than `handoff_after_s`
(60 s), the turn moves to the next model in the route that's listed in `handoff_targets`.
The response then has the header `x-turn-affinity: handoff`. List a model there only after it
passes `python3 -I scripts/probe.py --handoffs` ([DESIGN §16.8](docs/DESIGN.md)); the example
config lists the ones that passed on the author's account.

To stop switching profiles by hand, uncomment the `auto:` block in `config.yaml` and use one
profile with model ID `gemini-auto` in every mode. Each turn starts on the fast models, unless
the smart models have more quota left than the rest of your day is expected to use. In that
case it starts on smart, and the response has `x-route-reason: pace-upgrade`. A turn that
can't be served in its tier moves to the other one (`spill`). The proxy learns when you work
from the last 14 days of turn starts (dates, hours and counts only), once 7 of those days have
any. Until then it uses `working_hours` and `timezone`, so set those to your own. Without them
it assumes even use over the Pacific day, and much of the smart quota can go unused
([DESIGN §16.9](docs/DESIGN.md)).

### Cline / Kilo Code

Provider **OpenAI Compatible**, same base URL and key. Plan mode `gemini-smart` (or
`gemini-fast` if you haven't set that route up), Act mode `gemini-fast`. Not tested yet.

### VS Code Copilot Chat (bring your own key)

**Chat: Manage Language Models** → **Add Models** → **Custom Endpoint**, API type
**Chat Completions**, URL `http://localhost:4000/v1`, key `PROXY_TOKEN`, model `gemini-fast`.
Turn on tool calling for it (otherwise it doesn't appear in agent mode) and set max input
tokens to about 200000. Inline completions keep using Copilot's own model. Business and
Enterprise plans need the "Bring Your Own Language Model Key in VS Code" policy.
Copilot drops the thought signatures from the tool calls it sends back, which Gemini 3 and
later reject. The proxy stores each signature when the response passes through and puts it
back on the follow-up ([DESIGN §15](docs/DESIGN.md#15-clients), §23). Checked live in agent
mode on `gemini-smart` (2026-11-22): the tool turn's follow-up got its signature back and
returned 200.

## Watching it

- **Response headers:** `x-served-model` (the model that answered), `x-fallback-path`
  (models tried, in order), `x-turn-affinity` (for tool turns).
- **`/dashboard`** in a browser: sign in with `PROXY_TOKEN`. It shows each model's
  requests per minute, input tokens per minute and requests today against its limits,
  cooldowns, the time to the daily reset (midnight Pacific), today's requests by status and
  reason, and recent errors. You stay signed in in that tab only (no cookie), for 12 hours
  or until the proxy restarts. The page needs JavaScript.
- **`/status`** (JSON) and **`/metrics`** (Prometheus) need `Authorization: Bearer PROXY_TOKEN`.
- **Grafana (optional):** set `GRAFANA_ADMIN_PASSWORD` in `.env`, then
  `docker compose --profile monitoring up -d` and open <http://localhost:3000> (user `admin`).
  Prometheus scrapes the proxy every 15 s and keeps 15 days; neither is reachable from other
  machines. `docker compose --profile monitoring down` stops them.

## Other devices over Tailscale

To use the proxy from your laptop or phone while this machine is on, without opening it to
the internet:

1. Install [Tailscale](https://tailscale.com) on both devices (the free Personal plan is enough).
2. On this machine: `tailscale serve --bg 4000`. It prints an address such as
   `https://pc.tail1234.ts.net`.
3. Add that name to `.env`: `PROXY_ALLOWED_HOSTS=pc.tail1234.ts.net`, then `docker compose up -d`.
   The proxy refuses other host names, which protects it from web pages in your browser.
4. On the other device, use base URL `https://pc.tail1234.ts.net/v1` with the same `PROXY_TOKEN`.

The token is still required; anyone on your tailnet without it gets 401. Don't use
`tailscale funnel`, which publishes to the internet. `tailscale serve reset` stops it.
Whether the dashboard sign-in works through `tailscale serve` is checked in Phase 4.

## How it behaves

- Each route is an ordered list of models. A request goes to the first one with room for it
  (requests per minute, input tokens per minute, requests per day); on a rate-limit reply it
  moves to the next. At most 3 attempts within 180 seconds.
- `gemini-auto` decides once per turn which tier comes first, and never refuses a turn for
  pacing: it still tries every model before replying 429. Its responses carry
  `x-route-tier` and `x-route-reason`, and `/status` shows the pace under `auto`.
- When every model is out for the day, the reply is 429 with `Retry-After` pointing at the
  next midnight Pacific, when Google resets daily quotas.
- A request too large for every model's context gets 400 `context_length_exceeded` without
  calling Google.
- A tool result goes back to the model that made the tool call (kept for 24 hours, across
  restarts), since Gemini's thought signatures only work with the same model. The exception
  is a handoff (see Zoo Code above): the proxy then swaps the other model's signatures for
  the placeholder Google documents for history from a different model.
- A model that hangs is waited on for up to 120 seconds before the next one is tried. A model
  can set a shorter `first_byte_timeout_s`. A timed-out request still uses one of that model's
  daily requests, so set it only well above the `first_byte_ms` of its successful answers.
- Prompts and replies are never logged. Each request logs one JSON line with route, model,
  status, token counts and timings. A 429 or 503 from the proxy also logs its `retry_after_s`
  and what blocked each model (`blocked`: `rpm`, `rpd`, a cooldown and so on). The first few rate-limit replies are saved, with keys
  removed, in `data/429/` so their format can be checked.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `startup error: config /app/config.yaml is a folder` | `config.yaml` was missing, so Docker created a folder: `rmdir config.yaml`, then step 2 above |
| `startup error: fill in ….rpm …` | Replace the `FILL_ME` values in `config.yaml` |
| `startup error: … database …` | The state file is damaged or locked. Deleting it is safe: only today's local counts and open tool turns are lost |
| 403 `forbidden_host` | Use `localhost`/`127.0.0.1`, or add the host name to `PROXY_ALLOWED_HOSTS` |
| 401 `invalid_api_key` | The client's API key isn't your `PROXY_TOKEN` |
| 429 with `Retry-After` | Every model in the route is out of quota; see `/dashboard` |

## Development

```sh
docker compose run --build --rm test          # the test suite, offline (no network)
.venv/bin/python -m pytest -q                 # same, without Docker
```

Set up the virtualenv once with `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
Requirements are hash-locked; to change one, edit `requirements.in` or `requirements-dev.in` and
regenerate with `pip-compile --generate-hashes --allow-unsafe --strip-extras` (pip-tools).

Not affiliated with, endorsed by, or sponsored by Google. Gemini is a trademark of Google LLC.
