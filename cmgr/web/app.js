"use strict";
// Claude Manager web UI. Plain JS, no build step. Everything goes through /hub/* on this machine.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const store = {
  get(k, d) { try { const v = localStorage.getItem("cm." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("cm." + k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem("cm." + k); } catch {} },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = {
  token: store.get("token", ""),
  state: null,          // last /hub/state
  filter: "all",
  showShells: store.get("showShells", false),
  current: null,        // {host, target}
  paneHash: "",
  lastPane: null,
  prevStatus: new Map(),
  projects: null,
  dec: null,            // decisions sheet state
  termMode: store.get("termMode", "auto"),   // auto | fit | wrap
  wrapFont: store.get("wrapFont", 12.5),
  fastUntil: 0,
};

// ------------------------------------------------------------------ API
class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(method, path, params, body) {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(path + qs, {
    method, headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined, cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new ApiError((data && data.error) || "HTTP " + res.status, res.status);
  return data;
}

// ------------------------------------------------------------------ toasts / clipboard / time
function toast(msg, opts = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (opts.err ? " err" : "");
  el.textContent = msg;
  el.onclick = () => { opts.onclick && opts.onclick(); el.remove(); };
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), opts.ms || 3500);
}

function copyFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;font-size:16px";
  document.body.appendChild(ta);
  ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch {}
  ta.remove();
  return ok;
}
// Starts the copy synchronously inside the tap handler (iOS needs that), resolves to success.
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true, () => copyFallback(text));
  }
  return Promise.resolve(copyFallback(text));
}

function rel(ts) {
  const d = ts - Date.now() / 1000, a = Math.abs(d);
  let s;
  if (a < 60) s = Math.round(a) + "s";
  else if (a < 3600) s = Math.round(a / 60) + "m";
  else if (a < 86400) s = Math.floor(a / 3600) + "h " + Math.round((a % 3600) / 60) + "m";
  else s = Math.floor(a / 86400) + "d " + Math.round((a % 86400) / 3600) + "h";
  return d >= 0 ? "in " + s : s + " ago";
}
function clock(ts) {
  const dt = new Date(ts * 1000), now = new Date();
  const hm = dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (dt - now < 20 * 3600e3 && dt.getDate() === now.getDate()) return hm;
  return dt.toLocaleDateString([], { weekday: "short" }) + " " + hm;
}
const fmtTokens = (n) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n));
const shortAcct = (a) => (a ? a.label || (a.email || "").split("@")[0] : "");

// ------------------------------------------------------------------ routing
function route() {
  const h = location.hash || "#/";
  const m = h.match(/^#\/s\/([^/]+)\/(.+)$/);
  $("#projects-view").hidden = h !== "#/projects";
  $("#list-view").hidden = h === "#/projects";
  if (m) {
    openSession(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
  } else {
    closeSession();
    if (h === "#/projects") loadProjects();
  }
}
const sessionHash = (host, target) => "#/s/" + encodeURIComponent(host) + "/" + encodeURIComponent(target);

// ------------------------------------------------------------------ state polling
let stateTimer = null, stateBusy = false;
async function refreshState() {
  if (stateBusy) return;
  stateBusy = true;
  try {
    S.state = await api("GET", "/hub/state");
    $("#sync-dot").className = "sync-dot ok";
    renderSide();
    renderSessions();
    notifyTransitions();
    if (S.current) renderSessionHeader();
  } catch (e) {
    $("#sync-dot").className = "sync-dot bad";
    if (e.status === 401) return showLogin("Token rejected.");
  } finally {
    stateBusy = false;
  }
}
function scheduleState() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(async () => { if (!document.hidden) await refreshState(); scheduleState(); }, S.current ? 8000 : 4000);
}

function allSessions() {
  const out = [];
  for (const h of (S.state && S.state.hosts) || []) {
    for (const s of h.sessions || []) {
      out.push({ ...s, host: h.name, status: h.ok ? s.status : "offline", hostOk: h.ok });
    }
  }
  return out;
}
const ORDER = { waiting: 0, working: 1, idle: 2, unknown: 3, offline: 4, shell: 5, dead: 6 };
const STATUS_LABEL = { waiting: "needs you", working: "working", idle: "idle", shell: "shell", offline: "offline", dead: "exited", unknown: "?" };

function renderSessions() {
  const list = allSessions().filter((s) => s.is_claude || S.showShells);
  const counts = { waiting: 0, working: 0 };
  list.forEach((s) => { if (s.status in counts) counts[s.status]++; });
  $("#n-waiting").textContent = counts.waiting || "";
  $("#n-working").textContent = counts.working || "";
  document.title = (counts.waiting ? `(${counts.waiting}) ` : "") + "Claude Manager";

  const shown = list
    .filter((s) => S.filter === "all" || s.status === S.filter)
    .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.host.localeCompare(b.host) || a.project.localeCompare(b.project));
  const box = $("#sessions");
  if (!shown.length) {
    const nHosts = ((S.state && S.state.hosts) || []).length;
    box.innerHTML = `<div class="empty">${list.length ? "Nothing in this filter." : `No Claude sessions in tmux on ${nHosts} machine${nHosts === 1 ? "" : "s"}.<br>Tap ＋ to start one.`}</div>`;
    return;
  }
  box.innerHTML = shown.map((s) => {
    const active = S.current && S.current.host === s.host && S.current.target === s.target;
    const acct = shortAcct(s.account);
    const meta = [s.host, acct, "tmux " + s.target].filter(Boolean).map(esc).join(" · ");
    return `<button class="card${active ? " active" : ""}" data-host="${esc(s.host)}" data-target="${esc(s.target)}">
      <span class="dot ${esc(s.status)}"></span>
      <span class="title">${esc(s.project || s.session)}</span>
      <span class="badge ${esc(s.status)}">${esc(STATUS_LABEL[s.status] || s.status)}</span>
      <span class="meta">${meta}</span>
      ${s.preview ? `<span class="preview">${esc(s.preview)}</span>` : ""}
    </button>`;
  }).join("");
}

