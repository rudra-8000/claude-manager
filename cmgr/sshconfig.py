"""Your own ~/.ssh/config aliases (`Host robot` -> HostName guru) matched to Tailscale devices, so a device
you already reach with `ssh robot` is connected without typing anything."""
from __future__ import annotations

import glob
import os
import re
import socket
import subprocess
import sys
import threading
import time

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_cache: dict = {"at": 0.0, "map": {}}
_lock = threading.Lock()


def aliases(path: str = "~/.ssh/config", _seen: set | None = None) -> list[str]:
    """Host names defined in the ssh config (following Include), without wildcard patterns."""
    path = os.path.expanduser(path)
    seen = _seen if _seen is not None else set()
    if path in seen or not os.path.isfile(path):
        return []
    seen.add(path)
    out: list[str] = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    for ln in lines:
        m = re.match(r"\s*(Host|Include)\s+(.+?)\s*$", ln, re.I)
        if not m:
            continue
        if m.group(1).lower() == "include":
            for pat in m.group(2).split():
                pat = os.path.expanduser(pat)
                if not os.path.isabs(pat):
                    pat = os.path.join(os.path.expanduser("~/.ssh"), pat)
                for inc in sorted(glob.glob(pat)):
                    out += [a for a in aliases(inc, seen) if a not in out]
            continue
        for name in m.group(2).split():
            if not any(c in name for c in "*?!") and name not in out:
                out.append(name)
    return out


def resolve(alias: str) -> dict:
    """`ssh -G alias` -> {"hostname", "user", "port"} as ssh itself would use them."""
    try:
        out = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=5,
                             creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    d = {}
    for ln in out.splitlines():
        k, _, v = ln.partition(" ")
        if k in ("hostname", "user", "port"):
            d[k] = v.strip()
    return d


def _ip_of(host: str) -> str:
    try:
        socket.setdefaulttimeout(2)
        return socket.gethostbyname(host)
    except OSError:
        return ""
    finally:
        socket.setdefaulttimeout(None)


def match(devices: list[dict], alias_list: list[str] | None = None, resolver=resolve, ip_of=_ip_of) -> dict[str, str]:
    """{device label: ssh alias} for aliases whose HostName is that device (by name, MagicDNS name or IP)."""
    out: dict[str, str] = {}
    for alias in alias_list if alias_list is not None else aliases():
        host = (resolver(alias).get("hostname") or "").lower().rstrip(".")
        if not host:
            continue
        for d in devices:
            if d["label"] in out:
                continue
            names = {d["label"].lower(), (d.get("name") or "").lower(), (d.get("dns") or "").lower().rstrip("."),
                     (d.get("dns") or "").lower().split(".")[0], d.get("ip", "")}
            if host in names or (ip_of(host) or "-") == d.get("ip"):
                out[d["label"]] = alias
                break
    return out


def cached_match(devices: list[dict], max_age: float = 120.0) -> dict[str, str]:
    with _lock:
        key = tuple(sorted(d["label"] for d in devices))
        if time.time() - _cache["at"] < max_age and _cache.get("key") == key:
            return _cache["map"]
    m = match(devices)
    with _lock:
        _cache.update(at=time.time(), map=m, key=key)
    return m
