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

    def test_known_peers_are_answered_from_the_peer_list(self):
        calls = []
        ts = Tailscale(status_fn=lambda: STATUS, whois_fn=lambda ip: calls.append(ip))
        self.assertEqual(ts.owner_of("100.115.0.1"), 5)        # computenode, mine
        self.assertEqual(ts.owner_of("100.116.0.1"), 5)        # offline peer, still mine
        self.assertEqual(ts.owner_of("100.103.0.1"), 99)       # someone else's machine
        self.assertEqual(calls, [])                            # no whois needed

    def test_whois_fallback_caches_answers_but_not_failures(self):
        calls, answer = [], {"v": None}
        ts = Tailscale(status_fn=lambda: STATUS,
                       whois_fn=lambda ip: calls.append(ip) or (answer["v"] and {"UserProfile": {"ID": answer["v"]}}))
        self.assertIsNone(ts.owner_of("100.99.0.9"))            # whois failed (e.g. tailscaled restarting)
        answer["v"] = 5
        self.assertEqual(ts.owner_of("100.99.0.9"), 5)          # retried right away, not stuck on the failure
        self.assertEqual(ts.owner_of("100.99.0.9"), 5)
        self.assertEqual(calls, ["100.99.0.9", "100.99.0.9"])   # the good answer was cached

    def test_failed_status_keeps_the_last_good_one(self):
        t = {"now": 0.0}
        state = {"v": STATUS}
        ts = Tailscale(status_fn=lambda: state["v"], whois_fn=lambda ip: None, clock=lambda: t["now"], background=False)
        self.assertEqual(ts.my_user(), 5)
        state["v"] = None                                       # tailscaled restarting / CLI timing out
        t["now"] += 31
        self.assertEqual(ts.my_user(), 5)                       # still trusted
        self.assertEqual(ts.owner_of("100.115.0.1"), 5)
        t["now"] += 7200                                        # failing for two hours: still the last good list
        self.assertEqual(ts.my_user(), 5)
        changed = json.loads(json.dumps(STATUS))
        changed["Peer"]["a"]["UserID"] = 77
        state["v"] = changed
        t["now"] += 31
        self.assertEqual(ts.owner_of("100.115.0.1"), 77)        # a working refresh replaces it

    def test_requests_never_wait_for_a_slow_refresh(self):
        import threading
        gate, calls = threading.Event(), []

        def slow_status():
            calls.append(1)
            if len(calls) > 1:
                gate.wait(10)                                   # the CLI taking 20 s to start
            return STATUS
        t = {"now": 0.0}
        ts = Tailscale(status_fn=slow_status, whois_fn=lambda ip: None, clock=lambda: t["now"])
        self.assertEqual(ts.my_user(), 5)                       # first answer is fetched synchronously
        t["now"] += 31
        t0 = time.time()
        self.assertEqual(ts.owner_of("100.115.0.1"), 5)         # stale, refresh starts in the background...
        self.assertEqual(ts.my_user(), 5)
        self.assertLess(time.time() - t0, 1)                    # ...and nobody waited for it
        self.assertEqual(len(calls), 2)                         # exactly one refresh in flight
        gate.set()

    def test_localapi_over_a_unix_socket(self):
        import socket as _s
        import threading
        from cmgr import tailscale as tsmod
        if not hasattr(_s, "AF_UNIX"):
            self.skipTest("no unix sockets here")
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ts.sock")
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        seen = {}

        def serve():
            c, _ = srv.accept()
            seen["req"] = c.recv(4096).decode()
            body = json.dumps(STATUS).encode()
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            c.close()
        threading.Thread(target=serve, daemon=True).start()
        old = tsmod.SOCKET_PATHS
        tsmod.SOCKET_PATHS = [path]
        try:
            os.environ.pop("TS_SOCKET", None)
            self.assertEqual(tsmod.localapi("/localapi/v0/status")["Self"]["UserID"], 5)
            self.assertIn("GET /localapi/v0/status", seen["req"])
            self.assertIn("Host: local-tailscaled.sock", seen["req"])
        finally:
            tsmod.SOCKET_PATHS = old
            srv.close()

    def test_refusal_says_why(self):
        from cmgr.server import Authorizer
        ts = Tailscale(status_fn=lambda: STATUS, whois_fn=lambda ip: None)
        a = Authorizer({"token": "", "trust_local": False}, ts)
        self.assertEqual(a.check("100.115.0.1", ""), (True, ""))
        self.assertIn("another Tailscale user", a.check("100.103.0.1", "")[1])
        self.assertIn("isn't a device Tailscale knows", a.check("100.1.2.3", "")[1])
        down = Authorizer({"token": ""}, Tailscale(status_fn=lambda: None, whois_fn=lambda ip: None))
        self.assertIn("Tailscale isn't answering", down.check("100.115.0.1", "")[1])


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