function renderSide() {
  const st = S.state;
  if (!st) return;
  const now = Date.now() / 1000;
  $("#accounts").innerHTML = st.accounts.length ? st.accounts.map((u) => {
    const a = u.account;
    const limits = (u.limits || []).map((l) => {
      const p = l.percent == null ? null : Math.round(l.percent);
      const cls = l.severity === "critical" || p >= 90 ? "danger" : l.severity === "warning" || p >= 70 ? "warn" : "";
      const reset = l.resets_at ? `resets ${rel(Date.parse(l.resets_at) / 1000)} · ${clock(Date.parse(l.resets_at) / 1000)}` : "not started";
      return `<div class="limit"><div class="limit-top"><span>${esc(l.label)}</span><b>${p == null ? "–" : p + "%"}</b></div>
        <div class="bar ${cls}"><i style="width:${Math.min(100, Math.max(0, p || 0))}%"></i></div>
        <div class="limit-reset">${esc(reset)}</div></div>`;
    }).join("");
    const notes = [];
    if (u.error) notes.push(`<div class="note err">${esc(u.error)}</div>`);
    if (u.fetched_at) notes.push(`<div class="note">updated ${esc(rel(u.fetched_at))}${u.stale ? " (stale)" : ""} · on ${esc(u.hosts.join(", "))}</div>`);
    else if (!u.error) notes.push(`<div class="note">fetching…</div>`);
    return `<div class="acct"><div class="acct-head"><span class="stack"><span class="acct-name" title="${esc(a.dir)}">${esc(a.label)}</span>
      <span class="sub">${esc(a.email || "")}</span></span>
      <span class="badge">${esc(a.subscription || "")}</span></div>${limits}${notes.join("")}</div>`;
  }).join("") : `<div class="empty">No logged-in accounts found.</div>`;

  $("#hosts").innerHTML = st.hosts.map((h) => {
    const n = (h.sessions || []).filter((s) => s.is_claude).length;
    const os = h.info ? (h.info.platform === "win32" ? "Windows" : h.info.platform) : "";
    return `<div class="host-row"><span class="dot ${h.ok ? "idle" : "offline"}"></span>
      <span class="grow"><b>${esc(h.name)}</b> <span class="muted small">${esc(os)} · ${n} session${n === 1 ? "" : "s"}${h.ok ? " · " + h.latency_ms + " ms" : ""}</span>
      ${h.ok ? "" : `<br><span class="error">${esc(h.error)}${h.seen_at ? " · last seen " + esc(rel(h.seen_at)) : ""}</span>`}</span></div>`;
  }).join("");
}

function notifyTransitions() {
  const seen = new Set();
  for (const s of allSessions()) {
    if (!s.is_claude) continue;
    const key = s.host + "\u0000" + s.target;
    seen.add(key);
    const prev = S.prevStatus.get(key);
    S.prevStatus.set(key, s.status);
    if (!prev || prev === s.status || s.status === "offline" || prev === "offline") continue;
    let msg = null;
    if (s.status === "waiting") msg = `${s.project} (${s.host}) needs you`;
    else if (prev === "working" && s.status === "idle") msg = `${s.project} (${s.host}) finished`;
    if (!msg) continue;
    const viewing = S.current && S.current.host === s.host && S.current.target === s.target && !document.hidden;
    if (viewing) continue;
    toast(msg, { onclick: () => (location.hash = sessionHash(s.host, s.target)), ms: 6000 });
    if (navigator.vibrate) navigator.vibrate(s.status === "waiting" ? [80, 60, 80] : 60);
    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      try { new Notification("Claude Manager", { body: msg, tag: key }); } catch {}
    }
  }
  for (const k of S.prevStatus.keys()) if (!seen.has(k)) S.prevStatus.delete(k);
}

