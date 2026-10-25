"""Per-machine side: what runs in tmux here, which Claude account each session uses,
usage for the accounts on this machine, and DECISIONS.md files."""
from __future__ import annotations

import hashlib
import os
import platform
import sys
import threading
import time

from . import __version__
from .claude_state import (UsageTracker, discover_accounts, expand, last_text_line, match_proc,
                           read_registry, screen_status)
from .decisions import DecisionsCache, find_file, find_in_dir
from .mux import Mux, MuxError


class AgentError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class Agent:
    def __init__(self, cfg: dict, mux: Mux | None = None):
        self.cfg = cfg
        self.mux = mux or Mux(cfg.get("tmux") or None)
        self.usage_tracker = UsageTracker()
        self.decisions_cache = DecisionsCache()
        self._accounts = None
        self._accounts_at = 0.0
        self._dec_lookup: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ accounts
    def accounts(self):
        now = time.time()
        if self._accounts is None or now - self._accounts_at > 60:
            self._accounts = discover_accounts(self.cfg.get("accounts") or None)
            self._accounts_at = now
        return self._accounts

    def _account_by_dir(self, d: str):
        for a in self.accounts():
            if os.path.normcase(a.dir) == os.path.normcase(expand(d)):
                return a
        return None

    # ------------------------------------------------------------ info
    def info(self) -> dict:
        return {"name": self.cfg["name"], "platform": sys.platform, "os": platform.platform(terse=True),
                "version": __version__, "tmux": self.mux.binary, "tmux_ok": self.mux.available(),
                "accounts": [a.to_dict() for a in self.accounts()]}

    # ------------------------------------------------------------ sessions
    def _decisions_for(self, cwd: str) -> str | None:
        if not cwd:
            return None
        now = time.time()
        hit = self._dec_lookup.get(cwd)
        if hit and now - hit[0] < 30:
            return hit[1]
        path = find_file(cwd)
        self._dec_lookup[cwd] = (now, path)
        return path

    def sessions(self) -> list[dict]:
        panes = self.mux.list_panes()
        accounts = self.accounts()
        procs = read_registry(accounts)
        by_dir = {a.dir: a for a in accounts}
        out = []
        for p in panes:
            proc = match_proc(p, procs)
            is_claude = proc is not None or "claude" in p.command.lower()
            d = p.to_dict()
            d["is_claude"] = is_claude
            d["claude"] = proc.to_dict() if proc else None
            acct = by_dir.get(proc.account_dir) if proc else None
            d["account"] = acct.to_dict() if acct else None
            cwd = (proc.cwd if proc else "") or p.cwd
            d["cwd"] = cwd
            dec = self._decisions_for(cwd)
            d["decisions_path"] = dec
            d["project"] = os.path.basename(os.path.dirname(dec)) if dec else os.path.basename(cwd.rstrip("\\/"))
            d["status"], d["preview"] = ("shell", "")
            if is_claude and not p.dead:
                try:
                    screen = self.mux.capture(p.target)
                    d["status"] = screen_status(screen, proc.status if proc else "")
                    d["preview"] = last_text_line(screen)
                except MuxError:
                    d["status"] = "unknown"
            elif p.dead:
                d["status"] = "dead"
            out.append(d)
        return out

    def _check_target(self, target: str):
        for p in self.mux.list_panes():
            if p.target == target:
                return p
        raise AgentError(f"no such pane: {target}", 404)

    def pane(self, target: str, history: int = 300) -> dict:
        p = self._check_target(target)
        text = self.mux.capture(target, history=min(max(int(history), 0), 5000))
        visible = "\n".join(text.splitlines()[-p.height:])
        return {"target": target, "text": text, "width": p.width, "height": p.height,
                "hash": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16],
                "status": screen_status(visible)}

    def send(self, target: str, text: str, submit: bool = True) -> dict:
        self._check_target(target)
        if len(text) > 400_000:
            raise AgentError("prompt too large (400k chars max)")
        self.mux.send_text(target, text, submit=submit)
        return {"ok": True}

    def keys(self, target: str, keys: list[str] | None = None, literal: str = "") -> dict:
        self._check_target(target)
        try:
            if literal:
                self.mux.send_literal(target, literal)
            if keys:
                self.mux.send_keys(target, keys)
        except MuxError as e:
            raise AgentError(str(e))
        return {"ok": True}

    def new_session(self, cwd: str, account_dir: str = "", name: str = "", resume: bool = False) -> dict:
        cwd = expand(cwd)
        if not os.path.isdir(cwd):
            raise AgentError(f"not a directory: {cwd}")
        acct = self._account_by_dir(account_dir) if account_dir else None
        if account_dir and not acct:
            raise AgentError(f"unknown account dir: {account_dir}")
        env = acct.env() if acct else {}
        cmd = self.cfg.get("claude_command") or "claude"
        if resume:
            cmd += " --continue"
        sname = self.mux.new_session(name or os.path.basename(cwd.rstrip("\\/")), cwd, env, cmd)
        return {"ok": True, "session": sname}

    # ------------------------------------------------------------ usage
    def usage(self) -> list[dict]:
        return [self.usage_tracker.get(a) for a in self.accounts()]

    # ------------------------------------------------------------ decisions / projects
    def _resolve_decisions(self, dir_or_file: str) -> str:
        p = expand(dir_or_file)
        if os.path.isfile(p) and os.path.basename(p).lower() == "decisions.md":
            return p
        hit = find_file(p) if os.path.isdir(p) else None
        if not hit:
            raise AgentError(f"no DECISIONS.md at or above {p}", 404)
        return hit

    def decisions(self, dir_or_file: str, bodies: int = 25) -> dict:
        return self.decisions_cache.summary(self._resolve_decisions(dir_or_file), bodies=bodies)

    def decision_bodies(self, dir_or_file: str, indexes: list[int]) -> dict:
        path = self._resolve_decisions(dir_or_file)
        return {"path": path, "bodies": {str(k): v for k, v in self.decisions_cache.bodies(path, indexes).items()}}

    def projects(self) -> list[dict]:
        seen, out = set(), []

        def add(d: str, dec: str | None):
            key = os.path.normcase(d)
            if key in seen:
                return
            seen.add(key)
            out.append({"name": os.path.basename(d.rstrip("\\/")) or d, "dir": d, "decisions_path": dec})

        for root in self.cfg.get("project_roots") or []:
            root = expand(root)
            if not os.path.isdir(root):
                continue
            dec = find_in_dir(root)
            if dec:
                add(root, dec)
            try:
                children = sorted(os.listdir(root))
            except OSError:
                continue
            for c in children:
                full = os.path.join(root, c)
                if os.path.isdir(full) and not c.startswith("."):
                    add(full, find_in_dir(full))
        return out
