"""cm - control Claude Code sessions in tmux/psmux on all your machines from one web page.

  cm up [--restart]   start the server in a detached tmux/psmux session "cm" (run this on every machine)
  cm down             stop it
  cm doctor           check tmux, accounts, usage, Tailscale and which other machines are reachable
  cm serve            run the server in the foreground (what `cm up` runs)

No configuration is needed: your other machines are found through Tailscale, and only devices of your
own Tailscale user can connect. ~/.claude-manager/config.json is optional (see README).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import config as cfgmod
from .agent import Agent

SESSION = "cm"
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(a) -> dict:
    cfg = cfgmod.load(a.config)
    if getattr(a, "port", None):
        cfg["port"] = a.port
    return cfg


def _urls(cfg) -> list[str]:
    from .tailscale import Tailscale
    me = Tailscale().status()["self"]
    out = []
    if me:
        if me.get("dns"):
            out.append(f"http://{me['dns']}:{cfg['port']}")
        out += [f"http://{ip}:{cfg['port']}" for ip in me["ips"] if ":" not in ip]
    return out or [f"http://127.0.0.1:{cfg['port']}"]


def cmd_serve(a) -> None:
    from .server import serve
    serve(_cfg(a))


def _stop(mux) -> bool:
    if SESSION not in mux.session_names():
        return False
    try:
        mux.send_keys(SESSION, ["C-c"])  # let the server exit cleanly (Windows doesn't kill children with the pane)
        time.sleep(1.0)
    except Exception:
        pass
    mux._run("kill-session", "-t", SESSION)
    return True


def cmd_up(a) -> None:
    from .mux import Mux
    cfg = _cfg(a)
    mux = Mux(cfg.get("tmux") or None)
    if not mux.available():
        raise SystemExit(f"tmux / psmux not found ({mux.binary})")
    if SESSION in mux.session_names():
        if not a.restart:
            print(f"already running in tmux session '{SESSION}' (use --restart after `git pull`)")
            print("open: " + "  or  ".join(_urls(cfg)))
            return
        _stop(mux)
    exe = sys.executable
    args = " -m cmgr serve" + (f" --port {a.port}" if a.port else "") + (f' --config "{a.config}"' if a.config else "")
    # typed into the session's shell, so a crash stays readable with `tmux attach -t cm`
    cmd = (f'& "{exe}"' if sys.platform == "win32" else f'"{exe}"') + args
    mux.new_session(SESSION, PKG_PARENT, {}, cmd)
    time.sleep(3.0)
    from .claude_state import strip_ansi
    out = [ln for ln in strip_ansi(mux.capture(SESSION)).splitlines() if ln.strip()]
    tail = [ln for ln in out if "[cm]" in ln or "Error" in ln or "error" in ln][-6:]
    print("\n".join(tail) if tail else "\n".join(out[-6:]))
    print(f"\nrunning in tmux session '{SESSION}' (see it with: tmux attach -t {SESSION})")
    print("open: " + "  or  ".join(_urls(cfg)))


def cmd_down(a) -> None:
    from .mux import Mux
    mux = Mux(_cfg(a).get("tmux") or None)
    print("stopped" if _stop(mux) else "not running")


def cmd_doctor(a) -> None:
    from .server import RemoteClient, resolve_binds
    from .tailscale import Tailscale
    cfg = _cfg(a)
    agent = Agent(cfg)
    ok = lambda b: "ok  " if b else "FAIL"
    exists = os.path.exists(cfg["_path"])
    print(f"config      {cfg['_path'] if exists else '(none - defaults, which is fine)'}")
    print(f"name        {cfg['name']}   port={cfg['port']}")
    print(f"[{ok(agent.mux.available())}] tmux: {agent.mux.binary}")
    ts = Tailscale()
    st = ts.status()
    print(f"[{ok(st['running'])}] tailscale: " + (", ".join(st["self"]["ips"]) if st["running"] else "not running / logged out"))
    print(f"       will listen on: {', '.join(resolve_binds(cfg['bind'])) or 'nothing!'}")
    accts = agent.accounts()
    print(f"[{ok(bool(accts))}] accounts: {len(accts)}")
    for acc in accts:
        print(f"       - {acc.to_dict()['label']:<24} {acc.email or '(no email found)':<32} {acc.subscription:<6} {acc.dir}")
    if accts:
        agent.usage()  # starts the background fetches
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
    print(f"[ok  ] projects (folders Claude Code was used in): {len(agent.projects())}")
    machines = ts.my_machines()
    print(f"[ok  ] your other online machines on Tailscale: {len(machines)}")
    for m in machines:
        try:
            info = RemoteClient(f"http://{m['ip']}:{cfg['port']}", cfg.get("token", "")).call("GET", "/api/info", timeout=3)
            print(f"       - {m['name']:<20} cm running ({info.get('os', '?')})")
        except Exception as e:
            msg = str(e)
            if "unreachable" in msg:
                hint = "cm not running there (run `cm up` on it)"
            elif "token" in msg:
                hint = "older cm version there - update it: git pull && cm up --restart"
            else:
                hint = msg
            print(f"       - {m['name']:<20} {hint}")
    for p in cfg.get("peers") or []:
        try:
            RemoteClient(p["url"], p.get("token") or cfg.get("token", "")).call("GET", "/api/info", timeout=3)
            print(f"[ok  ] configured peer {p['name']}")
        except Exception as e:
            print(f"[FAIL] configured peer {p['name']} ({p['url']}): {e}  - remove it from {cfg['_path']} if it is not real")


def cmd_init(a) -> None:
    path = a.config or cfgmod.config_path()
    if os.path.exists(path) and not a.force:
        raise SystemExit(f"{path} exists (use --force to overwrite)")
    over = {"hub": not a.no_hub, "project_roots": a.project_root or []}
    cfgmod.write_initial(path, token=a.token or "", **over)
    print(f"wrote {path} (optional - everything works without it)")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="cm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file (default ~/.claude-manager/config.json, optional)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("up", help="start the server in a detached tmux session")
    p.add_argument("--restart", action="store_true", help="restart it (e.g. after git pull)")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_up)
    sub.add_parser("down", help="stop the server").set_defaults(fn=cmd_down)
    p = sub.add_parser("serve", help="run the server in the foreground")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("init", help="write an optional config file")
    p.add_argument("--token", help="optional shared token (only needed for access from outside your Tailscale user)")
    p.add_argument("--project-root", action="append", help="extra folder whose sub-folders are projects")
    p.add_argument("--no-hub", action="store_true", help="don't serve the web UI on this machine")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
