"""HTTP layer. Every machine serves /api/* (its own agent). A machine with "hub": true also
serves the web UI and /hub/*, which fans out to itself and its peers."""
from __future__ import annotations

import gzip
import hmac
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent, AgentError
from .mux import MuxError
from .tailscale import Tailscale, is_loopback

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
PEER_TIMEOUT = 6.0


class RemoteError(Exception):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status if 400 <= status < 500 else 502  # pass the peer's 4xx through


# ---------------------------------------------------------------- clients (local / remote agent)

class LocalClient:
    def __init__(self, agent: Agent):
        self.agent = agent

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        return dispatch_agent(self.agent, method, path, params or {}, body or {})


class RemoteClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
             timeout: float = PEER_TIMEOUT):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except Exception:
                msg = ""
            raise RemoteError(msg or f"HTTP {e.code}", e.code) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise RemoteError(f"unreachable: {getattr(e, 'reason', e)}") from e


# ---------------------------------------------------------------- agent API

def _int_list(s: str) -> list[int]:
    return [int(x) for x in str(s).split(",") if x.strip().lstrip("-").isdigit()]


def dispatch_agent(agent: Agent, method: str, path: str, q: dict, body: dict):
    if method == "GET":
        if path == "/api/info":
            return agent.info()
        if path == "/api/snapshot":
            return {"info": agent.info(), "sessions": agent.sessions(), "usage": agent.usage(),
                    "prefs": agent.prefs(), "time": time.time()}
        if path == "/api/sessions":
            return agent.sessions()
        if path == "/api/pane":
            return agent.pane(q["target"], int(q.get("history", 300)))
        if path == "/api/usage":
            return agent.usage()
        if path == "/api/transcript":
            num = lambda k: int(q[k]) if q.get(k) not in (None, "") else None
            return agent.transcript(q["target"], before=num("before"), since=num("since"))
        if path == "/api/projects":
            return agent.projects()
        if path == "/api/decisions":
            return agent.decisions(q["dir"], int(q.get("bodies", 25)))
        if path == "/api/decisions/bodies":
            return agent.decision_bodies(q["dir"], _int_list(q.get("i", "")))
    elif method == "POST":
        if path == "/api/send":
            return agent.send(body["target"], body.get("text", ""), bool(body.get("submit", True)))
        if path == "/api/keys":
            return agent.keys(body["target"], body.get("keys") or [], body.get("literal", ""))
        if path == "/api/prefs":
            return agent.set_pref(body["section"], body["key"], body.get("value"))
        if path == "/api/new":
            return agent.new_session(body["cwd"], body.get("account_dir", ""), body.get("name", ""),
                                     bool(body.get("resume", False)))
    raise AgentError(f"not found: {method} {path}", 404)


# ---------------------------------------------------------------- hub

DISCOVER_EVERY = 60.0


