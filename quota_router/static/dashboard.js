// quota-router dashboard (DESIGN §21.5). Read-only; all text goes in with textContent.
"use strict";

const POLL_MS = 5000;
const KEY = "qr_session"; // sessionStorage: this origin (port included) and this tab only
let data = null;
let skewMs = 0; // server time minus browser time
let pollTimer = null;
let generation = 0; // bumped on sign-in/out, so a fetch in flight can't render or re-arm afterwards

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function fmtDuration(s) {
  if (s <= 0) return "now";
  s = Math.ceil(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

const num = (n) => Number(n).toLocaleString();

function untilSeconds(iso) {
  return (Date.parse(iso) - (Date.now() + skewMs)) / 1000;
}

function usageCell(used, limit) {
  const td = el("td");
  if (limit === null || limit === undefined) {
    td.append(el("span", `${num(used)} · no limit`, "muted"));
    return td;
  }
  const m = el("meter");
  m.min = 0; m.max = limit; m.value = Math.min(used, limit);
  m.low = limit * 0.7; m.high = limit * 0.9; m.optimum = 0;
  td.append(m, el("span", ` ${num(used)} / ${num(limit)}`));
  return td;
}

function renderModels() {
  const body = document.getElementById("models");
  const rows = [];
  for (const [id, s] of Object.entries(data.models)) {
    const tr = el("tr");
    tr.append(el("td", id), el("td", s.state, `state ${s.state}`),
      usageCell(s.rpm_used, s.rpm_limit), usageCell(s.tpm_input_used, s.tpm_input_limit),
      usageCell(s.rpd_used, s.rpd_limit));
    const until = s.exhausted_until || s.cooldown_until;
    const td = el("td");
    const left = el("span", until ? fmtDuration(untilSeconds(until)) : "–");
    if (until) left.dataset.until = until;
    td.append(left);
    // Failed calls in a row (DESIGN §22.4); the cooldown grows with it.
    if (s.unavailable_streak) td.append(el("span", ` · ${num(s.unavailable_streak)} failed in a row`, "muted"));
    tr.append(td);
    rows.push(tr);
  }
  body.replaceChildren(...rows);
  const routes = Object.entries(data.routes).map(([r, ms]) => `${r} → ${ms.join(", ")}`);
  document.getElementById("routes").textContent = routes.length ? `Routes: ${routes.join(" · ")}` : "";
  // gemini-auto's pace (DESIGN §16.9.5): turns are upgraded while `left` is above the threshold.
  const pace = data.auto && data.auto.pace;
  document.getElementById("pace").textContent = pace
    ? `Pace (${data.auto.route}, ${pace.tier}): ${num(pace.left)} of ${num(pace.capacity)} left · ` +
      `upgrades while above ${num(Math.round(pace.upgrade_above * 10) / 10)} · ` +
      // A paced-tier rule's line (DESIGN §16.10.2); absent from an older proxy's /status.
      (typeof pace.rule_above === "number"
        ? `rule turns while above ${num(Math.round(pace.rule_above * 10) / 10)} · ` : "") +
      `${Math.round(pace.share * 100)}% of the day's demand gone (${pace.source})` +
      (pace.upgrade_now ? " · upgrading now" : "")
    : "";
}

function renderCounts(id, counts) {
  const items = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => { const li = el("li"); li.append(el("span", k), el("strong", v)); return li; });
  document.getElementById(id).replaceChildren(...(items.length ? items : [el("li", "none yet", "muted")]));
}

function renderErrors() {
  const rows = data.recent_errors.map((e) => {
    const tr = el("tr");
    tr.append(el("td", new Date(e.at).toLocaleTimeString()), el("td", e.route), el("td", e.status), el("td", e.reason));
    return tr;
  });
  if (!rows.length) { const tr = el("tr"); const td = el("td", "none", "muted"); td.colSpan = 4; tr.append(td); rows.push(tr); }
  document.getElementById("errors").replaceChildren(...rows);
  document.getElementById("state-errors").textContent =
    data.state_errors ? `State database errors since start: ${data.state_errors}` : "";
}

function tick() {
  if (!data) return;
  document.getElementById("reset").textContent = fmtDuration(untilSeconds(data.next_pt_reset));
  for (const span of document.querySelectorAll("span[data-until]")) {
    span.textContent = fmtDuration(untilSeconds(span.dataset.until));
  }
}

function showSignIn(message) {
  generation += 1;
  clearTimeout(pollTimer);
  data = null;
  document.getElementById("app").hidden = true;
  document.getElementById("signin").hidden = false;
  const err = document.getElementById("signin-error");
  err.textContent = message || "";
  err.hidden = !message;
  document.getElementById("token").focus();
}

function showApp() {
  generation += 1;
  document.getElementById("signin").hidden = true;
  document.getElementById("app").hidden = false;
  poll();
}

async function signIn(ev) {
  ev.preventDefault();
  const input = document.getElementById("token");
  let r;
  try {
    r = await fetch("/dashboard/login", {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: input.value }),
    });
  } catch (e) {
    showSignIn("The proxy isn't reachable.");
    return;
  }
  input.value = "";
  const body = await r.json().catch(() => ({}));
  if (!r.ok || typeof body.session !== "string") {
    showSignIn(body.error || `Sign-in failed (HTTP ${r.status}).`);
    return;
  }
  sessionStorage.setItem(KEY, body.session);
  showApp();
}

async function signOut() {
  const sid = sessionStorage.getItem(KEY);
  sessionStorage.removeItem(KEY);
  if (sid) {
    try {
      await fetch("/dashboard/logout", { method: "POST", credentials: "omit", headers: { "X-QR-Session": sid } });
    } catch (e) { /* the session expires on its own */ }
  }
  showSignIn();
}

async function poll() {
  clearTimeout(pollTimer);
  const gen = generation;
  const sid = sessionStorage.getItem(KEY);
  if (!sid) { showSignIn(); return; }
  try {
    const r = await fetch("/dashboard/status.json",
      { credentials: "omit", cache: "no-store", headers: { "X-QR-Session": sid } });
    if (gen !== generation) return;
    if (r.status === 401) { // expired, or the proxy restarted
      sessionStorage.removeItem(KEY);
      showSignIn("Signed out. Sign in again.");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const fresh = await r.json();
    if (gen !== generation) return;
    data = fresh;
    skewMs = Date.parse(data.now) - Date.now();
    renderModels();
    document.getElementById("since").textContent = new Date(data.requests.since).toLocaleString();
    renderCounts("by-status", data.requests.by_status);
    renderCounts("by-reason", data.requests.by_reason);
    renderErrors();
    document.getElementById("updated").textContent = new Date().toLocaleTimeString();
    document.getElementById("conn").hidden = true;
    tick();
  } catch (e) {
    if (gen !== generation) return;
    document.getElementById("conn").hidden = false;
  }
  pollTimer = setTimeout(poll, POLL_MS);
}

document.getElementById("signin-form").addEventListener("submit", signIn);
document.getElementById("signout").addEventListener("click", signOut);
setInterval(tick, 1000);
if (sessionStorage.getItem(KEY)) showApp(); else showSignIn();
