import json
import os
import tempfile
import time
import unittest

from cmgr import claude_state as cs
from cmgr import decisions as dec
from cmgr.mux import Pane, parse_panes, _SEP, _FIELDS, safe_session_name

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class DecisionsTests(unittest.TestCase):
    def test_trnsfr_format(self):
        es = dec.parse(open(os.path.join(FIX, "decisions_trnsfr.md"), encoding="utf-8").read())
        self.assertEqual([e["id"] for e in es], ["D-248", "D-247"])  # preamble skipped, fence ignored
        e = es[0]
        self.assertEqual(e["date"], "2026-10-23")
        self.assertEqual(e["time"], "17:31")
        self.assertEqual(e["tz"], "UTC+8")
        self.assertEqual(e["tags"][0], "TIER 2, RESULT of the pre-registration D-247")
        self.assertTrue(e["title"].startswith("EXACT CLIENT-PATH REPLAY"))
        self.assertIn("## not a heading: inside a code fence", e["body"])
        self.assertIn("10. DECISIONS FOR YOU.", e["body"])
        self.assertNotIn("D-247  [USER", e["body"])
        self.assertTrue(e["body"].startswith("## D-248"))
        self.assertEqual(e["tokens_est"], (e["chars"] + 3) // 4)

    def test_noble_format_keeps_subheadings(self):
        es = dec.parse(open(os.path.join(FIX, "decisions_noble.md"), encoding="utf-8").read())
        self.assertEqual([e["id"] for e in es], ["D-014", "D-013"])
        self.assertEqual(es[0]["date"], "2026-10-25")
        self.assertEqual(es[0]["time"], "")
        self.assertIn("### 2. Exact changed semantics", es[0]["body"])
        self.assertTrue(es[0]["title"].startswith("INCREMENT 11: ONE action-representation"))
        self.assertTrue(es[0]["tags"][0].startswith("status: DONE"))

    def test_heading_without_id_or_date(self):
        h = dec.parse_heading("Switch to uv for env management")
        self.assertEqual(h["id"], "")
        self.assertEqual(h["date"], "")
        self.assertEqual(h["title"], "Switch to uv for env management")

    def test_find_file_case_insensitive_and_upwards(self):
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "src", "pkg")
            os.makedirs(sub)
            with open(os.path.join(root, "decisions.md"), "w") as f:
                f.write("## D-1 x 2026-01-01\n")
            self.assertEqual(os.path.basename(dec.find_file(sub)), "decisions.md")
            self.assertIsNone(dec.find_file(sub, max_up=1))

    def test_cache_reparses_on_change(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "DECISIONS.md")
            with open(p, "w") as f:
                f.write("## D-1 first\n")
            c = dec.DecisionsCache()
            self.assertEqual(len(c.entries(p)), 1)
            time.sleep(0.01)
            with open(p, "w") as f:
                f.write("## D-2 second\nbody\n\n## D-1 first\n")
            s = c.summary(p, bodies=1)
            self.assertEqual(s["count"], 2)
            self.assertIn("body", s["entries"][0])
            self.assertNotIn("body", s["entries"][1])
            self.assertEqual(c.bodies(p, [1, 9]), {1: "## D-1 first\n"})


class MuxParseTests(unittest.TestCase):
    def test_parse_panes_and_target(self):
        vals = ["0", "@1", "0", "claude.exe", "%1", "0", "claude.exe.old.17", r"C:\Users\x\proj", "48116", "179", "53", "0", "1", "0"]
        out = _SEP.join(vals) + "\n" + "garbage line\n"
        ps = parse_panes(out)
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertEqual(p.target, "0:0.0")
        self.assertEqual((p.width, p.height, p.pid), (179, 53, 48116))
        self.assertFalse(p.attached)
        self.assertEqual(len(vals), len(_FIELDS))

    def test_safe_session_name(self):
        self.assertEqual(safe_session_name("my project.v2:x"), "my-project-v2-x")
        self.assertEqual(safe_session_name("..."), "claude")


def _pane(session="0", pane_id="%1", command="claude"):
    return Pane(session, "@1", 0, "w", pane_id, 0, command, "/tmp", 1, 80, 24, False, True, False)


class RegistryTests(unittest.TestCase):
    def test_registry_matches_session_and_pane(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sessions"))
            recs = [
                {"pid": 10, "sessionId": "old", "tmux": "0:@1.%1", "startedAt": 1000, "status": "idle", "cwd": "/a"},
                {"pid": 11, "sessionId": "new", "tmux": "0:@1.%1", "startedAt": 2000, "status": "busy", "cwd": "/a"},
                {"pid": 12, "sessionId": "other", "tmux": "work:@3.%1", "startedAt": 3000},   # psmux reuses %1
                {"pid": 13, "sessionId": "dead", "tmux": "0:@1.%1", "startedAt": 9000},
                {"pid": 14, "sessionId": "bg-job", "startedAt": 9000},                         # not in tmux
            ]
            for r in recs:
                with open(os.path.join(d, "sessions", f"{r['pid']}.json"), "w") as f:
                    json.dump(r, f)
            acct = cs.Account(dir=d)
            procs = cs.read_registry([acct], check_alive=lambda pid: pid != 13)
            self.assertEqual(sorted(p.session_id for p in procs), ["new", "old", "other"])
            hit = cs.match_proc(_pane("0", "%1"), procs)
            self.assertEqual(hit.session_id, "new")
            self.assertEqual(hit.account_dir, d)
            self.assertEqual(cs.match_proc(_pane("work", "%1"), procs).session_id, "other")
            self.assertIsNone(cs.match_proc(_pane("x", "%1"), procs))

    def test_pid_alive_self(self):
        self.assertTrue(cs.pid_alive(os.getpid()))
        self.assertFalse(cs.pid_alive(0))


class ScreenTests(unittest.TestCase):
    RULE = "─" * 60

    def test_waiting_on_permission_prompt(self):
        s = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\n"
        self.assertEqual(cs.screen_status(s), "waiting")

    def test_working_and_idle(self):
        self.assertEqual(cs.screen_status("✻ Brewing… (12s · esc to interrupt)\n" + self.RULE + "\n❯ \n"), "working")
        self.assertEqual(cs.screen_status("✻ Brewed for 2m 9s\n" + self.RULE + "\n❯ \n"), "idle")
        self.assertEqual(cs.screen_status("anything", registry_status="busy"), "working")

    def test_ansi_is_ignored(self):
        self.assertEqual(cs.screen_status("\x1b[0;38;5;246mesc to interrupt\x1b[0m"), "working")

    def test_preview_skips_input_box_and_hints(self):
        s = ("\x1b[0mDone, the tests pass.\x1b[0m\n\n✻ Brewed for 2m 9s · done 4:28 PM\n\n"
             "new task? /clear to save 116.5k tokens\n" + self.RULE + "\n❯ Try \"fix lint\"\n" + self.RULE +
             "\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")
        self.assertEqual(cs.last_text_line(s), "✻ Brewed for 2m 9s · done 4:28 PM")


class UsageTests(unittest.TestCase):
    def test_limits_list(self):
        raw = {"limits": [{"kind": "session", "percent": 3, "resets_at": "2026-10-25T14:00:00+00:00", "severity": "normal"},
                          {"kind": "weekly_all", "percent": 69, "resets_at": None}],
               "five_hour": {"utilization": 99}}
        out = cs.normalize_usage(raw)
        self.assertEqual([(l["label"], l["percent"]) for l in out], [("Session (5h)", 3), ("Weekly", 69)])

    def test_legacy_fields(self):
        raw = {"five_hour": {"utilization": 3.0, "resets_at": "x"}, "seven_day": {"utilization": 69.0}, "seven_day_opus": None}
        out = cs.normalize_usage(raw)
        self.assertEqual([(l["kind"], l["percent"]) for l in out], [("five_hour", 3.0), ("seven_day", 69.0)])

    def test_expired_token_is_not_fetched(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}, f)
            tr = cs.UsageTracker()
            acct = cs.Account(dir=d)
            tr.get(acct)
            for _ in range(50):
                time.sleep(0.02)
                if tr._cache(d).error:
                    break
            u = tr.get(acct)
            self.assertIn("expired", u["error"])
            self.assertEqual(u["limits"], [])

    def test_account_profile_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"emailAddress": "a@b.c", "accountUuid": "u1"}}, f)
            with open(os.path.join(d, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {"subscriptionType": "max"}}, f)
            a = cs.load_account(d)
            self.assertEqual((a.email, a.key, a.subscription, a.is_default), ("a@b.c", "u1", "max", False))
            self.assertEqual(a.env(), {"CLAUDE_CONFIG_DIR": a.dir})


if __name__ == "__main__":
    unittest.main()