class Hub:
    """The one page's backend. Shows this machine plus every other machine you switch on in the Devices
    panel: machines running `cm` are reached over HTTP, any other machine over SSH (nothing to install
    there). Device and account visibility live in ~/.claude-manager/hub.json."""

    def __init__(self, cfg: dict, agent: Agent, ts: Tailscale | None = None):
        self.cfg = cfg
        self.ts = ts or Tailscale()
        self.local = LocalClient(agent)
        self.pool = ThreadPoolExecutor(max_workers=24)
        self.last: dict[str, dict] = {}
        self.agents: dict[str, RemoteClient] = {}     # device name -> cm found running there
        self.ssh: dict[str, object] = {}              # device name -> SSHClient
        self.discovered_at = 0.0
        self._discover_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self.settings_path = os.path.join(os.path.dirname(cfg["_path"]), "hub.json")
        self.peers = {p["name"]: RemoteClient(p["url"], p.get("token") or cfg.get("token", "")) for p in cfg.get("peers") or []}
        self.agents.update(self.peers)

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        s.setdefault("devices", {})
        s.setdefault("hidden_accounts", [])
        return s

    def _save_settings(self, s: dict) -> None:
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(self.settings_path + ".tmp", self.settings_path)

    def set_device(self, name: str, enabled=None, via=None, ssh=None, python=None) -> dict:
        if via not in (None, "auto", "ssh"):
            raise AgentError("via must be auto or ssh")
        with self._settings_lock:
            s = self.settings()
            d = s["devices"].setdefault(name, {})
            for k, v in (("enabled", enabled), ("via", via), ("ssh", ssh), ("python", python)):
                if v is not None:
                    d[k] = v.strip() if isinstance(v, str) else v
            self._save_settings(s)
        old = self.ssh.pop(name, None)
        if old:
            old.close()
        self.last.pop(name, None)
        return {"devices": self.devices()}

    def set_account_hidden(self, key: str, hidden: bool) -> dict:
        with self._settings_lock:
            s = self.settings()
            hid = [k for k in s["hidden_accounts"] if k != key] + ([key] if hidden else [])
            s["hidden_accounts"] = hid
            self._save_settings(s)
        return {"hidden_accounts": hid}

    # ------------------------------------------------------------ devices
    def discover(self, force: bool = False) -> None:
        """Note which of my Tailscale machines run `cm` (probe /api/info)."""
        if not self.cfg.get("discover", True):
            return
        if not force and time.time() - self.discovered_at < DISCOVER_EVERY:
            return
        if not self._discover_lock.acquire(blocking=False):
            return
        try:
            port = int(self.cfg["port"])
            mine = set(self.ts.my_ips())

            def probe(m):
                if m["ip"] in mine or not m["online"]:
                    return m["label"], None
                c = RemoteClient(f"http://{m['ip']}:{port}", self.cfg.get("token", ""))
                try:
                    c.call("GET", "/api/info", timeout=2.5)
                    return m["label"], c
                except Exception:
                    return m["label"], None

            for label, c in self.pool.map(probe, self.ts.my_devices()):
                if c:
                    self.agents[label] = c
                elif label not in self.peers:
                    self.agents.pop(label, None)
            self.discovered_at = time.time()
        finally:
            self._discover_lock.release()

    def devices(self) -> list[dict]:
        s = self.settings()["devices"]
        me = self.cfg["name"]
        mine = set(self.ts.my_ips())
        rows = [{"name": me, "kind": "this machine", "online": True, "os": sys.platform, "ip": ""}]
        seen = {me}
        for m in self.ts.my_devices():
            if m["ip"] in mine or m["label"] in seen:
                continue
            seen.add(m["label"])
            rows.append({"name": m["label"], "kind": "tailscale", "online": m["online"], "os": m["os"], "ip": m["ip"]})
        for name in list(s) + list(self.agents):
            if name not in seen:
                seen.add(name)
                rows.append({"name": name, "kind": "configured", "online": True, "os": "", "ip": ""})
        for r in rows:
            d = s.get(r["name"], {})
            r["via"] = d.get("via", "auto")
            r["ssh"] = d.get("ssh", "")
            r["python"] = d.get("python", "")
            r["has_agent"] = r["name"] in self.agents
            usable = r["name"] == me or r["has_agent"] or r["via"] == "ssh"
            r["enabled"] = bool(d.get("enabled", usable)) and usable
            last = self.last.get(r["name"])
            if r["name"] == me:
                r["status"] = "this machine"
            elif not r["online"]:
                r["status"] = "offline"
            elif not usable:
                r["status"] = "not connected"
            elif not r["enabled"]:
                r["status"] = "hidden"
            elif last and not last.get("ok"):
                r["status"] = "error: " + (last.get("error") or "")
            else:
                r["status"] = ("connected over SSH" if r["via"] == "ssh" else "connected (cm running there)")
        return rows

    def _client(self, name: str):
        if name == self.cfg["name"]:
            return self.local
        d = self.settings()["devices"].get(name, {})
        if d.get("via") == "ssh":
            c = self.ssh.get(name)
            if c is None:
                from .remote import SSHClient
                target = d.get("ssh") or name
                c = self.ssh[name] = SSHClient(target, d.get("python") or "python3")
            return c
        return self.agents.get(name)

    def active(self) -> list[str]:
        return [d["name"] for d in self.devices() if d["enabled"] and d["online"]]

    def client(self, host: str):
        c = self._client(host)
        if not c:
            raise AgentError(f"unknown or disconnected device: {host}", 404)
        return c

    def _snap(self, name: str) -> dict:
        t0 = time.time()
        try:
            snap = self.client(name).call("GET", "/api/snapshot")
            entry = {"name": name, "ok": True, "error": "", "latency_ms": int((time.time() - t0) * 1000),
                     "seen_at": time.time(), **snap}
        except Exception as e:  # keep showing the last good data, marked offline
            prev = self.last.get(name, {})
            entry = {"name": name, "ok": False, "error": str(e), "seen_at": prev.get("seen_at"),
                     "info": prev.get("info"), "sessions": prev.get("sessions", []), "usage": prev.get("usage", []),
                     "prefs": prev.get("prefs")}
        self.last[name] = entry
        return entry

    def state(self) -> dict:
        self.discover()
        hosts = list(self.pool.map(self._snap, self.active()))
        accounts: dict[str, dict] = {}
        for h in hosts:
            for u in h.get("usage") or []:
                key = u["account"]["key"]
                u = {**u, "hosts": [h["name"]], "account": {**u["account"], "names": u["account"].get("names") or [u["account"]["label"]]}}
                cur = accounts.get(key)
                if cur is None:
                    accounts[key] = u
                else:
                    hosts_seen = cur["hosts"] + [h["name"]]
                    names = list(dict.fromkeys(cur["account"]["names"] + u["account"].get("names", [])))
                    if (u.get("fetched_at") or 0) > (cur.get("fetched_at") or 0):
                        accounts[key] = u
                    accounts[key]["hosts"] = hosts_seen
                    # same account under different aliases on different machines -> one entry, all names
                    accounts[key]["account"] = {**accounts[key]["account"], "names": names, "label": " / ".join(names)}
        settings = self.settings()
        return {"time": time.time(), "hub": self.cfg["name"], "hosts": hosts,
                "accounts": sorted(accounts.values(), key=lambda a: a["account"]["label"].lower()),
                "devices": self.devices(), "hidden_accounts": settings["hidden_accounts"],
                "prefs": self.prefs(hosts)}

    def projects(self) -> list[dict]:
        def one(name):
            try:
                return [{**p, "host": name} for p in self.client(name).call("GET", "/api/projects")]
            except Exception:
                return []
        return [p for ps in self.pool.map(one, self.active()) for p in ps]

    def prefs(self, hosts: list | None = None) -> dict:
        """Perplexity links are written to every machine; merged here, this machine first."""
        hosts = hosts if hosts is not None else [self.last.get(n, {}) for n in self.active()]
        merged: dict = {"perplexity": {}}
        for h in sorted(hosts, key=lambda h: h.get("name") != self.cfg["name"]):
            for section, values in (h.get("prefs") or {}).items():
                for k, v in (values or {}).items():
                    merged.setdefault(section, {}).setdefault(k, v)
        return merged

    def set_pref(self, section: str, key: str, value) -> dict:
        body = {"section": section, "key": key, "value": value}
        names = self.active()
        results = list(self.pool.map(lambda n: self._try(n, "POST", "/api/prefs", body), names))
        mine = results[names.index(self.cfg["name"])] if self.cfg["name"] in names else self.local.call("POST", "/api/prefs", None, body)
        if isinstance(mine, Exception):
            raise mine
        for name, r in zip(names, results):
            if not isinstance(r, Exception):
                self.last.setdefault(name, {"name": name})["prefs"] = r
        return self.prefs()

    def _try(self, name, method, path, body):
        try:
            return self.client(name).call(method, path, None, body)
        except Exception as e:
            return e


