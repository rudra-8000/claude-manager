"""End-to-end over HTTP with a fake tmux: a hub plus one peer, both real servers on loopback."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cmgr.agent import Agent
from cmgr.mux import ALLOWED_KEYS, MuxError, Pane
from cmgr.server import Authorizer, Hub, make_handler

TOKEN = "test-token"


class FakeTS:
    """Stands in for the tailscale CLI: my user id 1, my IP 100.0.0.1."""

    def __init__(self, machines=(), owners=None):
        self.machines = list(machines)
        self.owners = owners or {}

    def my_ips(self):
        return ["100.0.0.1"]

    def my_user(self):
        return 1

    def my_machines(self):
        return self.machines

    def owner_of(self, ip):
        return self.owners.get(ip)


class FakeMux:
    binary = "fake-tmux"

    def __init__(self, session="work"):
        self.panes = [Pane(session, "@1", 0, "w", "%1", 0, "claude", os.getcwd(), 1, 100, 30, False, True, False)]
        self.sent, self.keys = [], []

    def available(self):
        return True

    def list_panes(self):
        return list(self.panes)

    def capture(self, target, history=0):
        return "\x1b[1mhello\x1b[0m\n✻ Working (esc to interrupt)\n"

    def send_text(self, target, text, submit=True):
        self.sent.append((target, text, submit))

    def send_keys(self, target, keys):
        bad = [k for k in keys if k not in ALLOWED_KEYS]
        if bad:
            raise MuxError(f"key(s) not allowed: {bad}")
        self.keys.append((target, keys))

    def send_literal(self, target, text):
        self.keys.append((target, ["literal:" + text]))

    def new_session(self, name, cwd, env, command):
        self.created = (name, cwd, env, command)
        return name


def start(cfg, agent, hub_enabled, ts=None):
    ts = ts or FakeTS()
    hub = Hub(cfg, agent, ts) if hub_enabled else None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, agent, hub, Authorizer(cfg, ts)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = {"token": TOKEN, "accounts": [], "project_roots": [], "claude_command": "claude", "tmux": "",
                "port": 1, "trust_local": False, "discover": False}
        cls.peer_mux = FakeMux("lab")
        peer_cfg = {**base, "name": "peer", "_path": os.path.join(cls.tmp.name, "peer.json")}
        cls.peer_agent = Agent(peer_cfg, mux=cls.peer_mux)
        cls.peer_agent._accounts, cls.peer_agent._accounts_at = [], 1e18
        cls.peer_srv, cls.peer_url = start(peer_cfg, cls.peer_agent, False)

        cls.hub_mux = FakeMux("work")
        hub_cfg = {**base, "name": "hub", "_path": os.path.join(cls.tmp.name, "hub.json"),
                   "peers": [{"name": "peer", "url": cls.peer_url}, {"name": "gone", "url": "http://127.0.0.1:9"}]}
        cls.hub_agent = Agent(hub_cfg, mux=cls.hub_mux)
        cls.hub_agent._accounts, cls.hub_agent._accounts_at = [], 1e18
        cls.hub_srv, cls.url = start(hub_cfg, cls.hub_agent, True)

    @classmethod
    def tearDownClass(cls):
        cls.hub_srv.shutdown()
        cls.peer_srv.shutdown()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, token=TOKEN):
        r = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                   method=method, headers={"Content-Type": "application/json",
                                                           **({"Authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_auth_required(self):
        self.assertEqual(self.req("GET", "/hub/state", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/sessions", token="wrong")[0], 401)

    def test_ui_served_without_auth(self):
        with urllib.request.urlopen(self.url + "/") as r:
            self.assertIn(b"Claude Manager", r.read())

    def test_state_aggregates_hosts_and_marks_offline_peer(self):
        code, st = self.req("GET", "/hub/state")
        self.assertEqual(code, 200)
        hosts = {h["name"]: h for h in st["hosts"]}
        self.assertEqual(set(hosts), {"hub", "peer", "gone"})
        self.assertTrue(hosts["hub"]["ok"])
        self.assertTrue(hosts["peer"]["ok"])
        self.assertFalse(hosts["gone"]["ok"])
        s = hosts["peer"]["sessions"][0]
        self.assertEqual((s["target"], s["status"], s["is_claude"]), ("lab:0.0", "working", True))

    def test_send_is_forwarded_to_the_right_host(self):
        code, _ = self.req("POST", "/hub/send", {"host": "peer", "target": "lab:0.0", "text": "a\nb", "submit": True})
        self.assertEqual(code, 200)
        self.assertEqual(self.peer_mux.sent[-1], ("lab:0.0", "a\nb", True))
        self.assertNotIn(("lab:0.0", "a\nb", True), self.hub_mux.sent)

    def test_unknown_target_rejected(self):
        code, _ = self.req("POST", "/hub/send", {"host": "hub", "target": "nope:0.0", "text": "x"})
        self.assertEqual(code, 404)
        code, _ = self.req("POST", "/hub/send", {"host": "peer", "target": "nope:0.0", "text": "x"})
        self.assertEqual(code, 404)  # peer's 404 passes through the hub

    def test_key_allow_list(self):
        self.assertEqual(self.req("POST", "/hub/keys", {"host": "hub", "target": "work:0.0", "keys": ["Escape"]})[0], 200)
        code, body = self.req("POST", "/hub/keys", {"host": "hub", "target": "work:0.0", "keys": ["C-z"]})
        self.assertEqual(code, 400)
        self.assertIn("not allowed", body["error"])

    def test_pane_and_decisions(self):
        code, p = self.req("GET", "/hub/pane?host=peer&target=lab:0.0&history=10")
        self.assertEqual(code, 200)
        self.assertEqual(p["status"], "working")
        proj = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(proj, "src"), exist_ok=True)
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "decisions_noble.md"), encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(proj, "DECISIONS.md"), "w", encoding="utf-8") as f:
            f.write(text)
        fix = os.path.join(proj, "src")  # found by walking up
        code, d = self.req("GET", "/hub/decisions?" + urllib.parse.urlencode({"host": "peer", "dir": fix, "bodies": 1}))
        self.assertEqual(code, 200)
        self.assertEqual(d["count"], 2)
        code, b = self.req("GET", "/hub/decisions/bodies?" + urllib.parse.urlencode({"host": "peer", "dir": fix, "i": "1"}))
        self.assertTrue(b["bodies"]["1"].startswith("## D-013"))

    def test_decisions_refuses_other_files(self):
        code, _ = self.req("GET", "/api/decisions?" + urllib.parse.urlencode({"dir": __file__}))
        self.assertEqual(code, 404)
        fix = os.path.join(os.path.dirname(__file__), "fixtures", "decisions_noble.md")
        code, _ = self.req("GET", "/hub/decisions?" + urllib.parse.urlencode({"host": "peer", "dir": fix}))
        self.assertEqual(code, 404)  # only files named DECISIONS.md / decisions.md are readable

    def test_prefs_roundtrip(self):
        code, p = self.req("POST", "/hub/prefs", {"section": "perplexity", "key": "trnsfr", "value": "https://www.perplexity.ai/search/abc"})
        self.assertEqual(p["perplexity"]["trnsfr"], "https://www.perplexity.ai/search/abc")
        code, st = self.req("GET", "/hub/state")
        self.assertEqual(st["prefs"]["perplexity"]["trnsfr"], "https://www.perplexity.ai/search/abc")
        self.assertEqual(self.req("POST", "/hub/prefs", {"section": "evil", "key": "k", "value": 1})[0], 400)
        # written to the peer too, so the peer's own page shows the same link
        self.assertEqual(self.peer_agent.prefs()["perplexity"]["trnsfr"], "https://www.perplexity.ai/search/abc")
        self.req("POST", "/hub/prefs", {"section": "perplexity", "key": "trnsfr", "value": ""})
        self.assertNotIn("trnsfr", self.peer_agent.prefs()["perplexity"])

    def test_new_session_validates(self):
        code, _ = self.req("POST", "/hub/new", {"host": "hub", "cwd": os.path.join(self.tmp.name, "missing")})
        self.assertEqual(code, 400)
        code, r = self.req("POST", "/hub/new", {"host": "hub", "cwd": self.tmp.name, "resume": True})
        self.assertEqual(code, 200)
        self.assertEqual(self.hub_mux.created[3], "claude --continue")
        code, _ = self.req("POST", "/hub/new", {"host": "hub", "cwd": self.tmp.name, "account_dir": "/not/an/account"})
        self.assertEqual(code, 400)


class AuthorizerTests(unittest.TestCase):
    def ts(self):
        return FakeTS(owners={"100.0.0.7": 1, "100.0.0.9": 2})

    def test_own_tailscale_devices_need_no_token(self):
        a = Authorizer({"token": "", "trust_local": False}, self.ts())
        self.assertTrue(a.check("100.0.0.7", "")[0])          # my phone / other PC
        self.assertFalse(a.check("100.0.0.9", "")[0])         # a machine shared in by someone else
        self.assertFalse(a.check("100.0.0.8", "")[0])         # unknown

    def test_local_connections(self):
        strict = Authorizer({"token": "t", "trust_local": False}, self.ts())
        self.assertFalse(strict.check("127.0.0.1", "")[0])    # other users on a shared Linux box
        self.assertFalse(strict.check("100.0.0.1", "")[0])    # ... also via the box's own Tailscale IP
        self.assertTrue(strict.check("127.0.0.1", "t")[0])
        self.assertTrue(Authorizer({"trust_local": True}, self.ts()).check("127.0.0.1", "")[0])

    def test_token_always_works_and_wrong_token_does_not(self):
        a = Authorizer({"token": "t", "trust_local": False}, self.ts())
        self.assertTrue(a.check("8.8.8.8", "t")[0])
        self.assertFalse(a.check("8.8.8.8", "x")[0])
        self.assertFalse(Authorizer({"token": ""}, self.ts()).check("8.8.8.8", "")[0])

    def test_tailscale_can_be_disabled(self):
        a = Authorizer({"token": "", "trust_local": False, "allow_tailscale": False}, self.ts())
        self.assertFalse(a.check("100.0.0.7", "")[0])


class DiscoveryTests(unittest.TestCase):
    def test_hub_finds_cm_on_my_machines(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mux = FakeMux("lab")
        peer_cfg = {"name": "labbox", "token": "", "accounts": [], "project_roots": [], "tmux": "",
                    "_path": os.path.join(tmp.name, "p.json"), "trust_local": True}
        peer_agent = Agent(peer_cfg, mux=mux)
        peer_agent._accounts, peer_agent._accounts_at = [], 1e18
        srv, url = start(peer_cfg, peer_agent, False, ts=FakeTS())
        self.addCleanup(srv.shutdown)
        port = int(url.rsplit(":", 1)[1])
        hub_cfg = {"name": "me", "token": "", "accounts": [], "project_roots": [], "tmux": "", "port": port,
                   "_path": os.path.join(tmp.name, "h.json"), "peers": [], "discover": True}
        hub_agent = Agent(hub_cfg, mux=FakeMux("work"))
        hub_agent._accounts, hub_agent._accounts_at = [], 1e18
        ts = FakeTS(machines=[{"name": "labbox", "ip": "127.0.0.1"}, {"name": "dead", "ip": "127.0.0.2"}])
        hub = Hub(hub_cfg, hub_agent, ts)
        st = hub.state()
        names = {h["name"]: h for h in st["hosts"]}
        self.assertEqual(set(names), {"me", "labbox"})   # 127.0.0.2 has no cm -> not listed
        self.assertEqual(names["labbox"]["sessions"][0]["target"], "lab:0.0")


import urllib.parse  # noqa: E402

if __name__ == "__main__":
    unittest.main()
