"""Who is listening on a TCP port (so `cm up` can replace an older cm instead of failing to bind)."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cmdline(pid: int) -> str:
    if sys.platform == "win32":
        return _run(["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"]).strip()
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return _run(["ps", "-o", "args=", "-p", str(pid)]).strip()


def parse_ss(out: str) -> list[int]:
    return sorted({int(m) for m in re.findall(r"pid=(\d+)", out)})


def parse_netstat(out: str, port: int) -> list[int]:
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING" \
                and parts[1].rsplit(":", 1)[-1] == str(port):
            pids.add(int(parts[4]))
    return sorted(pids)


def holders(port: int) -> list[dict]:
    """-> [{"pid", "cmd", "is_cm"}] for processes listening on `port` that we can see."""
    if sys.platform == "win32":
        pids = parse_netstat(_run(["netstat", "-ano", "-p", "tcp"]), port)
    else:
        pids = parse_ss(_run(["ss", "-ltnpH", f"sport = :{port}"]))
        if not pids:
            pids = sorted({int(p) for p in _run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"]).split() if p.isdigit()})
    out = []
    for pid in pids:
        cmd = _cmdline(pid)
        out.append({"pid": pid, "cmd": cmd, "is_cm": is_cm_server(cmd)})
    return out


def is_cm_server(cmd: str) -> bool:
    return bool(re.search(r"-m\s+cmgr\s+serve|[\\/]cm(?:\.exe)?\"?\s+serve\b|^cm\s+serve\b", cmd))


def stop(pid: int) -> bool:
    if sys.platform == "win32":
        _run(["taskkill", "/PID", str(pid), "/F"])
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
    for _ in range(20):
        time.sleep(0.25)
        if not any(h["pid"] == pid for h in holders_quick(pid)):
            return True
    return False


def holders_quick(pid: int) -> list[dict]:
    if sys.platform == "win32":
        alive = str(pid) in _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    else:
        alive = os.path.exists(f"/proc/{pid}") or bool(_run(["ps", "-p", str(pid), "-o", "pid="]).strip())
    return [{"pid": pid}] if alive else []