def dispatch_hub(hub: Hub, method: str, path: str, q: dict, body: dict):
    if method == "GET":
        if path == "/hub/state":
            return hub.state()
        if path == "/hub/projects":
            return hub.projects()
    if method == "POST" and path == "/hub/prefs":
        return hub.set_pref(body["section"], body["key"], body.get("value"))
    if method == "POST" and path == "/hub/device":
        return hub.set_device(body["name"], body.get("enabled"), body.get("via"), body.get("ssh"), body.get("python"))
    if method == "POST" and path == "/hub/account":
        return hub.set_account_hidden(body["key"], bool(body.get("hidden")))
    # /hub/<anything> with a host -> forward to that host's /api/<anything>
    host = q.pop("host", None) or body.pop("host", None)
    if not host:
        raise AgentError("missing host", 400)
    api_path = "/api/" + path[len("/hub/"):]
    c = hub.client(host)
    return c.call(method, api_path, q, body if method == "POST" else None)


# ---------------------------------------------------------------- HTTP handler

class Authorizer:
    """Who may call /api and /hub:
    - anyone presenting the configured token (the token is optional);
    - devices owned by my own Tailscale user (checked with `tailscale whois`), so my phone and my
      other machines need no token;
    - this machine itself (loopback or its own Tailscale IP) only with trust_local, which defaults to
      true on Windows/macOS and false on Linux, where the box may be shared with other users."""

    def __init__(self, cfg: dict, ts: Tailscale):
        self.token = cfg.get("token") or ""
        self.ts = ts
        tl = cfg.get("trust_local")
        self.trust_local = (sys.platform != "linux") if tl is None else bool(tl)
        self.allow_tailscale = cfg.get("allow_tailscale", True)

    def check(self, ip: str, presented: str) -> tuple[bool, str]:
        if self.token and presented and hmac.compare_digest(presented.encode(), self.token.encode()):
            return True, ""
        if is_loopback(ip) or ip in self.ts.my_ips():
            if self.trust_local:
                return True, ""
            return False, "connections from this machine itself need the token here (or set trust_local)"
        if self.allow_tailscale:
            me = self.ts.my_user()
            if me is not None and self.ts.owner_of(ip) == me:
                return True, ""
        return False, "not one of your Tailscale devices, and no valid token"


