"""Reads what Claude Code already keeps on disk: accounts (one config dir each), the
per-process session registry (<config>/sessions/<pid>.json, which records the tmux pane),
and the subscription usage endpoint that `/usage` itself calls. No model calls.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TTL = 180          # seconds between usage fetches per account
USAGE_BACKOFF = 600      # after a 429 / error


def expand(p: str) -> str:
    return os.path.normpath(os.path.expanduser(os.path.expandvars(p)))


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- accounts

@dataclass
class Account:
    dir: str
    label: str = ""
    email: str = ""
    uuid: str = ""
    org: str = ""
    subscription: str = ""
    is_default: bool = False
    names: list = field(default_factory=list)   # your shell aliases / functions for this account

    @property
    def key(self) -> str:
        return self.uuid or self.email or self.dir

    def env(self) -> dict[str, str]:
        return {} if self.is_default else {"CLAUDE_CONFIG_DIR": self.dir}

    def to_dict(self) -> dict:
        names = self.names or [os.path.basename(self.dir).lstrip(".") or self.dir]
        return {"dir": self.dir, "label": self.label or " / ".join(names), "names": names,
                "email": self.email, "key": self.key, "org": self.org,
                "subscription": self.subscription, "is_default": self.is_default}


def default_config_dir() -> str:
    return expand("~/.claude")


def load_account(d: str, label: str = "") -> Account:
    d = expand(d)
    is_default = os.path.normcase(d) == os.path.normcase(default_config_dir())
    acct = Account(dir=d, label=label, is_default=is_default)
    # Without CLAUDE_CONFIG_DIR the account profile lives in ~/.claude.json, otherwise inside the dir.
    candidates = ([expand("~/.claude.json")] if is_default else []) + [os.path.join(d, ".claude.json")]
    for c in candidates:
        oa = (_read_json(c) or {}).get("oauthAccount") or {}
        if oa.get("emailAddress") or oa.get("accountUuid"):
            acct.email = oa.get("emailAddress", "")
            acct.uuid = oa.get("accountUuid", "")
            acct.org = oa.get("organizationName", "")
            break
    creds = (_read_json(os.path.join(d, ".credentials.json")) or {}).get("claudeAiOauth") or {}
    acct.subscription = creds.get("subscriptionType", "") or ""
    return acct


def discover_accounts(explicit: list | None = None, alias_files: list | None = None) -> list[Account]:
    """Explicit list from config ([{"dir": ..., "label": ...}] or ["dir", ...]) or, by default,
    ~/.claude, every ~/.claude-* / ~/.claude_* directory, and every CLAUDE_CONFIG_DIR your shell
    aliases / functions point at - whichever of those hold a login. Alias names become labels."""
    from .aliases import discover as discover_aliases
    names = discover_aliases(default_config_dir(), alias_files)
    out: list[Account] = []
    if explicit:
        for item in explicit:
            if isinstance(item, str):
                item = {"dir": item}
            a = load_account(item["dir"], item.get("label", ""))
            a.names = names.get(os.path.normcase(a.dir), [])
            out.append(a)
        return out
    dirs = [default_config_dir()] + sorted(glob.glob(expand("~/.claude-*")) + glob.glob(expand("~/.claude_*")))
    dirs += [d for d in names if d not in {os.path.normcase(x) for x in dirs}]
    seen = set()
    for d in dirs:
        if os.path.normcase(d) in seen:
            continue
        seen.add(os.path.normcase(d))
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, ".credentials.json")):
            a = load_account(d)
            a.names = names.get(os.path.normcase(a.dir), [])
            out.append(a)
    return out


# ---------------------------------------------------------------- session registry

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows; ask the kernel instead.
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_TMUX_REF = re.compile(r"^(?P<session>.+):(?P<window>@\d+)\.(?P<pane>%\d+)$")


@dataclass
class ClaudeProc:
    pid: int
    session_id: str
    name: str
    cwd: str
    status: str
    version: str
    started_at: float
    account_dir: str
    tmux_session: str = ""
    tmux_window: str = ""
    tmux_pane: str = ""
    updated_at: float = 0.0
    bridge_session_id: str = ""   # set when Remote Control is on for this session

    def to_dict(self) -> dict:
        return {"pid": self.pid, "session_id": self.session_id, "name": self.name, "cwd": self.cwd,
                "status": self.status, "version": self.version, "started_at": self.started_at,
                "updated_at": self.updated_at, "account_dir": self.account_dir,
                "remote_url": f"https://claude.ai/code/{self.bridge_session_id}" if self.bridge_session_id else ""}


def read_registry(accounts: list[Account], check_alive=pid_alive) -> list[ClaudeProc]:
    procs = []
    for acct in accounts:
        for f in glob.glob(os.path.join(acct.dir, "sessions", "*.json")):
            j = _read_json(f)
            if not isinstance(j, dict) or not j.get("tmux"):
                continue  # only sessions running inside tmux can be driven
            m = _TMUX_REF.match(str(j["tmux"]))
            if not m:
                continue
            pid = int(j.get("pid") or 0)
            if not check_alive(pid):
                continue
            procs.append(ClaudeProc(
                pid=pid, session_id=j.get("sessionId", ""), name=j.get("name", ""), cwd=j.get("cwd", ""),
                status=j.get("status", ""), version=j.get("version", ""),
                started_at=(j.get("startedAt") or 0) / 1000, updated_at=(j.get("statusUpdatedAt") or 0) / 1000,
                account_dir=acct.dir, tmux_session=m["session"], tmux_window=m["window"], tmux_pane=m["pane"],
                bridge_session_id=str(j.get("bridgeSessionId") or ""),
            ))
    return procs


def _profile_paths(acct: Account) -> list[str]:
    return ([expand("~/.claude.json")] if acct.is_default else []) + [os.path.join(acct.dir, ".claude.json")]


def known_projects(accounts: list[Account]) -> list[dict]:
    """Every folder Claude Code has been used in, on any account (from its own .claude.json), newest first.
    Last use = mtime of <config>/projects/<path with non-alphanumerics as '-'>, where transcripts live."""
    found: dict[str, dict] = {}
    for acct in accounts:
        for prof in _profile_paths(acct):
            for path in ((_read_json(prof) or {}).get("projects") or {}):
                if not os.path.isdir(path):
                    continue
                enc = re.sub(r"[^A-Za-z0-9]", "-", path)
                try:
                    used = os.path.getmtime(os.path.join(acct.dir, "projects", enc))
                except OSError:
                    used = 0.0
                key = os.path.normcase(os.path.normpath(path))
                cur = found.get(key)
                if cur is None or used > cur["last_used"]:
                    found[key] = {"dir": os.path.normpath(path), "last_used": used}
    return sorted(found.values(), key=lambda p: -p["last_used"])


def match_proc(pane, procs: list[ClaudeProc]) -> ClaudeProc | None:
    hits = [p for p in procs if p.tmux_session == pane.session and p.tmux_pane == pane.pane_id]
    return max(hits, key=lambda p: p.started_at) if hits else None


# ---------------------------------------------------------------- screen status

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]")
_WAITING = re.compile(
    r"Do you want to|Would you like to|Enter to select|Esc to cancel|Enter to confirm|"
    r"^\s*[❯>]\s*1\.\s", re.M)
_WORKING = re.compile(r"esc to interrupt|ctrl\+c to interrupt", re.I)


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def screen_status(screen: str, registry_status: str = "") -> str:
    """'waiting' (a question / permission prompt is on screen), 'working', or 'idle'."""
    tail = "\n".join([ln for ln in strip_ansi(screen).splitlines() if ln.strip()][-25:])
    if _WAITING.search(tail):
        return "waiting"
    if _WORKING.search(tail) or registry_status == "busy":
        return "working"
    return "idle"


# Newer Claude Code puts the session name inside the rule above the input: "──── trnsfr ─"
_RULE = re.compile(r"^\s*[─━═]{3,}")
_HINT = re.compile(r"^[─━═╭╮╰╯│\s]*$|^\s*[─━═]{3,}|^[❯>]\s|^[❯>]$|shift\+tab|\? for shortcuts|auto mode|accept edits|"
                   r"bypass permissions|plan mode|new task\?|/clear to save|Update installed|Restart to update|"
                   r"· /[a-z-]+$|ctrl\+g to edit")


def last_text_line(screen: str) -> str:
    """Last line of real content above Claude Code's input box, for list previews."""
    lines = [ln.strip() for ln in strip_ansi(screen).splitlines()]
    # Claude Code draws the input as rule / "❯ ..." / rule; start looking above the top rule.
    end = len(lines)
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].startswith(("❯", ">")) and _RULE.match(lines[i - 1] or ""):
            end = i - 1
            break
    for ln in reversed(lines[:end]):
        if ln and not _HINT.search(ln):
            return ln[:200]
    return ""


# ---------------------------------------------------------------- usage

@dataclass
class UsageCache:
    data: dict | None = None
    fetched_at: float = 0.0
    error: str = ""
    next_try: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def normalize_usage(raw: dict) -> list[dict]:
    """-> [{kind, label, percent, resets_at}] from either the `limits` list or the older fields."""
    labels = {"session": "Session (5h)", "weekly_all": "Weekly", "weekly_opus": "Weekly · Opus",
              "weekly_sonnet": "Weekly · Sonnet"}
    out = []
    if isinstance(raw.get("limits"), list) and raw["limits"]:
        for lim in raw["limits"]:
            kind = lim.get("kind", "")
            out.append({"kind": kind, "label": labels.get(kind, kind.replace("_", " ").capitalize()),
                        "percent": lim.get("percent"), "resets_at": lim.get("resets_at"),
                        "severity": lim.get("severity", "")})
        return out
    for key, label in (("five_hour", "Session (5h)"), ("seven_day", "Weekly"),
                       ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
        v = raw.get(key)
        if isinstance(v, dict):
            out.append({"kind": key, "label": label, "percent": v.get("utilization"),
                        "resets_at": v.get("resets_at"), "severity": ""})
    return out


class UsageTracker:
    """Fetches usage per account in the background. Only READS the access token that Claude Code
    maintains; it never refreshes it (refresh tokens rotate, refreshing here could log the CLI out)."""

    def __init__(self, ttl: float = USAGE_TTL):
        self.ttl = ttl
        self._caches: dict[str, UsageCache] = {}
        self._lock = threading.Lock()

    def _cache(self, d: str) -> UsageCache:
        with self._lock:
            return self._caches.setdefault(d, UsageCache())

    def get(self, acct: Account) -> dict:
        c = self._cache(acct.dir)
        now = time.time()
        if now - c.fetched_at > self.ttl and now >= c.next_try and c.lock.acquire(blocking=False):
            threading.Thread(target=self._refresh, args=(acct, c), daemon=True).start()
        creds = (_read_json(os.path.join(acct.dir, ".credentials.json")) or {}).get("claudeAiOauth") or {}
        exp = (creds.get("expiresAt") or 0) / 1000
        return {"account": acct.to_dict(), "limits": normalize_usage(c.data) if c.data else [],
                "fetched_at": c.fetched_at or None, "error": c.error,
                "token_expires_at": exp or None, "stale": bool(c.data) and now - c.fetched_at > 3 * self.ttl}

    def _refresh(self, acct: Account, c: UsageCache) -> None:
        try:
            creds = (_read_json(os.path.join(acct.dir, ".credentials.json")) or {}).get("claudeAiOauth") or {}
            token = creds.get("accessToken")
            if not token:
                c.error, c.next_try = "no login in this config dir", time.time() + USAGE_BACKOFF
                return
            if (creds.get("expiresAt") or 0) / 1000 < time.time():
                c.error = "login token expired - start Claude Code on this account to refresh it"
                c.next_try = time.time() + 60
                return
            req = urllib.request.Request(USAGE_URL, headers={
                "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-manager/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                c.data = json.load(r)
            c.fetched_at, c.error, c.next_try = time.time(), "", 0.0
        except urllib.error.HTTPError as e:
            c.error = f"usage endpoint HTTP {e.code}"
            c.next_try = time.time() + (USAGE_BACKOFF if e.code == 429 else 120)
        except Exception as e:  # network down etc.
            c.error = f"usage fetch failed: {e.__class__.__name__}"
            c.next_try = time.time() + 120
        finally:
            c.lock.release()
