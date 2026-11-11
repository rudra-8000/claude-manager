"""Tailscale as the zero-config layer: who am I, which of my machines are online, and is a caller
one of my own devices.

On Linux/macOS it asks tailscaled directly over its local socket (read-only LocalAPI, open to normal
users): ~0.5 ms, no process to start. Starting the `tailscale` CLI instead took 19.7 s on a server
under memory pressure (the binary had been paged out), which made own devices look like strangers.
The CLI stays as the fallback (and on Windows) with a generous timeout."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PHONE_OS = {"ios", "android"}


def _exe() -> str | None:
    return shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if sys.platform == "win32" else None)


SOCKET_PATHS = ["/var/run/tailscale/tailscaled.sock", "/run/tailscale/tailscaled.sock",
                "/var/run/tailscaled.socket"]
CLI_TIMEOUT = 30.0


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _socket_path() -> str | None:
    if not hasattr(socket, "AF_UNIX"):
        return None
    for p in [os.environ.get("TS_SOCKET", "")] + SOCKET_PATHS:
        if p and os.path.exists(p):
            return p
    return None


def localapi(path: str, timeout: float = 10.0):
    """GET a tailscaled LocalAPI endpoint over the unix socket; parsed JSON, or None if unavailable."""
    sock = _socket_path()
    if not sock:
        return None
    conn = _UnixHTTPConnection(sock, timeout)
    try:
        conn.request("GET", path, headers={"Host": "local-tailscaled.sock"})
        r = conn.getresponse()
        body = r.read()
        return json.loads(body) if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def get_status():
    return localapi("/localapi/v0/status") or _run_json("status", "--json")


def get_whois(ip: str):
    return localapi("/localapi/v0/whois?addr=" + urllib.parse.quote(ip)) or _run_json("whois", "--json", ip)


def _run_json(*args: str, timeout: float = CLI_TIMEOUT):
    exe = _exe()
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def parse_status(d: dict | None) -> dict:
    """-> {"running", "self": {name, ips, user_id}, "peers": [{name, dns, ip, os, online, user_id}]}"""
    if not d or d.get("BackendState") != "Running":
        return {"running": False, "self": None, "peers": []}
    s = d.get("Self") or {}
    me = {"name": s.get("HostName", ""), "dns": (s.get("DNSName") or "").rstrip("."),
          "ips": s.get("TailscaleIPs") or [], "user_id": s.get("UserID")}
    peers = []
    for p in (d.get("Peer") or {}).values():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
        if not ips:
            continue
        peers.append({"name": p.get("HostName", ""), "dns": (p.get("DNSName") or "").rstrip("."), "ip": ips[0],
                      "ips": list(p.get("TailscaleIPs") or []),
                      "os": (p.get("OS") or "").lower(), "online": bool(p.get("Online")), "user_id": p.get("UserID")})
    return {"running": True, "self": me, "peers": peers}


class Tailscale:
    """The peer list is refreshed in the background, so a request never waits on Tailscale, and a failed
    refresh never replaces a good answer: no device can reach this machine's Tailscale address while
    Tailscale is down anyway, so the last good list stays valid. Caches use time.monotonic()."""

    def __init__(self, status_fn=None, whois_fn=None, clock=time.monotonic, background: bool = True):
        self._status_fn = status_fn or get_status
        self._whois_fn = whois_fn or get_whois
        self._clock = clock
        self._background = background
        self._status, self._status_at, self._good_at = None, 0.0, None
        self._whois: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._refreshing = False

    def _refresh(self) -> None:
        try:
            fresh = parse_status(self._status_fn())
        except Exception:
            fresh = parse_status(None)
        with self._lock:
            if fresh["running"] or self._good_at is None:   # never replace a good answer with a failure
                self._status = fresh
                if fresh["running"]:
                    self._good_at = self._clock()
            self._status_at = self._clock()
            self._refreshing = False

    def status(self, max_age: float = 30.0) -> dict:
        with self._lock:
            have = self._status is not None
            start = (not have or self._clock() - self._status_at > max_age) and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            if have and self._background:   # answer from what we have; refresh behind it
                threading.Thread(target=self._refresh, daemon=True).start()
            else:
                self._refresh()
        elif not have:                      # another thread is fetching the very first answer
            while self._status is None:
                time.sleep(0.05)
        with self._lock:
            return self._status

    def my_ips(self) -> list[str]:
        me = self.status()["self"]
        return list(me["ips"]) if me else []

    def my_user(self):
        me = self.status()["self"]
        return me["user_id"] if me else None

    def my_devices(self) -> list[dict]:
        """All computers of my Tailscale user, online or not (phones and shared-in machines excluded),
        named by their MagicDNS label, which is unique in the tailnet unlike the hostname."""
        st = self.status()
        if not st["running"]:
            return []
        uid = st["self"]["user_id"]
        out = []
        for p in st["peers"]:
            if p["user_id"] == uid and p["os"] not in PHONE_OS:
                out.append({**p, "label": (p["dns"].split(".")[0] if p["dns"] else p["name"]) or p["ip"]})
        return sorted(out, key=lambda p: p["label"])

    def my_machines(self) -> list[dict]:
        """Online peers owned by the same Tailscale user (shared-in machines of other people excluded), no phones."""
        st = self.status()
        if not st["running"]:
            return []
        uid = st["self"]["user_id"]
        return [p for p in st["peers"] if p["online"] and p["user_id"] == uid and p["os"] not in PHONE_OS]

    def owner_of(self, ip: str):
        """Tailscale user id that owns the device at `ip`, or None. Answered from the peer list (refreshed
        every 30 s); `tailscale whois` is only asked about addresses that aren't in it. Failures aren't cached."""
        for p in self.status()["peers"]:
            if ip == p["ip"] or ip in p.get("ips", ()):
                return p["user_id"]
        now = self._clock()
        hit = self._whois.get(ip)
        if hit and now - hit[0] < 300:
            return hit[1]
        uid = ((self._whois_fn(ip) or {}).get("UserProfile") or {}).get("ID")
        if uid is not None:
            self._whois[ip] = (now, uid)
        return uid


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False
