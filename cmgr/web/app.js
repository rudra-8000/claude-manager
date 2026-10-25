"use strict";
// Claude Manager web UI. Plain JS, no build step. Everything goes through /hub/* on the hub machine.

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
  state: null,
  filter: "all",
  showShells: store.get("showShells", false),
  showExternal: store.get("showExternal", false),   // sessions outside tmux are read-only; off by default
  current: null,          // {host, target}
  tab: store.get("tab", "chat"),
  chat: null,             // {key, items, start, end, error, open:Set, folds:Set}
  pane: null,             // last /pane result
  paneHash: "",
  prevStatus: new Map(),
  projects: null,
  dec: null,
  termMode: store.get("termMode", "auto"),
  wrapFont: store.get("wrapFont", 12.5),
  fastUntil: 0,
};

// ------------------------------------------------------------------ API
class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(method, path, params, body) {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const res = await fetch(path + qs, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new ApiError((data && data.error) || "HTTP " + res.status, res.status);
  return data;
}

// ------------------------------------------------------------------ small helpers
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
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => true, () => copyFallback(text));
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
const atBottom = (el) => el.scrollHeight - el.scrollTop - el.clientHeight < 80;
const hidden = () => new Set((S.state && S.state.hidden_accounts) || []);

// ------------------------------------------------------------------ routing
function route() {
  const h = location.hash || "#/";
  const m = h.match(/^#\/s\/([^/]+)\/(.+)$/);
  const app = $("#app");
  $("#projects-view").hidden = h !== "#/projects";
  if (m) {
    $("#empty-view").hidden = true;
    app.classList.add("mid-open");
    openSession(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
  } else {
    closeSession();
    const proj = h === "#/projects";
    app.classList.toggle("mid-open", proj);
    $("#empty-view").hidden = proj;
    if (proj) loadProjects();
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
    renderUsage(); renderSessions(); notifyTransitions();
    // don't redraw the devices panel under the user's fingers (that wiped half-typed SSH targets)
    if (!$("#devices-sheet").hidden && !$("#devices-sheet").contains(document.activeElement)) renderDevices();
    if (S.current) renderSessionHeader();
  } catch (e) {
    $("#sync-dot").className = "sync-dot bad";
    if (e.status === 401) showLogin(e.message);
  } finally { stateBusy = false; }
}
function scheduleState() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(async () => { if (!document.hidden) await refreshState(); scheduleState(); }, S.current ? 6000 : 4000);
}
function allSessions() {
  const out = [];
  for (const h of (S.state && S.state.hosts) || []) {
    for (const s of h.sessions || []) out.push({ ...s, host: h.name, status: h.ok ? s.status : "offline", hostOk: h.ok });
  }
  return out;
}
function findSession(host, target) { return allSessions().find((s) => s.host === host && s.target === target); }
const ORDER = { waiting: 0, working: 1, idle: 2, unknown: 3, offline: 4, shell: 5, dead: 6 };
const STATUS_LABEL = { waiting: "needs you", working: "working", idle: "idle", shell: "shell", offline: "offline", dead: "exited", unknown: "?" };

function visibleSessions() {
  const hid = hidden();
  return allSessions().filter((s) => (s.is_claude || S.showShells) && !(s.account && hid.has(s.account.key))
    && !(s.external && (s.parked_by || s.duplicate_of || !S.showExternal)));
}
function renderSessions() {
  const list = visibleSessions();
  const counts = { waiting: 0, working: 0 };
  list.forEach((s) => { if (s.status in counts) counts[s.status]++; });
  $("#n-waiting").textContent = counts.waiting || "";
  $("#n-working").textContent = counts.working || "";
  document.title = (counts.waiting ? `(${counts.waiting}) ` : "") + "Claude Manager";
  const box = $("#sessions");
  const hosts = (S.state && S.state.hosts) || [];
  const groups = hosts.map((h) => ({ h, items: list.filter((s) => s.host === h.name && (S.filter === "all" || s.status === S.filter))
    .sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.project.localeCompare(b.project)) }));
  const devs = (S.state && S.state.devices) || [];
  const off = devs.some((d) => d.enabled && d.status !== "this machine") ? 0
    : devs.filter((d) => !d.enabled && d.online && d.status !== "this machine").length;
  let html = "";
  for (const { h, items } of groups) {
    if (!items.length && S.filter !== "all") continue;
    html += `<div class="group-label"><span class="dot ${h.ok ? "on" : ""}"></span>${esc(h.name)}${h.ok ? "" : ` · <span class="error">${esc(h.error || "offline")}</span>`}</div>`;
    if (!items.length) { html += `<div class="muted small" style="padding:0 6px">no Claude sessions</div>`; continue; }
    html += items.map((s) => {
      const active = S.current && S.current.host === s.host && S.current.target === s.target;
      const jobs = (s.bg_jobs || []).length;
      const title = titleOf(s), folder = sameName(title, s.project) ? "" : s.project;
      const meta = [folder, shortAcct(s.account), where(s), jobs ? `+${jobs} background job${jobs > 1 ? "s" : ""}` : ""].filter(Boolean).map(esc).join(" · ");
      return `<button class="card${active ? " active" : ""}" data-host="${esc(s.host)}" data-target="${esc(s.target)}">
        <span class="dot ${esc(s.status)}"></span><span class="title">${esc(title)}</span>
        <span class="badge ${esc(s.status)}">${esc(STATUS_LABEL[s.status] || s.status)}</span>
        <span class="meta">${meta}</span>${s.preview ? `<span class="preview">${esc(s.preview)}</span>` : ""}</button>`;
    }).join("");
  }
  if (off) html += `<div class="muted small" style="padding:10px 6px">Only this machine is shown. Tap ⚙ to add your other devices (${off} online).</div>`;
  box.innerHTML = html || `<div class="empty">No sessions.</div>`;
}