def make_handler(cfg: dict, agent: Agent, hub: Hub | None, auth: Authorizer | None = None):
    auth = auth or Authorizer(cfg, hub.ts if hub else Tailscale())

    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-manager"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; errors still go to stderr below
            pass

        def _authed(self) -> tuple[bool, str]:
            got = self.headers.get("Authorization", "")
            got = got[7:] if got.startswith("Bearer ") else self.headers.get("X-CM-Token", "")
            return auth.check(self.client_address[0], got)

        def _send(self, status: int, payload: bytes, ctype: str, cache: str = "no-store"):
            if len(payload) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
                payload = gzip.compress(payload, compresslevel=5)
                gz = True
            else:
                gz = False
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _static(self, rel: str):
            if not hub:
                return self._json(404, {"error": "this machine is not a hub"})
            rel = rel.lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
                full = os.path.join(WEB_DIR, "index.html")  # single-page app
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype, cache="no-cache")

        def _handle(self, method: str):
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            if not (path.startswith("/api/") or path.startswith("/hub/")):
                if method == "GET":
                    return self._static(path)
                return self._json(404, {"error": "not found"})
            ok, why = self._authed()
            if not ok:
                return self._json(401, {"error": why})
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    return self._json(413, {"error": "body too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "invalid JSON"})
            try:
                if path.startswith("/hub/"):
                    if not hub:
                        return self._json(404, {"error": "this machine is not a hub"})
                    res = dispatch_hub(hub, method, path, q, body)
                else:
                    res = dispatch_agent(agent, method, path, q, body)
                self._json(200, res)
            except AgentError as e:
                self._json(e.status, {"error": str(e)})
            except KeyError as e:
                self._json(400, {"error": f"missing parameter: {e.args[0]}"})
            except RemoteError as e:
                self._json(e.status, {"error": str(e)})
            except MuxError as e:
                self._json(502, {"error": str(e)})
            except Exception as e:
                print(f"[cm] error on {method} {path}: {e!r}", file=sys.stderr)
                self._json(500, {"error": f"internal error: {e.__class__.__name__}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def tailscale_ip() -> str | None:
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5,
                                 creationflags=0x08000000 if sys.platform == "win32" else 0).stdout
            ip = out.strip().splitlines()[0] if out.strip() else ""
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):  # not "no current Tailscale IPs; state: ..."
                return ip
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def resolve_binds(binds: list[str]) -> list[str]:
    out = []
    for b in binds:
        if b == "tailscale":
            ip = tailscale_ip()
            if ip:
                out.append(ip)
            else:
                print("[cm] warning: Tailscale IP not found; not listening on it", file=sys.stderr)
        elif b not in out:
            out.append(b)
    return out


def serve(cfg: dict, agent: Agent | None = None) -> None:
    agent = agent or Agent(cfg)
    ts = Tailscale()
    hub = Hub(cfg, agent, ts) if cfg.get("hub") else None
    handler = make_handler(cfg, agent, hub, Authorizer(cfg, ts))
    servers = []
    for addr in resolve_binds(cfg["bind"]):
        try:
            srv = ThreadingHTTPServer((addr, int(cfg["port"])), handler)
        except OSError as e:
            print(f"[cm] warning: cannot listen on {addr}:{cfg['port']} ({e.strerror or e}); "
                  "another program probably has that port - skipping it", file=sys.stderr)
            continue
        srv.daemon_threads = True
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[cm] {cfg['name']}: listening on http://{addr}:{cfg['port']}" + (" (UI)" if hub else ""))
    if not servers:
        raise SystemExit(f"[cm] could not listen anywhere on port {cfg['port']}; pick another with "
                         "`cm serve --port 8766` (the same port on every machine)")
    if hub:
        threading.Thread(target=hub.discover, kwargs={"force": True}, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for srv in servers:
            srv.shutdown()
