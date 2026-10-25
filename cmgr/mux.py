"""Thin wrapper over the tmux CLI. Works with real tmux (Linux) and psmux (Windows),
which ships a tmux.exe that accepts the same commands and format strings.

Everything here is a subprocess call; nothing talks to a model.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

# Printable on purpose: Linux tmux escapes control characters in format output (psmux does not).
# "<" and ">" cannot appear in Windows paths and practically never in Linux paths or session names.
_SEP = "<|>"
_FIELDS = [
    "session_name", "window_id", "window_index", "window_name", "pane_id", "pane_index",
    "pane_current_command", "pane_current_path", "pane_pid", "pane_width", "pane_height",
    "session_attached", "pane_active", "pane_dead",
]
_FORMAT = _SEP.join("#{%s}" % f for f in _FIELDS)

# Names tmux accepts for send-keys that the web UI is allowed to send.
ALLOWED_KEYS = {
    "Enter", "Escape", "Tab", "BTab", "BSpace", "Up", "Down", "Left", "Right",
    "Home", "End", "PageUp", "PageDown", "C-c", "C-d", "C-l", "C-o", "C-r", "C-t", "C-b",
    "Space", "y", "n", "1", "2", "3", "4", "5",
}

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _clean_env() -> dict[str, str]:
    """Our environment minus Claude Code's own markers. If `cm serve` was started from inside a
    Claude session, new tmux sessions would otherwise inherit them (and the parent's account)."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_CONFIG_DIR"))}


class MuxError(RuntimeError):
    pass


@dataclass
class Pane:
    session: str
    window_id: str
    window_index: int
    window_name: str
    pane_id: str
    pane_index: int
    command: str
    cwd: str
    pid: int
    width: int
    height: int
    attached: bool
    active: bool
    dead: bool

    @property
    def target(self) -> str:
        # psmux reuses pane ids (%1) across sessions, so always qualify by session.
        return f"{self.session}:{self.window_index}.{self.pane_index}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target"] = self.target
        return d


def parse_panes(out: str) -> list[Pane]:
    panes = []
    for line in out.splitlines():
        parts = line.split(_SEP)
        if len(parts) != len(_FIELDS):
            continue
        f = dict(zip(_FIELDS, parts))
        panes.append(Pane(
            session=f["session_name"], window_id=f["window_id"],
            window_index=_int(f["window_index"]), window_name=f["window_name"],
            pane_id=f["pane_id"], pane_index=_int(f["pane_index"]),
            command=f["pane_current_command"], cwd=f["pane_current_path"],
            pid=_int(f["pane_pid"]), width=_int(f["pane_width"], 80), height=_int(f["pane_height"], 24),
            attached=f["session_attached"] not in ("", "0"), active=f["pane_active"] == "1",
            dead=f["pane_dead"] == "1",
        ))
    return panes


def _int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


_SESSION_NAME_BAD = re.compile(r"[^A-Za-z0-9_-]+")


def safe_session_name(name: str) -> str:
    name = _SESSION_NAME_BAD.sub("-", name.strip()).strip("-")
    return name[:40] or "claude"


_COMMON = ["~/miniforge3/bin/tmux", "~/mambaforge/bin/tmux", "~/miniconda3/bin/tmux", "~/anaconda3/bin/tmux",
           "~/.local/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux", "/usr/bin/tmux", "/snap/bin/tmux"]


def running_server_binary() -> str | None:
    """The executable of a tmux server this user is running (Linux). Over a non-interactive ssh login, PATH
    may lack conda's tmux, and a different tmux version may not speak to the running server at all."""
    if not os.path.isdir("/proc"):
        return None
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/comm") as f:
                if not f.read().startswith("tmux"):
                    continue
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            continue
        exe = exe.replace(" (deleted)", "")
        if os.path.isfile(exe):
            return exe
    return None


def find_tmux() -> str:
    if sys.platform != "win32":
        found = running_server_binary()
        if found:
            return found
    found = shutil.which("tmux") or shutil.which("psmux")
    if found:
        return found
    for p in _COMMON:
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    if sys.platform == "win32":
        links = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\tmux.exe")
        if os.path.isfile(links):
            return links
    return "tmux"


class Mux:
    def __init__(self, binary: str | None = None, timeout: float = 5.0):
        self.binary = binary or find_tmux()
        self.timeout = timeout

    def _run(self, *args: str, input: str | None = None, check: bool = True) -> str:
        try:
            p = subprocess.run(
                [self.binary, *args], input=input, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout, creationflags=_NO_WINDOW,
                env=_clean_env(),
            )
        except FileNotFoundError as e:
            raise MuxError(f"tmux binary not found: {self.binary}") from e
        except subprocess.TimeoutExpired as e:
            raise MuxError(f"tmux {args[0]} timed out") from e
        if check and p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            # "no server running" just means zero sessions.
            if "no server running" in err or "no sessions" in err or "error connecting" in err:
                return ""
            raise MuxError(f"tmux {args[0]} failed: {err}")
        return p.stdout

    def available(self) -> bool:
        try:
            self._run("-V")
            return True
        except MuxError:
            return False

    def list_panes(self) -> list[Pane]:
        return parse_panes(self._run("list-panes", "-a", "-F", _FORMAT))

    def session_names(self) -> set[str]:
        out = self._run("list-sessions", "-F", "#{session_name}")
        return {s for s in out.splitlines() if s}

    def capture(self, target: str, history: int = 0) -> str:
        args = ["capture-pane", "-p", "-e", "-t", target]
        if history:
            args += ["-S", f"-{int(history)}"]
        return self._run(*args)

    def send_text(self, target: str, text: str, submit: bool = True, submit_delay: float = 0.4) -> None:
        """Paste text as one bracketed paste (so newlines don't submit early), then Enter."""
        if text:
            buf = f"cm-{os.getpid()}-{time.monotonic_ns()}"
            self._run("load-buffer", "-b", buf, "-", input=text)
            self._run("paste-buffer", "-b", buf, "-p", "-d", "-t", target)
        if submit:
            if text:
                time.sleep(submit_delay)  # let the app finish handling the paste
            self._run("send-keys", "-t", target, "Enter")

    def send_keys(self, target: str, keys: list[str]) -> None:
        bad = [k for k in keys if k not in ALLOWED_KEYS]
        if bad:
            raise MuxError(f"key(s) not allowed: {bad}")
        if keys:
            self._run("send-keys", "-t", target, *keys)

    def send_literal(self, target: str, text: str) -> None:
        if text:
            self._run("send-keys", "-t", target, "-l", text)

    def new_session(self, name: str, cwd: str, env: dict[str, str], command: str) -> str:
        """Start a detached session with a normal shell, then type the command into it,
        so the pane survives when Claude exits (same as starting tmux by hand)."""
        base = safe_session_name(name)
        existing = self.session_names()
        sname, i = base, 2
        while sname in existing:
            sname, i = f"{base}-{i}", i + 1
        args = ["new-session", "-d", "-s", sname, "-c", cwd, "-x", "200", "-y", "50"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        self._run(*args)
        target = sname  # the session's active pane, whatever base-index is set to
        if command:
            time.sleep(0.8)  # give the shell a moment to draw its prompt
            self.send_literal(target, command)
            self._run("send-keys", "-t", target, "Enter")
        return sname