// ------------------------------------------------------------------ ANSI -> HTML
const PAL16 = ["#2e3138", "#e5676f", "#8fc56b", "#d9a35a", "#5aa8ef", "#c677dd", "#4fbac6", "#d8d8d4",
  "#6b707b", "#ff7a82", "#a8de82", "#f0bf72", "#79c0ff", "#dd93f0", "#6fd6e1", "#ffffff"];
function c256(n) {
  if (n < 16) return PAL16[n];
  if (n < 232) { n -= 16; const v = (x) => (x ? 55 + x * 40 : 0); return `rgb(${v(Math.floor(n / 36))},${v(Math.floor(n / 6) % 6)},${v(n % 6)})`; }
  const l = 8 + (n - 232) * 10; return `rgb(${l},${l},${l})`;
}
function sgr(st, params) {
  const p = params === "" ? [0] : params.split(";").map((x) => (x === "" ? 0 : +x));
  for (let i = 0; i < p.length; i++) {
    const c = p[i];
    if (c === 0) { st.fg = st.bg = null; st.b = st.d = st.i = st.u = st.r = false; }
    else if (c === 1) st.b = true; else if (c === 2) st.d = true; else if (c === 3) st.i = true;
    else if (c === 4) st.u = true; else if (c === 7) st.r = true;
    else if (c === 22) st.b = st.d = false; else if (c === 23) st.i = false; else if (c === 24) st.u = false; else if (c === 27) st.r = false;
    else if (c >= 30 && c <= 37) st.fg = PAL16[c - 30]; else if (c >= 90 && c <= 97) st.fg = PAL16[c - 82];
    else if (c >= 40 && c <= 47) st.bg = PAL16[c - 40]; else if (c >= 100 && c <= 107) st.bg = PAL16[c - 92];
    else if (c === 39) st.fg = null; else if (c === 49) st.bg = null;
    else if (c === 38 || c === 48) {
      let col = null;
      if (p[i + 1] === 5) { col = c256(p[i + 2] || 0); i += 2; }
      else if (p[i + 1] === 2) { col = `rgb(${p[i + 2] || 0},${p[i + 3] || 0},${p[i + 4] || 0})`; i += 4; }
      if (c === 38) st.fg = col; else st.bg = col;
    }
  }
}
function spanFor(st, text) {
  const t = esc(text);
  let fg = st.fg, bg = st.bg;
  if (st.r) { [fg, bg] = [bg || "var(--term-bg)", fg || "var(--term-fg)"]; }
  const cls = (st.b ? " b" : "") + (st.d ? " d" : "") + (st.i ? " i" : "") + (st.u ? " u" : "");
  const style = (fg ? `color:${fg};` : "") + (bg ? `background:${bg};` : "");
  if (!cls && !style) return t;
  return `<span${cls ? ` class="${cls.trim()}"` : ""}${style ? ` style="${style}"` : ""}>${t}</span>`;
}
const CSI = /\x1b\[([0-9;?]*)([ -\/]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]/g;
function ansiToHtml(text, wrap) {
  const st = { fg: null, bg: null, b: false, d: false, i: false, u: false, r: false };
  const lines = text.replace(/\r/g, "").split("\n");
  while (lines.length && lines[lines.length - 1].replace(CSI, "").trim() === "") lines.pop();
  const out = [];
  for (const line of lines) {
    const segs = [];
    let last = 0, m;
    CSI.lastIndex = 0;
    while ((m = CSI.exec(line))) {
      if (m.index > last) segs.push([{ ...st }, line.slice(last, m.index)]);
      if (m[3] === "m" && m[2] === "") sgr(st, m[1]);
      last = CSI.lastIndex;
    }
    if (last < line.length) segs.push([{ ...st }, line.slice(last)]);
    // trim trailing padding that has no background colour
    while (segs.length) {
      const [s0, t] = segs[segs.length - 1];
      if (s0.bg || s0.r) break;
      const tt = t.replace(/\s+$/, "");
      if (tt) { segs[segs.length - 1][1] = tt; break; }
      segs.pop();
    }
    const plain = segs.map((x) => x[1]).join("");
    if (wrap && /^\s*[─━═]{8,}\s*$/.test(plain)) { out.push('<div class="rule"></div>'); continue; }
    out.push(segs.map(([s0, t]) => spanFor(s0, t)).join(""));
  }
  return out.join("\n").replace(/\n?<div class="rule"><\/div>\n?/g, '<div class="rule"></div>');
}

// ------------------------------------------------------------------ session view
let paneTimer = null, paneBusy = false;
function findSession(host, target) {
  return allSessions().find((s) => s.host === host && s.target === target);
}
function openSession(host, target) {
  const same = S.current && S.current.host === host && S.current.target === target;
  S.current = { host, target };
  $(".main").classList.add("has-session");
  $("#session-view").hidden = false;
  if (!same) {
    S.paneHash = ""; S.lastPane = null;
    $("#term").innerHTML = '<span class="d">loading…</span>';
    $("#prompt").value = store.get("draft:" + host + ":" + target, "");
    autoGrow(); updateCount();
  }
  renderSessionHeader();
  renderSessions();
  pollPane(true);
  scheduleState();
}
function closeSession() {
  S.current = null;
  clearTimeout(paneTimer);
  $(".main").classList.remove("has-session");
  $("#session-view").hidden = true;
  renderSessions();
  scheduleState();
}
function renderSessionHeader() {
  if (!S.current) return;
  const s = findSession(S.current.host, S.current.target);
  $("#s-title").textContent = s ? (s.project || s.session) : S.current.target;
  $("#s-meta").textContent = s
    ? [STATUS_LABEL[s.status] || s.status, S.current.host, shortAcct(s.account), "tmux " + s.target].filter(Boolean).join(" · ")
    : S.current.host + " · " + S.current.target;
  $("#dec-btn").disabled = !(s && s.decisions_path);
  $("#dec-btn").title = s && s.decisions_path ? s.decisions_path : "No DECISIONS.md found at or above this folder";
}
async function pollPane(force) {
  clearTimeout(paneTimer);
  if (!S.current) return;
  if (!document.hidden && (!paneBusy || force)) {
    paneBusy = true;
    const cur = S.current;
    try {
      const p = await api("GET", "/hub/pane", { host: cur.host, target: cur.target, history: 400 });
      if (S.current === cur && (p.hash !== S.paneHash || force)) {
        S.paneHash = p.hash; S.lastPane = p;
        renderPane();
      }
    } catch (e) {
      if (S.current === cur && e.status !== 401) $("#term").innerHTML = `<span style="color:#e5676f">${esc(e.message)}</span>`;
      if (e.status === 401) showLogin("Token rejected.");
    } finally { paneBusy = false; }
  }
  const fast = Date.now() < S.fastUntil;
  paneTimer = setTimeout(() => pollPane(false), fast ? 400 : 1000);
}
let charRatio = 0;
function monoRatio() {
  if (charRatio) return charRatio;
  const c = document.createElement("canvas").getContext("2d");
  c.font = "100px " + getComputedStyle($("#term")).fontFamily;
  charRatio = c.measureText("MMMMMMMMMM").width / 1000 || 0.6;
  return charRatio;
}
function fitFont(cols) {
  const w = $("#term-wrap").clientWidth - 22;
  return Math.max(3, Math.min(16, w / (cols * monoRatio())));
}
function effectiveMode() {
  if (S.termMode !== "auto") return S.termMode;
  const cols = (S.lastPane && S.lastPane.width) || 120;
  return fitFont(cols) >= 8 ? "fit" : "wrap";
}
function renderPane() {
  const p = S.lastPane;
  if (!p) return;
  const term = $("#term");
  const mode = effectiveMode();
  const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 60;
  term.classList.toggle("wrap", mode === "wrap");
  term.style.setProperty("--term-font", (mode === "fit" ? fitFont(p.width) : S.wrapFont) + "px");
  term.innerHTML = ansiToHtml(p.text, mode === "wrap");
  if (atBottom || !term.dataset.init) { term.scrollTop = term.scrollHeight; term.dataset.init = "1"; }
  $("#jump-btn").hidden = term.scrollHeight - term.scrollTop - term.clientHeight < 60;
}

async function sendPrompt(ev) {
  ev && ev.preventDefault();
  if (!S.current) return;
  const text = $("#prompt").value;
  const submit = !$("#no-submit").checked;
  if (!text && !submit) return;
  const cur = S.current;
  $("#send-btn").disabled = true;
  try {
    await api("POST", "/hub/send", null, { host: cur.host, target: cur.target, text, submit });
    $("#prompt").value = ""; store.del("draft:" + cur.host + ":" + cur.target);
    autoGrow(); updateCount();
    toast(submit ? "Sent" : "Pasted (not submitted)", { ms: 1500 });
    S.fastUntil = Date.now() + 6000;
    $("#term").dataset.init = "";
    pollPane(true);
  } catch (e) {
    toast("Send failed: " + e.message, { err: true, ms: 6000 });
  } finally { $("#send-btn").disabled = false; }
}
async function sendKey(k) {
  if (!S.current) return;
  try {
    await api("POST", "/hub/keys", null, { host: S.current.host, target: S.current.target, keys: [k] });
    S.fastUntil = Date.now() + 3000;
    pollPane(true);
  } catch (e) { toast(e.message, { err: true }); }
}
function autoGrow() {
  const ta = $("#prompt");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight + 2, window.innerHeight * 0.4) + "px";
}
function updateCount() {
  const n = $("#prompt").value.length;
  $("#prompt-count").textContent = n > 200 ? `${n.toLocaleString()} chars ≈ ${fmtTokens(Math.round(n / 4))} tok` : "";
}

