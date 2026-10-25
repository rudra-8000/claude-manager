from __future__ import annotations

import json
import os
import secrets
import socket

DEFAULT_HOME = os.path.expanduser("~/.claude-manager")
DEFAULT_PORT = 8765

DEFAULTS = {
    "name": "",                  # host label in the UI; defaults to the hostname
    "token": "",                 # shared secret, same on every machine
    "port": DEFAULT_PORT,
    "bind": ["tailscale", "127.0.0.1"],  # "tailscale" = this machine's Tailscale IPv4
    "hub": True,                 # serve the web UI and aggregate the peers below
    "peers": [],                 # [{"name": "lab-pc", "url": "http://lab-pc:8765"}]
    "accounts": [],              # [] = auto-discover ~/.claude and ~/.claude-*
    "project_roots": [],         # directories whose sub-folders are scanned for DECISIONS.md
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
    cfg["token"] = token or secrets.token_urlsafe(24)
    cfg.update(overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg
