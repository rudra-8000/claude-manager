import json
import os
import sys
import tempfile
import unittest

from cmgr import transcript as T
from cmgr.agent import Agent
from cmgr.remote import BOOTSTRAP, SSHClient
from cmgr.server import Hub, RemoteError

sys.path.insert(0, os.path.dirname(__file__))
from test_server import FakeMux, FakeTS  # noqa: E402


def line(**kw):
    return json.dumps(kw)


TRANSCRIPT = [
    line(type="user", message={"role": "user", "content": "fix the flaky test"}, timestamp="t1"),
    line(type="user", isMeta=True, message={"role": "user", "content": "<system-reminder>meta</system-reminder>"}),
    line(type="assistant", message={"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "Looking at it."},
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest -q tests/test_x.py"}}]}),
    line(type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "1 failed", "is_error": True}]}),
    line(type="assistant", isSidechain=True, message={"role": "assistant", "content": [{"type": "text", "text": "subagent"}]}),
    line(type="user", message={"role": "user", "content": "<command-name>/clear</command-name><command-args></command-args>"}),
    line(type="attachment", attachment={"x": 1}),
    line(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": "Fixed."}]}),
]


class TranscriptTests(unittest.TestCase):
    def test_parse(self):
        items = T.parse_lines(TRANSCRIPT)
        self.assertEqual([i["kind"] for i in items], ["user", "assistant", "tool", "result", "command", "assistant"])
        tool = items[2]
        self.assertEqual((tool["name"], tool["summary"], tool["tool_id"]), ("Bash", "pytest -q tests/test_x.py", "tu1"))
        self.assertTrue(items[3]["is_error"])
        self.assertEqual(items[4]["text"], "/clear")

    def test_read_tail_older_and_since(self):
        with tempfile.TemporaryDirectory() as d:
            acct = os.path.join(d, ".claude")
            cwd = "/home/me/proj_x"
            pdir = os.path.join(acct, "projects", T.encode_cwd(cwd))
            os.makedirs(pdir)
            self.assertEqual(T.encode_cwd(cwd), "-home-me-proj-x")
            path = os.path.join(pdir, "sess1.jsonl")
            filler = [line(type="assistant", message={"content": [{"type": "text", "text": f"msg {n} " + "x" * 200}]}) for n in range(300)]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(filler + TRANSCRIPT) + "\n")
            self.assertEqual(T.find(acct, cwd, "sess1"), path)
            old_chunk, T.CHUNK, old_min = T.CHUNK, 4000, T.MIN_ITEMS
            T.MIN_ITEMS = 5
            try:
                tail = T.read(path)
                self.assertEqual(tail["items"][-1]["text"], "Fixed.")
                self.assertGreater(tail["start"], 0)
                older = T.read(path, before=tail["start"])
                self.assertEqual(older["end"], tail["start"])
                self.assertLess(older["start"], tail["start"])
                self.assertTrue(older["items"][-1]["text"].startswith("msg "))
            finally:
                T.CHUNK, T.MIN_ITEMS = old_chunk, old_min
            self.assertEqual(T.read(path, since=tail["end"])["items"], [])
            with open(path, "a", encoding="utf-8") as f:
                f.write(line(type="user", message={"content": "next"}) + "\n" + '{"type": "assist')  # half-written line
            new = T.read(path, since=tail["end"])
            self.assertEqual([i["text"] for i in new["items"]], ["next"])
            self.assertLess(new["end"], os.path.getsize(path))  # the partial line is read next time
            self.assertTrue(T.read(path, since=10 ** 9)["reset"])


def stdio_client():
    return SSHClient("local", argv=[sys.executable, "-u", "-c", BOOTSTRAP])


class RemoteTests(unittest.TestCase):
    def test_in_memory_agent_over_a_pipe(self):
        c = stdio_client()
        self.addCleanup(c.close)
        info = c.call("GET", "/api/info")
        self.assertIn("accounts", info)
        with self.assertRaises(RemoteError) as cm:
            c.call("GET", "/api/decisions", {"dir": __file__})
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(c.call("GET", "/api/info")["name"], info["name"])   # channel still usable

    def test_unreachable_host_gives_readable_error(self):
        c = SSHClient("nowhere", argv=[sys.executable, "-c", "import sys; sys.stderr.write('ssh: connect to host nowhere port 22: Connection refused\\n')"])
        with self.assertRaises(RemoteError) as cm:
            c.call("GET", "/api/info", timeout=10)
        self.assertIn("Connection refused", str(cm.exception))


class HubDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = {"name": "hubbox", "token": "", "accounts": [], "project_roots": [], "tmux": "", "port": 1,
               "_path": os.path.join(self.tmp.name, "config.json"), "peers": [], "discover": False,
               "ssh_aliases": False}  # don't read the developer's real ~/.ssh/config
        agent = Agent(cfg, mux=FakeMux("work"))
        agent._accounts, agent._accounts_at = [], 1e18
        self.ts = FakeTS(machines=[{"name": "guru", "ip": "100.0.0.5"}, {"name": "rudy8k", "ip": "100.0.0.6", "online": False}])
        self.hub = Hub(cfg, agent, self.ts)

    def dev(self, name):
        return next(d for d in self.hub.devices() if d["name"] == name)

    def test_unconfigured_devices_are_listed_but_not_polled(self):
        self.assertEqual(self.dev("guru")["status"], "not connected")
        self.assertEqual(self.dev("rudy8k")["status"], "offline")
        self.assertEqual(self.hub.active(), ["hubbox"])

    def test_ssh_device_is_polled_and_can_be_switched_off(self):
        self.hub.set_device("guru", enabled=True, via="ssh", ssh="me@guru", python="python3")
        self.assertEqual(self.dev("guru")["ssh"], "me@guru")
        self.hub.ssh["guru"] = SSHClient("me@guru", "python3", argv=[sys.executable, "-u", "-c", BOOTSTRAP])  # stands in for ssh
        self.addCleanup(lambda: self.hub.ssh.get("guru") and self.hub.ssh["guru"].close())
        self.assertEqual(self.hub.active(), ["hubbox", "guru"])
        st = self.hub.state()
        self.assertEqual([h["name"] for h in st["hosts"]], ["hubbox", "guru"])
        self.assertIn("accounts", self.hub.client("guru").call("GET", "/api/info"))
        self.hub.set_device("guru", enabled=False)
        self.assertEqual(self.hub.active(), ["hubbox"])
        self.assertEqual(self.dev("guru")["status"], "hidden")
        with open(os.path.join(self.tmp.name, "hub.json")) as f:
            self.assertEqual(json.load(f)["devices"]["guru"], {"enabled": False, "via": "ssh", "ssh": "me@guru", "python": "python3"})

    def test_ssh_config_alias_connects_without_setup(self):
        self.hub.ssh_aliases = lambda: {"guru": "robot"}
        r = self.hub._route("guru")
        self.assertEqual((r["mode"], r["target"], r["python"], r["from_config"]), ("ssh", "robot", "python3", True))
        self.assertEqual(self.hub.active(), ["hubbox", "guru"])
        self.hub.set_device("guru", ssh="other-alias")           # an explicit target wins over the config
        self.assertEqual(self.hub._route("guru")["target"], "other-alias")
        self.ts.machines.append({"name": "rudy-windows", "ip": "100.0.0.7", "os": "windows"})
        self.hub.ssh_aliases = lambda: {"rudy-windows": "rudra"}
        self.assertEqual(self.hub._route("rudy-windows")["python"], "python")   # Windows default

    def test_slow_device_does_not_block_the_page(self):
        import threading
        gate = threading.Event()

        class Slow:
            target, python = "slow", "python3"

            def call(self, *a, **k):
                gate.wait(10)
                return {"info": None, "sessions": [], "usage": [], "prefs": {}}

            def close(self):
                pass
        self.hub.set_device("guru", enabled=True, via="ssh", ssh="slow", python="python3")
        self.hub.ssh["guru"] = Slow()
        import time as _t
        t0 = _t.time()
        hosts = {h["name"]: h for h in self.hub.snapshots(self.hub.active(), wait=0.5)}
        self.assertLess(_t.time() - t0, 3)
        self.assertEqual(hosts["guru"]["error"], "connecting…")
        self.assertEqual(self.dev("guru")["status"], "connecting…")
        gate.set()
        self.hub.inflight["guru"].result(5)
        self.assertTrue(self.hub.snapshots(["guru"], wait=2)[0]["ok"])

    def test_hidden_accounts(self):
        self.hub.set_account_hidden("acct-1", True)
        self.hub.set_account_hidden("acct-2", True)
        self.hub.set_account_hidden("acct-1", False)
        self.assertEqual(self.hub.state()["hidden_accounts"], ["acct-2"])


if __name__ == "__main__":
    unittest.main()


class PreviewTests(unittest.TestCase):
    def test_last_message_prefers_latest_activity(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(TRANSCRIPT) + "\n")
            self.assertEqual(T.last_message(p), "Fixed.")
            with open(p, "a", encoding="utf-8") as f:
                f.write(line(type="assistant", message={"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "make"}}]}) + "\n")
            self.assertEqual(T.last_message(p), "Bash: make")
            with open(p, "a", encoding="utf-8") as f:
                f.write(line(type="user", message={"content": "now **ship** it\nplease"}) + "\n")
            self.assertEqual(T.last_message(p), "you: now ship it")


class FindTests(unittest.TestCase):
    def test_two_sessions_in_one_folder_keep_their_own_transcripts(self):
        import time as _t
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "projects", T.encode_cwd("/w/openpi"))
            os.makedirs(pdir)
            a, b = os.path.join(pdir, "A.jsonl"), os.path.join(pdir, "B.jsonl")
            for f, t in ((a, 1000), (b, 2000)):
                open(f, "w").close()
                os.utime(f, (t, t))
            self.assertEqual(T.find(d, "/w/openpi", "A", 0, others={"B"}), a)   # B is newer but belongs to the other session
            self.assertEqual(T.find(d, "/w/openpi", "B", 0, others={"A"}), b)
            c = os.path.join(pdir, "C.jsonl")                                   # A ran /clear: its new file is followed
            open(c, "w").close()
            self.assertEqual(T.find(d, "/w/openpi", "A", 0, others={"B"}), c)