// ------------------------------------------------------------------ DECISIONS sheet
async function openDecisions(host, dir, fromSession) {
  $("#dec-sheet").hidden = false;
  $("#dec-list").innerHTML = '<div class="empty">Loading…</div>';
  $("#dec-insert").hidden = !fromSession;
  S.dec = { host, dir, data: null, selected: new Set(), bodies: new Map(), expanded: new Set(), loading: new Set(), fromSession };
  try {
    const data = await api("GET", "/hub/decisions", { host, dir, bodies: 25 });
    S.dec.data = data;
    data.entries.forEach((e) => { if (e.body != null) S.dec.bodies.set(e.index, e.body); });
    $("#dec-title").textContent = data.project + " · " + data.count + (data.count === 1 ? " entry" : " entries");
    $("#dec-path").textContent = host + " · " + data.path;
    const url = ((S.state && S.state.prefs && S.state.prefs.perplexity) || {})[data.project] || "";
    $("#pplx-url").value = url;
    syncPplxLink();
    const n = store.get("lastN:" + data.project, 1);
    setLastN(Math.min(n, data.count));
  } catch (e) {
    $("#dec-list").innerHTML = `<div class="empty error">${esc(e.message)}</div>`;
  }
}
function closeDecisions() { $("#dec-sheet").hidden = true; S.dec = null; }
function syncPplxLink() {
  const url = $("#pplx-url").value.trim();
  const a = $("#pplx-open");
  if (/^https?:\/\//.test(url)) { a.href = url; a.classList.remove("disabled"); a.removeAttribute("aria-disabled"); }
  else { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); }
}
async function savePplx() {
  if (!S.dec || !S.dec.data) return;
  const url = $("#pplx-url").value.trim();
  if (url && !/^https?:\/\//.test(url)) return toast("Thread URL must start with https://", { err: true });
  try {
    const prefs = await api("POST", "/hub/prefs", null, { section: "perplexity", key: S.dec.data.project, value: url });
    if (S.state) S.state.prefs = prefs;
    syncPplxLink();
  } catch (e) { toast("Could not save: " + e.message, { err: true }); }
}
function setLastN(n) {
  const d = S.dec;
  if (!d || !d.data) return;
  n = Math.max(0, Math.min(d.data.count, n | 0));
  $("#n-val").value = n;
  d.selected = new Set(Array.from({ length: n }, (_, i) => i));
  store.set("lastN:" + d.data.project, n);
  ensureBodies([...d.selected]);
  renderDecisions();
}
async function ensureBodies(indexes) {
  const d = S.dec;
  const missing = indexes.filter((i) => !d.bodies.has(i) && !d.loading.has(i));
  if (!missing.length) return;
  missing.forEach((i) => d.loading.add(i));
  updateDecSum();
  try {
    const res = await api("GET", "/hub/decisions/bodies", { host: d.host, dir: d.data.path, i: missing.join(",") });
    if (S.dec !== d) return;
    for (const [k, v] of Object.entries(res.bodies)) d.bodies.set(+k, v);
  } catch (e) { toast("Could not load entries: " + e.message, { err: true }); }
  finally { missing.forEach((i) => d.loading.delete(i)); if (S.dec === d) renderDecisions(); }
}
function renderDecisions() {
  const d = S.dec;
  if (!d || !d.data) return;
  const list = $("#dec-list");
  const scroll = list.scrollTop;
  list.innerHTML = d.data.entries.map((e) => {
    const sel = d.selected.has(e.index), open = d.expanded.has(e.index);
    const body = open ? (d.bodies.has(e.index) ? esc(d.bodies.get(e.index)) : "loading…") : "";
    const when = [e.date, e.time].filter(Boolean).join(" ");
    return `<div class="dec${sel ? " selected" : ""}" data-i="${e.index}">
      <div class="dec-row"><input type="checkbox" ${sel ? "checked" : ""} aria-label="include ${esc(e.id || "entry " + (e.index + 1))}">
        <div class="dec-main">
          <div class="dec-top"><span class="id">${esc(e.id || "#" + (e.index + 1))}</span><span>${esc(when)}</span><span>≈${fmtTokens(e.tokens_est)} tok</span><span>${open ? "▴" : "▾"}</span></div>
          <div class="dec-title">${esc(e.title)}</div>
          ${e.tags.length ? `<div class="dec-tag">${esc(e.tags.join(" · "))}</div>` : ""}
        </div></div>
      ${open ? `<div class="dec-body">${body}</div>` : ""}
    </div>`;
  }).join("") || '<div class="empty">No "## " entries found in this file.</div>';
  list.scrollTop = scroll;
  updateDecSum();
}
function updateDecSum() {
  const d = S.dec;
  if (!d || !d.data) return;
  const sel = [...d.selected];
  const tok = sel.reduce((a, i) => a + (d.data.entries[i] ? d.data.entries[i].tokens_est : 0), 0);
  const loading = sel.some((i) => !d.bodies.has(i));
  $("#dec-sum").textContent = sel.length ? `${sel.length} selected · ≈${fmtTokens(tok)} tokens${loading ? " · loading…" : ""}` : "nothing selected";
  ["#dec-copy", "#dec-pplx", "#dec-insert"].forEach((id) => ($(id).disabled = !sel.length || loading));
}
function selectedText() {
  const d = S.dec;
  return [...d.selected].sort((a, b) => a - b).map((i) => d.bodies.get(i).trimEnd()).join("\n\n");
}

// ------------------------------------------------------------------ projects + new session
async function loadProjects() {
  const box = $("#projects");
  box.innerHTML = '<div class="empty">Loading…</div>';
  try {
    S.projects = await api("GET", "/hub/projects");
  } catch (e) { box.innerHTML = `<div class="empty error">${esc(e.message)}</div>`; return; }
  const seen = new Set(), rows = [];
  const add = (p) => { const k = p.host + "\u0000" + p.dir.toLowerCase(); if (!seen.has(k)) { seen.add(k); rows.push(p); } };
  for (const s of allSessions()) if (s.is_claude && s.decisions_path) {
    const dir = s.decisions_path.replace(/[\\/][^\\/]+$/, "");
    add({ host: s.host, name: s.project, dir, decisions_path: s.decisions_path });
  }
  S.projects.forEach(add);
  rows.sort((a, b) => (!!b.decisions_path - !!a.decisions_path) || a.name.localeCompare(b.name));
  box.innerHTML = rows.length ? rows.map((p, i) => `<div class="card" data-pi="${i}" style="cursor:default">
      <span class="dot ${p.decisions_path ? "idle" : "shell"}"></span><span class="title">${esc(p.name)}</span><span class="badge">${esc(p.host)}</span>
      <span class="meta">${esc(p.dir)}</span>
      <div class="actions">${p.decisions_path ? `<button class="btn small" data-act="dec">DECISIONS</button>` : `<span class="muted small">no DECISIONS.md</span>`}
      <button class="btn small" data-act="new">New session here</button></div></div>`).join("")
    : `<div class="empty">No projects yet. Add folders to <code>project_roots</code> in each machine's config.</div>`;
  box._rows = rows;
}
function openNew(host, cwd) {
  const hosts = ((S.state && S.state.hosts) || []).filter((h) => h.ok && h.info);
  if (!hosts.length) return toast("No machine online", { err: true });
  $("#new-host").innerHTML = hosts.map((h) => `<option>${esc(h.name)}</option>`).join("");
  $("#new-host").value = host && hosts.some((h) => h.name === host) ? host : (S.current ? S.current.host : hosts[0].name);
  fillNewHost();
  $("#new-cwd").value = cwd || "";
  $("#new-name").value = "";
  $("#new-error").hidden = true;
  $("#new-sheet").hidden = false;
}
async function fillNewHost() {
  const name = $("#new-host").value;
  const h = S.state.hosts.find((x) => x.name === name);
  const accts = (h && h.info && h.info.accounts) || [];
  $("#new-account").innerHTML = accts.map((a) => `<option value="${esc(a.dir)}">${esc(a.label)}${a.email ? " · " + esc(a.email) : ""}</option>`).join("")
    || `<option value="">default</option>`;
  const dirs = new Set(allSessions().filter((s) => s.host === name && s.cwd).map((s) => s.cwd));
  if (!S.projects) { try { S.projects = await api("GET", "/hub/projects"); } catch { S.projects = []; } }
  S.projects.filter((p) => p.host === name).forEach((p) => dirs.add(p.dir));
  $("#new-cwd-list").innerHTML = [...dirs].map((d) => `<option value="${esc(d)}">`).join("");
}
async function submitNew(ev) {
  ev.preventDefault();
  const host = $("#new-host").value;
  const body = { host, cwd: $("#new-cwd").value.trim(), account_dir: $("#new-account").value, name: $("#new-name").value.trim(), resume: $("#new-resume").checked };
  const btn = $("#new-form button[type=submit]");
  btn.disabled = true;
  try {
    const res = await api("POST", "/hub/new", null, body);
    $("#new-sheet").hidden = true;
    toast("Started " + res.session);
    await refreshState();
    const s = allSessions().find((x) => x.host === host && x.session === res.session);
    if (s) location.hash = sessionHash(host, s.target);
  } catch (e) {
    $("#new-error").textContent = e.message; $("#new-error").hidden = false;
  } finally { btn.disabled = false; }
}

// ------------------------------------------------------------------ login / boot
function showLogin(msg) {
  $("#app").hidden = true; $("#login").hidden = false;
  $("#login-error").hidden = !msg; $("#login-error").textContent = msg || "";
  clearTimeout(stateTimer); clearTimeout(paneTimer);
}
async function start() {
  try {
    S.state = await api("GET", "/hub/state");
  } catch (e) {
    return showLogin(e.status === 401 ? (S.token ? "Token rejected." : "") : "Cannot reach the hub: " + e.message);
  }
  $("#login").hidden = true; $("#app").hidden = false;
  if (store.get("sideCollapsed", false) && innerWidth >= 900) $("#app").classList.add("side-collapsed");
  renderSide(); renderSessions(); notifyTransitions();
  route();
  scheduleState();
}

function bind() {
  $("#login-form").onsubmit = (e) => { e.preventDefault(); S.token = $("#login-token").value.trim(); store.set("token", S.token); start(); };
  $("#logout-btn").onclick = () => { store.del("token"); S.token = ""; showLogin(""); };
  $("#side-toggle").onclick = () => {
    const app = $("#app");
    if (innerWidth < 900) app.classList.toggle("side-open");
    else { app.classList.toggle("side-collapsed"); store.set("sideCollapsed", app.classList.contains("side-collapsed")); }
  };
  $("#side-close").onclick = $("#scrim").onclick = () => $("#app").classList.remove("side-open");
  $("#notify-btn").onclick = async () => {
    if (!("Notification" in window) || !window.isSecureContext) return toast("System notifications need HTTPS - see README (tailscale serve). In-page alerts still work.", { ms: 6000 });
    const r = await Notification.requestPermission();
    toast(r === "granted" ? "Notifications on" : "Notifications blocked");
  };
  $("#filters").onclick = (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    S.filter = b.dataset.f;
    $$("#filters [data-f]").forEach((x) => x.classList.toggle("on", x === b));
    renderSessions();
  };
  $("#show-shells").checked = S.showShells;
  $("#show-shells").onchange = (e) => { S.showShells = e.target.checked; store.set("showShells", S.showShells); renderSessions(); };
  $("#sessions").onclick = (e) => {
    const c = e.target.closest(".card[data-target]");
    if (c) location.hash = sessionHash(c.dataset.host, c.dataset.target);
  };
  $("#back-btn").onclick = () => { if (history.length > 1 && location.hash.startsWith("#/s/")) history.back(); else location.hash = "#/"; };
  $("#projects-btn").onclick = () => (location.hash = "#/projects");
  $("#projects-back").onclick = () => (location.hash = "#/");
  $("#new-btn").onclick = () => openNew();
  $("#projects").onclick = (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const p = $("#projects")._rows[+b.closest("[data-pi]").dataset.pi];
    if (b.dataset.act === "dec") openDecisions(p.host, p.dir, false);
    else openNew(p.host, p.dir);
  };

  $("#term").onscroll = () => { const t = $("#term"); $("#jump-btn").hidden = t.scrollHeight - t.scrollTop - t.clientHeight < 60; };
  $("#jump-btn").onclick = () => { const t = $("#term"); t.scrollTop = t.scrollHeight; };
  $("#fit-btn").onclick = () => { S.termMode = effectiveMode() === "fit" ? "wrap" : "fit"; store.set("termMode", S.termMode); renderPane(); };
  const bump = (d) => { S.wrapFont = Math.max(8, Math.min(20, S.wrapFont + d)); store.set("wrapFont", S.wrapFont); S.termMode = "wrap"; store.set("termMode", "wrap"); renderPane(); };
  $("#font-up").onclick = () => bump(1);
  $("#font-down").onclick = () => bump(-1);
  window.addEventListener("resize", () => { if (S.lastPane) renderPane(); });
  $("#keybar").onclick = (e) => { const b = e.target.closest("[data-k]"); if (b) sendKey(b.dataset.k); };
  $("#composer").onsubmit = sendPrompt;
  $("#prompt").addEventListener("input", () => {
    autoGrow(); updateCount();
    if (S.current) store.set("draft:" + S.current.host + ":" + S.current.target, $("#prompt").value);
  });
  $("#prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendPrompt(e); });
  $("#dec-btn").onclick = () => {
    const s = S.current && findSession(S.current.host, S.current.target);
    if (s && s.decisions_path) openDecisions(s.host, s.decisions_path, true);
  };

  // decisions sheet
  $("#dec-close").onclick = closeDecisions;
  $("#dec-sheet").onclick = (e) => { if (e.target.id === "dec-sheet") closeDecisions(); };
  $("#n-minus").onclick = () => setLastN(+$("#n-val").value - 1);
  $("#n-plus").onclick = () => setLastN(+$("#n-val").value + 1);
  $("#n-val").onchange = () => setLastN(+$("#n-val").value);
  $("#n-clear").onclick = () => setLastN(0);
  $("#pplx-url").addEventListener("change", savePplx);
  $("#pplx-url").addEventListener("input", syncPplxLink);
  $("#pplx-open").onclick = (e) => { if (!$("#pplx-open").getAttribute("href")) { e.preventDefault(); $("#pplx-url").focus(); } };
  $("#dec-list").onclick = (e) => {
    const row = e.target.closest(".dec");
    if (!row || !S.dec) return;
    const i = +row.dataset.i, d = S.dec;
    if (e.target.matches("input[type=checkbox]")) {
      if (e.target.checked) d.selected.add(i); else d.selected.delete(i);
      ensureBodies([i]);
      $("#n-val").value = d.selected.size;
      renderDecisions();
    } else if (e.target.closest(".dec-main")) {
      if (d.expanded.has(i)) d.expanded.delete(i); else { d.expanded.add(i); ensureBodies([i]); }
      renderDecisions();
    }
  };
  $("#dec-copy").onclick = () => {
    const t = selectedText();
    copyText(t).then((ok) => toast(ok ? `Copied ${S.dec ? S.dec.selected.size : ""} entries` : "Copy failed - long-press to select instead", { err: !ok }));
  };
  $("#dec-pplx").onclick = () => {
    const url = $("#pplx-url").value.trim();
    if (!/^https?:\/\//.test(url)) { toast("Paste this project's Perplexity thread URL first", { err: true }); $("#pplx-url").focus(); return; }
    const t = selectedText();
    const p = copyText(t);
    window.open(url, "_blank", "noopener");   // same tap, so popup blockers allow it
    p.then((ok) => toast(ok ? "Copied - paste it into the thread" : "Copy failed", { err: !ok }));
  };
  $("#dec-insert").onclick = () => {
    const ta = $("#prompt"), t = selectedText();
    const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? ta.value.length;
    const before = ta.value.slice(0, a), after = ta.value.slice(b);
    const pad1 = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    const pad2 = after && !after.startsWith("\n") ? "\n\n" : "";
    ta.value = before + pad1 + t + pad2 + after;
    ta.dispatchEvent(new Event("input"));
    closeDecisions();
    ta.focus();
  };

  // new session sheet
  $("#new-close").onclick = () => ($("#new-sheet").hidden = true);
  $("#new-sheet").onclick = (e) => { if (e.target.id === "new-sheet") $("#new-sheet").hidden = true; };
  $("#new-host").onchange = fillNewHost;
  $("#new-form").onsubmit = submitNew;

  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && S.token && !$("#app").hidden) { refreshState(); pollPane(true); } });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (!$("#dec-sheet").hidden) closeDecisions(); else if (!$("#new-sheet").hidden) $("#new-sheet").hidden = true; } });
}

window.addEventListener("error", (e) => toast("UI error: " + e.message, { err: true, ms: 8000 }));
window.addEventListener("unhandledrejection", (e) => toast("UI error: " + ((e.reason && e.reason.message) || e.reason), { err: true, ms: 8000 }));

// token may arrive as #token=... (handy for opening on a phone once)
(function boot() {
  const m = location.hash.match(/^#token=([^&]+)(?:&to=(.*))?$/);
  if (m) {
    S.token = decodeURIComponent(m[1]); store.set("token", S.token);
    history.replaceState(null, "", location.pathname + (m[2] ? "#" + decodeURIComponent(m[2]) : ""));
  }
  bind();
  if (S.token) start(); else showLogin("");
})();
