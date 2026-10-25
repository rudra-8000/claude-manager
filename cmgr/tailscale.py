"""Tailscale as the zero-config layer: who am I, which of my machines are online, and is a caller
one of my own devices. Uses the `tailscale` CLI (read-only LocalAPI calls, fine as a normal user)."""
from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import sys
import threading
import time

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
PHONE_OS = {"ios", "android"}


def _exe() -> str | None:
    return shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if sys.platform == "win32" else None)


def _run_json(*args: str, timeout: float = 5.0):
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
                      "os": (p.get("OS") or "").lower(), "online": bool(p.get("Online")), "user_id": p.get("UserID")})
    return {"running": True, "self": me, "peers": peers}


class Tailscale:
    def __init__(self, status_fn=None, whois_fn=None):
        self._status_fn = status_fn or (lambda: _run_json("status", "--json"))
        self._whois_fn = whois_fn or (lambda ip: _run_json("whois", "--json", ip))
        self._status, self._status_at = None, 0.0
        self._whois: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def status(self, max_age: float = 30.0) -> dict:
        with self._lock:
            if self._status is None or time.time() - self._status_at > max_age:
                self._status, self._status_at = parse_status(self._status_fn()), time.time()
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
        """Tailscale user id that owns the device at `ip`, or None (cached)."""
        now = time.time()
        hit = self._whois.get(ip)
        if hit and now - hit[0] < (300 if hit[1] is not None else 30):
            return hit[1]
        d = self._whois_fn(ip)
        uid = ((d or {}).get("UserProfile") or {}).get("ID")
        self._whois[ip] = (now, uid)
        return uid


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False
