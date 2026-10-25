import json
import os
import tempfile
import time
import unittest

from cmgr import claude_state as cs
from cmgr.tailscale import Tailscale, parse_status

STATUS = {
    "BackendState": "Running",
    "Self": {"HostName": "rudy", "DNSName": "rudy.tail1.ts.net.", "UserID": 5, "TailscaleIPs": ["100.74.0.1", "fd7a::1"]},
    "Peer": {
        "a": {"HostName": "computenode-01", "DNSName": "computenode-01.tail1.ts.net.", "OS": "linux", "Online": True, "UserID": 5, "TailscaleIPs": ["100.115.0.1"]},
        "b": {"HostName": "john", "OS": "linux", "Online": False, "UserID": 5, "TailscaleIPs": ["100.116.0.1"]},
        "c": {"HostName": "OnePlus", "OS": "android", "Online": True, "UserID": 5, "TailscaleIPs": ["100.126.0.1"]},
        "d": {"HostName": "patrick", "OS": "linux", "Online": True, "UserID": 99, "TailscaleIPs": ["100.103.0.1"]},
    },
}


class TailscaleTests(unittest.TestCase):
    def test_only_my_online_computers(self):
        ts = Tailscale(status_fn=lambda: STATUS, whois_fn=lambda ip: None)
        self.assertEqual([m["name"] for m in ts.my_machines()], ["computenode-01"])
        self.assertEqual(ts.my_ips(), ["100.74.0.1", "fd7a::1"])
        self.assertEqual(ts.my_user(), 5)

    def test_logged_out(self):
        st = parse_status({"BackendState": "NoState", "Self": {"HostName": "rudy"}})
        self.assertFalse(st["running"])
        ts = Tailscale(status_fn=lambda: {"BackendState": "NeedsLogin"}, whois_fn=lambda ip: None)
        self.assertEqual((ts.my_machines(), ts.my_ips(), ts.my_user()), ([], [], None))

    def test_whois_cached(self):
        calls = []
        ts = Tailscale(status_fn=lambda: STATUS,
                       whois_fn=lambda ip: calls.append(ip) or {"UserProfile": {"ID": 5, "LoginName": "x"}})
        self.assertEqual(ts.owner_of("100.115.0.1"), 5)
        self.assertEqual(ts.owner_of("100.115.0.1"), 5)
        self.assertEqual(calls, ["100.115.0.1"])


class KnownProjectsTests(unittest.TestCase):
    def test_folders_from_claude_history_newest_first(self):
        with tempfile.TemporaryDirectory() as root:
            acct_dir = os.path.join(root, ".claude-b")
            p_old, p_new = os.path.join(root, "old proj"), os.path.join(root, "new_proj")
            for d in (acct_dir, p_old, p_new):
                os.makedirs(d)
            with open(os.path.join(acct_dir, ".claude.json"), "w") as f:
                json.dump({"projects": {p_old: {}, p_new: {}, os.path.join(root, "deleted"): {}}}, f)
            for p, t in ((p_old, 1000), (p_new, 2000)):
                enc = os.path.join(acct_dir, "projects", "".join(c if c.isalnum() else "-" for c in p))
                os.makedirs(enc)
                os.utime(enc, (t, t))
            got = cs.known_projects([cs.Account(dir=acct_dir)])
            self.assertEqual([os.path.basename(p["dir"]) for p in got], ["new_proj", "old proj"])
            self.assertEqual(got[0]["last_used"], 2000)


class RemoteControlLinkTests(unittest.TestCase):
    def test_bridge_session_becomes_link(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sessions"))
            with open(os.path.join(d, "sessions", "1.json"), "w") as f:
                json.dump({"pid": 1, "tmux": "0:@1.%1", "bridgeSessionId": "session_01ABC", "startedAt": 1}, f)
            procs = cs.read_registry([cs.Account(dir=d)], check_alive=lambda pid: True)
            self.assertEqual(procs[0].to_dict()["remote_url"], "https://claude.ai/code/session_01ABC")


if __name__ == "__main__":
    unittest.main()


class PortTests(unittest.TestCase):
    def test_recognises_cm_servers(self):
        from cmgr.ports import is_cm_server
        self.assertTrue(is_cm_server("/home_local/rudra_1/miniforge3/bin/python3.13 -m cmgr serve"))
        self.assertTrue(is_cm_server("/home_local/rudra_1/miniforge3/bin/python3.13 /home_local/rudra_1/miniforge3/bin/cm serve"))
        self.assertTrue(is_cm_server(r'"C:\Users\rudra\anaconda3\Scripts\cm.exe" serve'))
        self.assertTrue(is_cm_server("cm serve --port 8766"))
        self.assertFalse(is_cm_server(r"C:\Program Files\Microsoft VS Code\Code.exe --type=utility"))
        self.assertFalse(is_cm_server("python -m http.server 8765"))

    def test_parsers(self):
        from cmgr.ports import parse_netstat, parse_ss
        ss = 'LISTEN 0 5 100.115.95.117:8765 0.0.0.0:* users:(("python3.13",pid=4242,fd=3))\n' \
             'LISTEN 0 5 127.0.0.1:8765 0.0.0.0:* users:(("python3.13",pid=4242,fd=4))\n'
        self.assertEqual(parse_ss(ss), [4242])
        ns = "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       51132\n" \
             "  TCP    127.0.0.1:18765        0.0.0.0:0              LISTENING       7\n" \
             "  TCP    10.0.0.2:51000         1.2.3.4:443            ESTABLISHED     9\n"
        self.assertEqual(parse_netstat(ns, 8765), [51132])
