from __future__ import annotations

import json
import os
import secrets
import socket

DEFAULT_HOME = os.path.expanduser("~/.claude-manager")
DEFAULT_PORT = 8765

DEFAULTS = {
    "name": "",                  # host label in the UI; defaults to the hostname
    "token": "",                 # optional shared secret; Tailscale identity is enough without it
    "port": DEFAULT_PORT,
    "bind": ["tailscale", "127.0.0.1"],  # "tailscale" = this machine's Tailscale IPv4
    "hub": True,                 # serve the web UI and show the other machines
    "discover": True,            # find the other machines through Tailscale (your own devices only)
    "peers": [],                 # extra machines Tailscale can't find: [{"name": "x", "url": "http://x:8765"}]
    "trust_local": None,         # None = trust this machine's own connections on Windows/macOS, not on Linux
    "accounts": [],              # [] = auto-discover ~/.claude and ~/.claude-*
    "project_roots": [],         # optional; folders you used Claude Code in are listed automatically
    "tmux": "",                  # tmux binary; "" = tmux (or psmux) from PATH
    "claude_command": "claude",  # what new sessions run
}


def home_dir() -> str:
    return os.environ.get("CM_HOME") or DEFAULT_HOME


def config_path() -> str:
    return os.path.join(home_dir(), "config.json")


def load(path: str | None = None) -> dict:
    path = path or config_path()
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["name"] = cfg["name"] or socket.gethostname()
    if isinstance(cfg["bind"], str):
        cfg["bind"] = [cfg["bind"]]
    cfg["_path"] = path
    return cfg


def write_initial(path: str | None = None, token: str | None = None, **overrides) -> dict:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = {k: v for k, v in DEFAULTS.items()}
    cfg["name"] = socket.gethostname()
    cfg["token"] = token if token is not None else secrets.token_urlsafe(24)
    cfg.update(overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg
