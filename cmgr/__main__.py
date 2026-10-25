"""cm - control Claude Code sessions in tmux/psmux on all your machines from one web page.

  cm init [--token T] [--peer NAME=URL ...]   write ~/.claude-manager/config.json
  cm serve                                    run the agent (and the hub UI if "hub": true)
  cm doctor                                   check tmux, accounts, usage, DECISIONS, Tailscale
  cm link                                     print a one-tap login URL for your phone
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import config as cfgmod
from .agent import Agent


def cmd_init(a) -> None:
    path = a.config or cfgmod.config_path()
    if os.path.exists(path) and not a.force:
        raise SystemExit(f"{path} exists (use --force to overwrite)")
    peers = []
    for p in a.peer or []:
        name, _, url = p.partition("=")
        if not url:
            raise SystemExit(f"--peer wants NAME=URL, got {p!r}")
        peers.append({"name": name, "url": url})
    over = {"peers": peers, "hub": not a.no_hub, "project_roots": a.project_root or []}
    cfg = cfgmod.write_initial(path, token=a.token, **over)
    print(f"wrote {path}")
    print(f"token: {cfg['token']}")
    print("Use the same token on every machine:  cm init --token <token>")


def cmd_serve(a) -> None:
    from .server import serve
    serve(cfgmod.load(a.config))


def cmd_doctor(a) -> None:
    from .server import tailscale_ip, resolve_binds
    cfg = cfgmod.load(a.config)
    agent = Agent(cfg)
    ok = lambda b: "ok  " if b else "FAIL"
    print(f"config      {cfg['_path']} ({'exists' if os.path.exists(cfg['_path']) else 'missing - run cm init'})")
    print(f"name        {cfg['name']}   hub={cfg['hub']}   port={cfg['port']}")
    print(f"[{ok(bool(cfg['token']))}] token set")
    print(f"[{ok(agent.mux.available())}] tmux: {agent.mux.binary}")
    ts = tailscale_ip()
    print(f"[{ok(bool(ts))}] tailscale ip: {ts}")
    print(f"       will listen on: {', '.join(resolve_binds(cfg['bind']))}")
    accts = agent.accounts()
    print(f"[{ok(bool(accts))}] accounts: {len(accts)}")
    for acc in accts:
        print(f"       - {acc.to_dict()['label']:<24} {acc.email or '(no email found)':<32} {acc.subscription:<6} {acc.dir}")
    if accts:
        agent.usage()  # kicks off background fetches
        time.sleep(4)
        for u in agent.usage():
            lim = ", ".join(f"{l['label']} {l['percent']}%" for l in u["limits"]) or "-"
            print(f"       usage {u['account']['label']}: {lim} {u['error']}")
    try:
        sess = agent.sessions()
        cl = [s for s in sess if s["is_claude"]]
        print(f"[ok  ] panes: {len(sess)} total, {len(cl)} running Claude")
        for s in cl:
            acct = (s.get("account") or {}).get("label", "?")
            print(f"       - {s['target']:<20} {s['status']:<8} {acct:<28} {s['cwd']}"
                  + ("  [DECISIONS.md]" if s["decisions_path"] else ""))
    except Exception as e:
        print(f"[FAIL] listing panes: {e}")
    for p in cfg.get("peers") or []:
        from .server import RemoteClient
        try:
            info = RemoteClient(p["url"], p.get("token") or cfg["token"]).call("GET", "/api/info", timeout=5)
            print(f"[ok  ] peer {p['name']}: {info['os']} tmux_ok={info['tmux_ok']}")
        except Exception as e:
            print(f"[FAIL] peer {p['name']} ({p['url']}): {e}")


def cmd_link(a) -> None:
    from .server import tailscale_ip
    cfg = cfgmod.load(a.config)
    host = a.host or tailscale_ip() or "127.0.0.1"
    base = host if host.startswith("http") else f"http://{host}:{cfg['port']}"
    print(f"{base.rstrip('/')}/#token={cfg['token']}")
    print("Open it once on each device; the token is then remembered and removed from the address bar.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="cm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default ~/.claude-manager/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write a config file")
    p.add_argument("--token", help="reuse the token from another machine")
    p.add_argument("--peer", action="append", help="NAME=URL of another machine (repeatable)")
    p.add_argument("--project-root", action="append", help="folder whose sub-folders are projects (repeatable)")
    p.add_argument("--no-hub", action="store_true", help="agent only, no web UI on this machine")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    sub.add_parser("serve", help="run the agent / hub").set_defaults(fn=cmd_serve)
    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("link", help="print a login URL with the token embedded")
    p.add_argument("--host", help="host or full base URL, e.g. https://pc.tailnet.ts.net (default: Tailscale IP)")
    p.set_defaults(fn=cmd_link)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