function renderUsage() {
  const st = S.state;
  if (!st) return;
  const hid = hidden();
  $("#accounts").innerHTML = st.accounts.length ? st.accounts.map((u) => {
    const a = u.account;
    const limits = (u.limits || []).map((l) => {
      const p = l.percent == null ? null : Math.round(l.percent);
      const cls = l.severity === "critical" || p >= 90 ? "danger" : l.severity === "warning" || p >= 70 ? "warn" : "";
      const reset = l.resets_at ? `resets ${rel(Date.parse(l.resets_at) / 1000)} · ${clock(Date.parse(l.resets_at) / 1000)}` : "not started";
      return `<div class="limit"><div class="limit-top"><span>${esc(l.label)}</span><b>${p == null ? "–" : p + "%"}</b></div>
        <div class="bar ${cls}"><i style="width:${Math.min(100, Math.max(0, p || 0))}%"></i></div><div class="limit-reset">${esc(reset)}</div></div>`;
    }).join("");
    const notes = [];
    if (u.error) notes.push(`<div class="note err">${esc(u.error)}</div>`);
    if (u.fetched_at) notes.push(`<div class="note">updated ${esc(rel(u.fetched_at))}${u.stale ? " (stale)" : ""} · on ${esc(u.hosts.join(", "))}</div>`);
    else if (!u.error) notes.push(`<div class="note">fetching…</div>`);
    return `<div class="acct${hid.has(a.key) ? " dim" : ""}"><div class="acct-head"><span class="stack"><span class="acct-name" title="${esc(a.dir)}">${esc(a.label)}</span>
      <span class="sub">${esc(a.email || "")}</span></span><span class="badge">${esc(a.subscription || "")}</span></div>${limits}${notes.join("")}</div>`;
  }).join("") : `<div class="empty">No logged-in accounts found.</div>`;
}

function notifyTransitions() {
  const seen = new Set();
  for (const s of visibleSessions()) {
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

// ------------------------------------------------------------------ ANSI -> HTML (screen tab)
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
  if (st.r) [fg, bg] = [bg || "var(--term-bg)", fg || "var(--term-fg)"];
  const cls = (st.b ? " b" : "") + (st.d ? " d" : "") + (st.i ? " i" : "") + (st.u ? " u" : "");
  const style = (fg ? `color:${fg};` : "") + (bg ? `background:${bg};` : "");
  if (!cls && !style) return t;
  return `<span${cls ? ` class="${cls.trim()}"` : ""}${style ? ` style="${style}"` : ""}>${t}</span>`;
}
const CSI = /\x1b\[([0-9;?]*)([ -\/]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]/g;
function ansiLines(text) {
  const st = { fg: null, bg: null, b: false, d: false, i: false, u: false, r: false };
  const lines = text.replace(/\r/g, "").split("\n");
  while (lines.length && lines[lines.length - 1].replace(CSI, "").trim() === "") lines.pop();
  return lines.map((line) => {
    const segs = [];
    let last = 0, m;
    CSI.lastIndex = 0;
    while ((m = CSI.exec(line))) {
      if (m.index > last) segs.push([{ ...st }, line.slice(last, m.index)]);
      if (m[3] === "m" && m[2] === "") sgr(st, m[1]);
      last = CSI.lastIndex;
    }
    if (last < line.length) segs.push([{ ...st }, line.slice(last)]);
    while (segs.length) {
      const [s0, t] = segs[segs.length - 1];
      if (s0.bg || s0.r) break;
      const tt = t.replace(/\s+$/, "");
      if (tt) { segs[segs.length - 1][1] = tt; break; }
      segs.pop();
    }
    return segs;
  });
}
function ansiToHtml(text, wrap, tail) {
  let lines = ansiLines(text);
  if (tail) lines = lines.filter((l) => l.map((x) => x[1]).join("").trim()).slice(-tail);
  const out = [];
  for (const segs of lines) {
    const plain = segs.map((x) => x[1]).join("");
    if (wrap && /^\s*[─━═]{8,}\s*$/.test(plain)) { out.push('<div class="rule"></div>'); continue; }
    out.push(segs.map(([s0, t]) => spanFor(s0, t)).join(""));
  }
  return out.join("\n").replace(/\n?<div class="rule"><\/div>\n?/g, '<div class="rule"></div>');
}

// ------------------------------------------------------------------ markdown (chat tab)
function inline(t) {
  let s = esc(t);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, n) => "<code>" + codes[n] + "</code>");
}
const BLOCK_START = /^\s*(```|~~~|#{1,6}\s|[-*+]\s|\d+[.)]\s|>)/;
const LIST = /^\s*([-*+]|\d+[.)])\s+/;
function md(src) {
  const lines = String(src).replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    const fence = l.match(/^\s*(```|~~~)/);
    if (fence) {
      const buf = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
      i++;
      out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) buf.push(lines[i++]);
      out.push("<pre>" + esc(buf.join("\n")) + "</pre>");
      continue;
    }
    const h = l.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const n = Math.min(h[1].length + 1, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (LIST.test(l)) {
      // one level of nesting: deeper-indented items go into a sub-list of the item above them
      const ind = (x) => x.match(/^\s*/)[0].length, base = ind(l);
      const items = [];
      while (i < lines.length && LIST.test(lines[i]) && ind(lines[i]) >= base) {
        const deep = ind(lines[i]) > base, ord = /^\s*\d/.test(lines[i]);
        let text = lines[i++].replace(LIST, "");
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST.test(lines[i])) text += " " + lines[i++].trim();
        if (deep && items.length) (items[items.length - 1].sub ||= { ord, li: [] }).li.push(text);
        else items.push({ text });
      }
      const tag = /^\s*\d/.test(l) ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((it) => "<li>" + inline(it.text) + (it.sub
        ? `<${it.sub.ord ? "ol" : "ul"}>` + it.sub.li.map((t) => "<li>" + inline(t) + "</li>").join("") + `</${it.sub.ord ? "ol" : "ul"}>` : "") + "</li>").join("") + `</${tag}>`);
      continue;
    }
    if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(inline(lines[i++].replace(/^\s*>\s?/, "")));
      out.push("<blockquote>" + buf.join("<br>") + "</blockquote>");
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const buf = [inline(lines[i++])];
    while (i < lines.length && lines[i].trim() && !BLOCK_START.test(lines[i]) && !/^\s*\|.*\|\s*$/.test(lines[i])) buf.push(inline(lines[i++]));
    out.push("<p>" + buf.join("<br>") + "</p>");
  }
  return out.join("");
}

// ------------------------------------------------------------------ session view
let paneTimer = null, chatTimer = null;
function openSession(host, target) {
  const key = host + "\u0000" + target;
  const same = S.current && S.current.host === host && S.current.target === target;
  S.current = { host, target };
  $("#session-view").hidden = false;
  $("#projects-view").hidden = true;
  if (!same) {
    S.pane = null; S.paneHash = "";
    S.chat = { key, items: [], start: null, end: null, error: "", open: new Set(), folds: new Set(), loading: true };
    $("#term").innerHTML = '<span class="d">loading…</span>';
    $("#term").dataset.init = "";
    $("#chat").innerHTML = '<div class="chat-note">Loading the conversation…</div>';
    $("#waiting-card").hidden = true;
    $("#prompt").value = store.get("draft:" + host + ":" + target, "");
    autoGrow(); updateCount();
    loadChat();
  }
  setTab(S.tab);
  renderSessionHeader();
  renderSessions();
  pollPane(true);
  scheduleState();
}
function closeSession() {
  S.current = null; S.chat = null;
  clearTimeout(paneTimer); clearTimeout(chatTimer);
  $("#session-view").hidden = true;
  renderSessions();
  scheduleState();
}
function renderSessionHeader() {
  if (!S.current) return;
  const s = findSession(S.current.host, S.current.target);
  $("#s-title").textContent = s ? titleOf(s) + (sameName(titleOf(s), s.project) ? "" : " · " + s.project) : S.current.target;
  $("#s-meta").textContent = s
    ? [STATUS_LABEL[s.status] || s.status, S.current.host, shortAcct(s.account), where(s)].filter(Boolean).join(" · ")
    : S.current.host + " · " + S.current.target;
  // sessions outside tmux: the chat can be read, but nothing can be typed into them from here
  const ext = isExternal();
  $(".tab[data-tab=screen]").hidden = ext;
  $("#keybar").hidden = ext;
  $("#composer").hidden = ext;
  $("#external-note").hidden = !ext;
  if (ext) {
    const rc = s && s.claude && s.claude.remote_url;
    const what = s && s.parked_by ? `A background job parked from tmux ${s.parked_by}`
      : s && s.kind === "bg" ? "A Claude Code background session" : "Running in a terminal outside tmux";
    $("#external-note").innerHTML = `${what}, so it can only be read here.
      ${rc ? `<a class="btn small primary" href="${esc(rc)}" target="_blank" rel="noopener">☁ Reply in the Claude app</a>`
           : `To prompt it from here, start it inside tmux next time.`}
      ${s && s.decisions_path ? `<button class="btn small" data-act="ext-dec">DECISIONS</button>` : ""}`;
  }
  // a tmux session's parked background job, and the way back from it
  const job = s && s.bg_jobs && s.bg_jobs[0];
  const back = s && (s.parked_by || s.duplicate_of);
  $("#job-btn").hidden = !(job || back);
  if (job) { $("#job-btn").textContent = `background job · ${STATUS_LABEL[job.status] || job.status}`; $("#job-btn").href = sessionHash(S.current.host, job.target); }
  else if (back) { $("#job-btn").textContent = "‹ tmux session"; $("#job-btn").href = sessionHash(S.current.host, back); }
  const rc = s && s.claude && s.claude.remote_url;
  $("#rc-btn").hidden = !rc;
  if (rc) $("#rc-btn").href = rc;
  $("#dec-btn").disabled = !(s && s.decisions_path);
  $("#dec-btn").title = s && s.decisions_path ? s.decisions_path : "No DECISIONS.md found at or above this folder";
}
// the name you gave the session (/rename) if any, else the project folder
const titleOf = (s) => (s.claude && s.claude.label) || s.project || s.session;
const sameName = (a, b) => String(a).toLowerCase().replace(/[-_ ]/g, "") === String(b).toLowerCase().replace(/[-_ ]/g, "");
const isExternal = () => !!(S.current && S.current.target.startsWith("proc:"));
const where = (s) => (s.external ? (s.kind === "bg" ? "background session" : "terminal, not tmux") : "tmux " + s.target);
function setTab(tab) {
  if (isExternal()) tab = "chat";
  S.tab = tab; store.set("tab", tab);
  $$(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $("#chat").hidden = tab !== "chat";
  $("#term-wrap").hidden = tab !== "screen";
  $("#screen-tools").hidden = tab !== "screen";
  if (tab === "screen") renderPane(); else renderWaiting();
  updateJump();
}
const scroller = () => (S.tab === "chat" ? $("#chat") : $("#term"));
function updateJump() { $("#jump-btn").hidden = atBottom(scroller()); }

// ---- chat
async function loadChat(older) {
  const c = S.chat, cur = S.current;
  if (!c || !cur) return;
  try {
    const params = { host: cur.host, target: cur.target };
    if (older) params.before = c.start;
    const r = await api("GET", "/hub/transcript", params);
    if (S.chat !== c) return;
    c.error = "";
    if (older) {
      c.items = r.items.concat(c.items); c.start = r.start;
      const el = $("#chat"), h0 = el.scrollHeight, t0 = el.scrollTop;
      renderChat(false);
      el.scrollTop = el.scrollHeight - h0 + t0;   // keep the reader where they were
    } else {
      c.items = r.items; c.start = r.start; c.end = r.end; c.path = r.path; c.loading = false;
      renderChat(true);
    }
  } catch (e) {
    if (S.chat !== c) return;
    c.loading = false; c.error = e.message;
    renderChat(false);
  }
  if (!older) scheduleChat();
}
function scheduleChat() {
  clearTimeout(chatTimer);
  chatTimer = setTimeout(pollChat, Date.now() < S.fastUntil ? 1000 : 2500);
}
async function pollChat() {
  const c = S.chat, cur = S.current;
  if (!c || !cur) return;
  if (c.end == null) return loadChat();          // no transcript yet: keep trying to find one
  if (document.hidden) return scheduleChat();
  try {
    const r = await api("GET", "/hub/transcript", { host: cur.host, target: cur.target, since: c.end });
    if (S.chat !== c) return;
    if (r.reset || (c.path && r.path !== c.path)) return loadChat();   // /clear or resume started a new file
    if (r.items.length) { c.items = c.items.concat(r.items); renderChat(false); }
    c.end = r.end;
  } catch (e) { /* transient; keep polling */ }
  scheduleChat();
}
function renderChat(forceBottom) {
  const c = S.chat, el = $("#chat");
  if (!c) return;
  const stick = forceBottom || atBottom(el);
  if (c.error && !c.items.length) {
    el.innerHTML = `<div class="chat-note">${esc(c.error)}<br><br><button class="btn small" data-act="to-screen">Show the screen instead</button></div>`;
    return;
  }
  const results = new Map(c.items.filter((i) => i.kind === "result").map((i) => [i.tool_id, i]));
  const parts = [];
  if (c.start > 0) parts.push(`<button class="btn small older" data-act="older">Load earlier messages</button>`);
  let run = [];
  const flush = () => {
    if (!run.length) return;
    const id = run[0].tool_id;
    if (run.length > 3 && !c.folds.has(id)) {
      const errs = run.filter((t) => (results.get(t.tool_id) || {}).is_error).length;
      const names = [...new Set(run.map((t) => t.name))].slice(0, 4).join(", ");
      parts.push(`<div class="tools-fold" data-fold="${esc(id)}">▸ ${run.length} tool calls (${esc(names)})${errs ? ` · <span class="error">${errs} failed</span>` : ""}</div>`);
    } else {
      for (const t of run) parts.push(toolHtml(t, results.get(t.tool_id), c.open.has(t.tool_id)));
    }
    run = [];
  };
  for (const it of c.items) {
    if (it.kind === "tool") { run.push(it); continue; }
    if (it.kind === "result") continue;
    flush();
    if (it.kind === "user") parts.push(`<div class="msg user${it.text.length > 900 ? " long" : ""}">${esc(it.text)}</div>`);
    else if (it.kind === "command") parts.push(`<div class="msg command">${esc(it.text)}</div>`);
    else if (it.kind === "assistant") parts.push(`<div class="msg assistant">${md(it.text)}</div>`);
  }
  flush();
  if (!c.items.length) parts.push(`<div class="chat-note">${c.loading ? "Loading…" : "No messages yet."}</div>`);
  const prevTop = el.scrollTop;
  el.innerHTML = parts.join("");
  el.scrollTop = stick ? el.scrollHeight : prevTop;
  updateJump();
}
function toolHtml(t, res, open) {
  const state = !res ? "…" : res.is_error ? "✗" : "✓";
  return `<div class="tool" data-tool="${esc(t.tool_id)}"><div class="tool-head"><span class="tool-name">${esc(t.name)}</span>
    <span class="tool-sum">${esc(t.summary)}</span><span class="tool-state${res && res.is_error ? " err" : ""}">${state}</span></div>
    ${open ? `<div class="tool-out">${esc(t.summary)}${res ? "\n\n" + esc(res.text) + (res.truncated ? "\n…(cut)" : "") : "\n\n(no result yet)"}</div>` : ""}</div>`;
}

// ---- screen
async function pollPane(force) {
  clearTimeout(paneTimer);
  if (!S.current || isExternal()) return;
  const cur = S.current;
  if (!document.hidden) {
    try {
      const p = await api("GET", "/hub/pane", { host: cur.host, target: cur.target, history: 0 });
      if (S.current === cur && (p.hash !== S.paneHash || force)) {
        S.paneHash = p.hash; S.pane = p;
        if (S.tab === "screen") renderPane();
        renderWaiting();
      }
    } catch (e) {
      if (S.current === cur && S.tab === "screen") $("#term").innerHTML = `<span style="color:#e5676f">${esc(e.message)}</span>`;
      if (e.status === 401) showLogin(e.message);
    }
  }
  const fast = Date.now() < S.fastUntil;
  paneTimer = setTimeout(() => pollPane(false), fast ? 500 : (S.tab === "screen" ? 1000 : 2000));
}
let charRatio = 0;
function monoRatio() {
  if (charRatio) return charRatio;
  const c = document.createElement("canvas").getContext("2d");
  c.font = "100px " + getComputedStyle($("#term")).fontFamily;
  charRatio = c.measureText("MMMMMMMMMM").width / 1000 || 0.6;
  return charRatio;
}
function fitFont(cols) { return Math.max(3, Math.min(16, ($(".pane-area").clientWidth - 22) / (cols * monoRatio()))); }
function effectiveMode() {
  if (S.termMode !== "auto") return S.termMode;
  return fitFont((S.pane && S.pane.width) || 120) >= 8 ? "fit" : "wrap";
}
function renderPane() {
  const p = S.pane, term = $("#term");
  if (!p) return;
  const mode = effectiveMode(), stick = atBottom(term) || !term.dataset.init;
  term.classList.toggle("wrap", mode === "wrap");
  term.style.setProperty("--term-font", (mode === "fit" ? fitFont(p.width) : S.wrapFont) + "px");
  term.innerHTML = ansiToHtml(p.text, mode === "wrap");
  if (stick) { term.scrollTop = term.scrollHeight; term.dataset.init = "1"; }
  updateJump();
}
function renderWaiting() {
  const s = S.current && findSession(S.current.host, S.current.target);
  const waiting = (S.pane && S.pane.status === "waiting") || (s && s.status === "waiting");
  const show = !!(waiting && S.tab === "chat" && S.pane);
  $("#waiting-card").hidden = !show;
  if (show) $("#waiting-screen").innerHTML = ansiToHtml(S.pane.text, true, 16);
}

// ---- sending
async function sendPrompt(ev) {
  ev && ev.preventDefault();
  if (!S.current) return;
  const text = $("#prompt").value, submit = !$("#no-submit").checked;
  if (!text && !submit) return;
  const cur = S.current;
  $("#send-btn").disabled = true;
  try {
    await api("POST", "/hub/send", null, { host: cur.host, target: cur.target, text, submit });
    $("#prompt").value = ""; store.del("draft:" + cur.host + ":" + cur.target);
    autoGrow(); updateCount();
    toast(submit ? "Sent" : "Pasted (not submitted)", { ms: 1500 });
    S.fastUntil = Date.now() + 8000;
    pollPane(true); scheduleChat();
    $("#chat").scrollTop = $("#chat").scrollHeight;
  } catch (e) { toast("Send failed: " + e.message, { err: true, ms: 6000 }); }
  finally { $("#send-btn").disabled = false; }
}
async function sendKey(k) {
  if (!S.current) return;
  try {
    await api("POST", "/hub/keys", null, { host: S.current.host, target: S.current.target, keys: [k] });
    S.fastUntil = Date.now() + 4000;
    pollPane(true);
  } catch (e) { toast(e.message, { err: true }); }
}
function autoGrow() { const ta = $("#prompt"); ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight + 2, window.innerHeight * 0.4) + "px"; }
function updateCount() {
  const n = $("#prompt").value.length;
  $("#prompt-count").textContent = n > 200 ? `${n.toLocaleString()} chars ≈ ${fmtTokens(Math.round(n / 4))} tok` : "";
}

// ------------------------------------------------------------------ devices & accounts
const S_OPEN = new Set();   // devices whose SSH form is expanded
const S_DRAFT = new Map();  // "device\u0000field" -> text typed but not saved yet
const draft = (name, f, saved) => (S_DRAFT.has(name + "\u0000" + f) ? S_DRAFT.get(name + "\u0000" + f) : saved || "");
function openDevices() { $("#devices-sheet").hidden = false; renderDevices(); }
function renderDevices() {
  const st = S.state;
  if (!st) return;
  $("#devices-hint").innerHTML = `This page runs on <b>${esc(st.hub)}</b>. Other machines are reached from there over SSH
    (key login must already work from ${esc(st.hub)} without a password; nothing gets installed), or directly if they run <code>cm</code>.`;
  $("#devices-list").innerHTML = (st.devices || []).map((d) => {
    const self = d.status === "this machine";
    const usable = self || d.mode != null;   // cm running there, an ssh alias, or SSH set up here
    const err = d.status.startsWith("error");
    const showForm = !self && (S_OPEN.has(d.name) || err);
    const os = d.os === "win32" || d.os === "windows" ? "Windows" : d.os || "";
    return `<div class="dev" data-name="${esc(d.name)}">
      <div class="dev-head"><span class="dot ${d.online ? "on" : ""}"></span><span class="dev-name">${esc(d.name)}</span>
        <span class="badge">${esc(os)}</span>
        <label class="switch" title="show this device's sessions"><input type="checkbox" data-act="toggle" data-usable="${usable ? 1 : 0}" ${d.enabled ? "checked" : ""} ${self ? "" : ""}><span></span></label></div>
      <div class="dev-status${err ? " err" : ""}">${esc(d.status)}${!self && !showForm ? ` · <a href="#" data-act="edit">${usable ? "SSH settings" : "Set up SSH"}</a>` : ""}</div>
      ${showForm ? `<div class="dev-ssh"><input data-f="ssh" placeholder="${esc(d.ssh_alias || "user@" + d.name)}" value="${esc(draft(d.name, "ssh", d.ssh))}" autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="ssh target">
        <input data-f="python" placeholder="${os === "Windows" ? "python" : "python3"}" value="${esc(draft(d.name, "python", d.python))}" autocapitalize="off" spellcheck="false" aria-label="python on that machine"></div>
        <div class="muted small">${d.ssh_alias ? `Leave empty to use <code>ssh ${esc(d.ssh_alias)}</code> from the hub's ~/.ssh/config.` : `Anything that works as <code>ssh &lt;this&gt;</code> on ${esc(st.hub)}: an alias from ~/.ssh/config or user@host.`}</div>
        <div class="dev-actions"><button class="btn small primary" data-act="ssh">${d.via === "ssh" ? "Save" : "Connect over SSH"}</button>
        ${d.via === "ssh" ? `<button class="btn small" data-act="auto">Reset</button>` : ""}</div>` : ""}
    </div>`;
  }).join("");
  const hid = hidden();
  const accts = new Map();
  for (const u of st.accounts || []) accts.set(u.account.key, u.account);
  for (const s of allSessions()) if (s.account && !accts.has(s.account.key)) accts.set(s.account.key, s.account);
  $("#accounts-list").innerHTML = [...accts.values()].map((a) => `<div class="acct-row" data-key="${esc(a.key)}">
      <span class="grow stack"><b>${esc(a.label)}</b><span class="sub">${esc(a.email || "")}</span></span>
      <label class="switch"><input type="checkbox" data-act="acct" ${hid.has(a.key) ? "" : "checked"}><span></span></label></div>`).join("")
    || `<div class="muted small">No accounts yet.</div>`;
}
async function deviceAction(e) {
  const dev = e.target.closest(".dev");
  if (!dev) return;
  const name = dev.dataset.name, act = e.target.dataset.act;
  if (act === "edit") { e.preventDefault(); S_OPEN.add(name); renderDevices(); return; }
  let body = null;
  if (act === "toggle" && e.target.dataset.usable === "0") { S_OPEN.add(name); renderDevices(); return; }   // needs SSH first
  if (act === "toggle") body = { name, enabled: e.target.checked };
  else if (act === "ssh") {
    body = { name, via: "ssh", enabled: true, ssh: $("[data-f=ssh]", dev).value.trim(), python: $("[data-f=python]", dev).value.trim() };
    S_DRAFT.delete(name + "\u0000ssh"); S_DRAFT.delete(name + "\u0000python");
    e.target.disabled = true; e.target.textContent = "Connecting…";
  } else if (act === "auto") body = { name, via: "auto" };
  if (!body) return;
  try {
    await api("POST", "/hub/device", null, body);
    await refreshState();
    if (act === "ssh") {
      const d = (S.state.devices || []).find((x) => x.name === name);
      const bad = d && d.status.startsWith("error");
      if (!bad) S_OPEN.delete(name);
      toast(bad ? d.status : `${name} connected`, { err: bad, ms: 7000 });
    }
    renderDevices();
  } catch (err) { toast(err.message, { err: true }); renderDevices(); }
}
async function accountToggle(e) {
  const row = e.target.closest(".acct-row");
  if (!row || e.target.dataset.act !== "acct") return;
  try {
    const r = await api("POST", "/hub/account", null, { key: row.dataset.key, hidden: !e.target.checked });
    S.state.hidden_accounts = r.hidden_accounts;
    renderSessions(); renderUsage();
  } catch (err) { toast(err.message, { err: true }); }
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
    $("#pplx-url").value = ((S.state && S.state.prefs && S.state.prefs.perplexity) || {})[data.project] || "";
    syncPplxLink();
    setLastN(Math.min(store.get("lastN:" + data.project, 1), data.count));
  } catch (e) { $("#dec-list").innerHTML = `<div class="empty error">${esc(e.message)}</div>`; }
}
function closeDecisions() { $("#dec-sheet").hidden = true; S.dec = null; }
function syncPplxLink() {
  const url = $("#pplx-url").value.trim(), a = $("#pplx-open");
  if (/^https?:\/\//.test(url)) a.href = url; else a.removeAttribute("href");
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
  const list = $("#dec-list"), scroll = list.scrollTop;
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
      ${open ? `<div class="dec-body">${body}</div>` : ""}</div>`;
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
  try { S.projects = await api("GET", "/hub/projects"); }
  catch (e) { box.innerHTML = `<div class="empty error">${esc(e.message)}</div>`; return; }
  const seen = new Set(), rows = [];
  const add = (p) => { const k = p.host + "\u0000" + p.dir.toLowerCase(); if (!seen.has(k)) { seen.add(k); rows.push(p); } };
  for (const s of visibleSessions()) if (s.is_claude && s.decisions_path) {
    add({ host: s.host, name: s.project, dir: s.decisions_path.replace(/[\\/][^\\/]+$/, ""), decisions_path: s.decisions_path, last_used: Date.now() / 1000 });
  }
  S.projects.forEach(add);
  rows.sort((a, b) => (b.last_used || 0) - (a.last_used || 0) || (!!b.decisions_path - !!a.decisions_path) || a.name.localeCompare(b.name));
  box.innerHTML = rows.length ? rows.map((p, i) => `<div class="card" data-pi="${i}" style="cursor:default">
      <span class="dot ${p.decisions_path ? "idle" : ""}"></span><span class="title">${esc(p.name)}</span><span class="badge">${esc(p.host)}</span>
      <span class="meta">${p.last_used ? "used " + esc(rel(p.last_used)) + " · " : ""}${esc(p.dir)}</span>
      <div class="actions">${p.decisions_path ? `<button class="btn small" data-act="dec">DECISIONS</button>` : `<span class="muted small">no DECISIONS.md</span>`}
      <button class="btn small" data-act="new">New session here</button></div></div>`).join("")
    : `<div class="empty">No projects yet.</div>`;
  box._rows = rows;
}
function openNew(host, cwd) {
  const hosts = ((S.state && S.state.hosts) || []).filter((h) => h.ok && h.info);
  if (!hosts.length) return toast("No device online", { err: true });
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
  } catch (e) { $("#new-error").textContent = e.message; $("#new-error").hidden = false; }
  finally { btn.disabled = false; }
}

// ------------------------------------------------------------------ login / boot
function showLogin(msg) {
  $("#app").hidden = true; $("#login").hidden = false;
  $("#login-error").hidden = !msg; $("#login-error").textContent = msg || "";
  clearTimeout(stateTimer); clearTimeout(paneTimer); clearTimeout(chatTimer);
}
async function start() {
  try { S.state = await api("GET", "/hub/state"); }
  catch (e) { return showLogin(e.status === 401 ? e.message : "Cannot reach the server: " + e.message); }
  $("#login").hidden = true; $("#app").hidden = false;
  $("#logout-btn").hidden = !S.token;
  if (store.get("usageHidden", false)) $("#app").classList.add("usage-hidden");
  renderUsage(); renderSessions(); notifyTransitions();
  route();
  scheduleState();
}

function bind() {
  $("#login-form").onsubmit = (e) => { e.preventDefault(); S.token = $("#login-token").value.trim(); store.set("token", S.token); start(); };
  $("#logout-btn").onclick = () => { store.del("token"); S.token = ""; showLogin(""); };
  const wide = () => innerWidth >= 1200;
  $("#usage-btn").onclick = () => {
    const app = $("#app");
    if (wide()) { app.classList.toggle("usage-hidden"); store.set("usageHidden", app.classList.contains("usage-hidden")); }
    else app.classList.toggle("usage-open");
  };
  $("#usage-close").onclick = $("#scrim").onclick = () => $("#app").classList.remove("usage-open");
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
  $("#show-external").checked = S.showExternal;
  $("#show-external").onchange = (e) => { S.showExternal = e.target.checked; store.set("showExternal", S.showExternal); renderSessions(); };
  $("#show-shells").checked = S.showShells;
  $("#show-shells").onchange = (e) => { S.showShells = e.target.checked; store.set("showShells", S.showShells); renderSessions(); };
  $("#sessions").onclick = (e) => { const c = e.target.closest(".card[data-target]"); if (c) location.hash = sessionHash(c.dataset.host, c.dataset.target); };
  $("#back-btn").onclick = () => { location.hash = "#/"; };
  $("#projects-btn").onclick = () => (location.hash = "#/projects");
  $("#projects-back").onclick = () => (location.hash = "#/");
  $("#new-btn").onclick = () => openNew();
  $("#devices-btn").onclick = openDevices;
  $("#devices-close").onclick = () => ($("#devices-sheet").hidden = true);
  $("#devices-sheet").onclick = (e) => { if (e.target.id === "devices-sheet") $("#devices-sheet").hidden = true; };
  $("#devices-list").addEventListener("click", (e) => { if (e.target.dataset.act && e.target.dataset.act !== "toggle") deviceAction(e); });
  $("#devices-list").addEventListener("change", (e) => { if (e.target.dataset.act === "toggle") deviceAction(e); });
  $("#accounts-list").addEventListener("change", accountToggle);
  $("#devices-list").addEventListener("input", (e) => {
    const dev = e.target.closest(".dev");
    if (dev && e.target.dataset.f) S_DRAFT.set(dev.dataset.name + "\u0000" + e.target.dataset.f, e.target.value);
  });
  $("#devices-list").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !e.target.dataset.f) return;
    e.preventDefault();
    const b = e.target.closest(".dev").querySelector("[data-act=ssh]");
    if (b) b.click();
  });
  $("#projects").onclick = (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const p = $("#projects")._rows[+b.closest("[data-pi]").dataset.pi];
    if (b.dataset.act === "dec") openDecisions(p.host, p.dir, false); else openNew(p.host, p.dir);
  };

  // session view
  $$(".tab").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
  $("#chat").onscroll = updateJump;
  $("#term").onscroll = updateJump;
  $("#jump-btn").onclick = () => { const el = scroller(); el.scrollTop = el.scrollHeight; updateJump(); };
  $("#chat").onclick = (e) => {
    const c = S.chat;
    if (!c) return;
    const act = e.target.closest("[data-act]");
    if (act && act.dataset.act === "older") { act.disabled = true; act.textContent = "Loading…"; return loadChat(true); }
    if (act && act.dataset.act === "to-screen") return setTab("screen");
    const fold = e.target.closest("[data-fold]");
    if (fold) { c.folds.add(fold.dataset.fold); return renderChat(false); }
    const tool = e.target.closest(".tool-head");
    if (tool) { const id = tool.parentElement.dataset.tool; if (c.open.has(id)) c.open.delete(id); else c.open.add(id); return renderChat(false); }
    const long = e.target.closest(".msg.user.long");
    if (long) long.classList.toggle("open");
  };
  $("#fit-btn").onclick = () => { S.termMode = effectiveMode() === "fit" ? "wrap" : "fit"; store.set("termMode", S.termMode); renderPane(); };
  const bump = (d) => { S.wrapFont = Math.max(8, Math.min(20, S.wrapFont + d)); store.set("wrapFont", S.wrapFont); S.termMode = "wrap"; store.set("termMode", "wrap"); renderPane(); };
  $("#font-up").onclick = () => bump(1);
  $("#font-down").onclick = () => bump(-1);
  window.addEventListener("resize", () => { if (S.pane && S.tab === "screen") renderPane(); });
  $("#keybar").onclick = (e) => { const b = e.target.closest("[data-k]"); if (b) sendKey(b.dataset.k); };
  $("#composer").onsubmit = sendPrompt;
  $("#prompt").addEventListener("input", () => {
    autoGrow(); updateCount();
    if (S.current) store.set("draft:" + S.current.host + ":" + S.current.target, $("#prompt").value);
  });
  $("#prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendPrompt(e); });
  $("#external-note").onclick = (e) => {
    if (!e.target.closest("[data-act=ext-dec]")) return;
    const s = S.current && findSession(S.current.host, S.current.target);
    if (s && s.decisions_path) openDecisions(s.host, s.decisions_path, false);
  };
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
    copyText(selectedText()).then((ok) => toast(ok ? `Copied ${S.dec ? S.dec.selected.size : ""} entries` : "Copy failed - long-press to select instead", { err: !ok }));
  };
  $("#dec-pplx").onclick = () => {
    const url = $("#pplx-url").value.trim();
    if (!/^https?:\/\//.test(url)) { toast("Paste this project's Perplexity thread URL first", { err: true }); $("#pplx-url").focus(); return; }
    const p = copyText(selectedText());
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

  $("#new-close").onclick = () => ($("#new-sheet").hidden = true);
  $("#new-sheet").onclick = (e) => { if (e.target.id === "new-sheet") $("#new-sheet").hidden = true; };
  $("#new-host").onchange = fillNewHost;
  $("#new-form").onsubmit = submitNew;

  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("#app").hidden) { refreshState(); if (S.current) { pollPane(true); scheduleChat(); } }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#dec-sheet").hidden) closeDecisions();
    else if (!$("#new-sheet").hidden) $("#new-sheet").hidden = true;
    else if (!$("#devices-sheet").hidden) $("#devices-sheet").hidden = true;
  });
}

window.addEventListener("error", (e) => toast("UI error: " + e.message, { err: true, ms: 8000 }));
window.addEventListener("unhandledrejection", (e) => toast("UI error: " + ((e.reason && e.reason.message) || e.reason), { err: true, ms: 8000 }));

// token may arrive as #token=...&to=... (only needed from outside your Tailscale account)
(function boot() {
  const m = location.hash.match(/^#token=([^&]+)(?:&to=(.*))?$/);
  if (m) {
    S.token = decodeURIComponent(m[1]); store.set("token", S.token);
    history.replaceState(null, "", location.pathname + (m[2] ? "#" + decodeURIComponent(m[2]) : ""));
  }
  bind();
  start();
})();
