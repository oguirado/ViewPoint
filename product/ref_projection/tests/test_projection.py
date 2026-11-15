import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import project  # noqa: E402
import reconcile  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
FAKE_MCP = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]
AGENTS = ["camera-agent", "look-agent", "modeler-agent", "props-agent"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vp-f3-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.osb_log = os.path.join(self.tmp, "osb.log")
        os.environ["FAKE_OSB_LOG"] = self.osb_log
        self.ledger = project.Ledger(os.path.join(self.tmp, "ledger.jsonl"))
        self.client = project.McpStdioClient(FAKE_MCP)
        self.client.start()
        self.addCleanup(self.client.stop)
        self.osb = project.OsbSink("test-room", self.ledger, osb=os.path.join(HERE, "fake_osb.py"),
                                   python=(sys.executable,))
        self.tl = project.TaskLightSink("demo", self.ledger, self.client)

    def run_fixture(self, name, cls=project.Projector, finalize=True, **kw):
        proj = cls(self.osb, self.tl, **kw)
        project.pump(proj, os.path.join(FIX, name))
        if finalize:
            proj.finalize()
        return proj

    def osb_posts(self):
        return [r for r in self.ledger.rows if r.get("sink") == "osb"]

    def tl_calls(self, tool=None, agent=None):
        return [r for r in self.ledger.rows if r.get("sink") == "tasklight" and "tool" in r
                and (tool is None or r["tool"] == tool) and (agent is None or r["agent"] == agent)]


class TestMapping(Base):
    def test_complete_stream(self):
        proj = self.run_fixture("events_complete.jsonl", claims={"modeler-agent": ["col:CHAR_mesh"]})
        acts = [(r["agent"], r["act"], r["ref"]) for r in self.osb_posts()]
        count = lambda act: sum(1 for a in acts if a[1] == act)
        self.assertEqual(count("claim"), 5)       # 5 enqueue
        self.assertEqual(count("ack"), 7)         # 7 turn
        self.assertEqual(count("evidence"), 4)    # STATE_VERIFIED, VERIFIED_WITH_SPILL, VERIFIED x2
        self.assertEqual(count("refute"), 2)      # CLAIM_REFUSED, STALE_FRAME; PASS is not a refusal
        self.assertEqual(count("verdict"), 2)     # stop, resume
        self.assertIn(("props-agent", "refute", "props-agent-1"), acts)
        self.assertIn(("camera-agent", "refute", "camera-agent-1"), acts)
        self.assertIn(("director-agent", "verdict", None), acts)
        self.assertEqual(self.osb.errors + self.tl.errors, 0)
        self.assertEqual(len(proj.s["anomalies"]), 1)  # the unparseable line, skipped not fatal
        self.assertEqual(proj.s["ignored"], {"cursor": 1})

    def test_every_refute_has_ref(self):
        self.run_fixture("events_complete.jsonl")
        with open(self.osb_log) as f:
            argvs = [json.loads(l) for l in f]
        refutes = [a for a in argvs if "refute" in a]
        self.assertEqual(len(refutes), 2)
        for a in refutes:
            self.assertTrue(any(x.startswith("--ref=") and len(x) > 6 for x in a), a)

    def test_negative_control_refute_without_ref_is_refused(self):
        # Goes red if the guard in OsbSink.post is removed.
        with self.assertRaises(ValueError):
            self.osb.post("props-agent", "refute", "close CLAIM_REFUSED", ref=None)
        self.assertFalse(os.path.exists(self.osb_log))  # nothing reached the CLI

    def test_script_and_keys_never_leave(self):
        self.run_fixture("events_complete.jsonl")
        blob = json.dumps(self.ledger.rows)
        self.assertNotIn("SECRET_SCRIPT_BODY", blob)
        self.assertNotIn("KP_Decimal", blob)

    def test_tasklight_sequence(self):
        self.run_fixture("events_complete.jsonl", claims={"modeler-agent": ["col:CHAR_mesh"]})
        for a in AGENTS:
            calls = self.tl_calls(agent=a)
            self.assertEqual(calls[0]["tool"], "post_plan", a)       # plan on the first enqueue
            self.assertEqual(calls[-1]["tool"], "close_task", a)     # close at the end
            self.assertEqual(len(self.tl_calls("close_task", a)), 1)
        plans = self.tl_calls("post_plan", "modeler-agent")
        self.assertEqual([len(p["arguments"]["steps"]) for p in plans], [1, 2])  # appended, same title
        self.assertEqual(len({p["arguments"]["title"] for p in plans}), 1)
        self.assertIn("col:CHAR_mesh", plans[0]["arguments"]["title"])
        steps = lambda a: [(c["arguments"]["index"], c["arguments"]["state"]) for c in self.tl_calls("post_step", a)]
        self.assertEqual(steps("modeler-agent"), [(0, "active"), (0, "done"), (1, "active"), (1, "done")])
        self.assertEqual(steps("props-agent"), [(0, "active"), (0, "blocked")])
        self.assertEqual(steps("camera-agent"), [(0, "active"), (0, "pending"), (0, "active"), (0, "done")])
        st = [c["arguments"]["text"].split(":")[0] for c in self.tl_calls("post_status", "camera-agent")]
        for word in ("waiting", "acting", "blocked"):
            self.assertIn(word, st)
        self.assertTrue(all(c["ok"] for c in self.tl_calls()))

    def test_duplicate_event_does_not_touch_done_step(self):
        p = project.Projector(self.osb, self.tl)
        p.feed({"v": 1, "rec": "enqueue", "event_id": "look-agent-1", "agent_id": "look-agent", "lane": "script",
                "kind": "job", "based_on_frame": 1, "write_set": ["col:LOOK"], "expect": "x"})
        p.feed({"v": 1, "rec": "close", "tick": 1, "turn": 1, "event_id": "look-agent-1", "verdict": "VERIFIED"})
        p.feed({"v": 1, "rec": "close", "tick": 1, "turn": 2, "event_id": "look-agent-1", "verdict": "DUPLICATE_EVENT"})
        self.assertEqual(p.s["agents"]["look-agent"]["steps"][0]["state"], "done")
        self.assertEqual(self.osb_posts()[-1]["act"], "refute")
        self.assertEqual(self.osb_posts()[-1]["ref"], "look-agent-1")

    def test_three_drops_parks_and_blocks(self):
        p = project.Projector(self.osb, self.tl)
        p.feed({"v": 1, "rec": "enqueue", "event_id": "camera-agent-1", "agent_id": "camera-agent", "lane": "gui",
                "kind": "key", "based_on_frame": 1, "write_set": [], "keys": ["KP_1"], "expect": "x"})
        for d in (1, 2, 3):
            p.feed({"v": 1, "rec": "close", "turn": d, "event_id": "camera-agent-1", "verdict": "STALE_FRAME", "drops": d})
        self.assertEqual([c["arguments"]["state"] for c in self.tl_calls("post_step")], ["pending", "pending", "blocked"])

    def test_plan_never_exceeds_server_limits(self):
        p = project.Projector(self.osb, self.tl)
        for n in range(1, 15):
            p.feed({"v": 1, "rec": "enqueue", "event_id": "props-agent-%d" % n, "agent_id": "props-agent",
                    "lane": "script", "kind": "job", "based_on_frame": 1, "write_set": ["col:" + "P" * 200],
                    "expect": "e" * 500})
        self.assertEqual(self.tl.errors, 0)  # the fake server rejects > 12 steps and long texts
        self.assertEqual(len(p.s["agents"]["props-agent"]["steps"]), 12)
        self.assertEqual(len(p.s["agents"]["props-agent"]["overflow"]), 2)

    def test_bad_ids_are_anomalies_not_posts(self):
        p = project.Projector(self.osb, self.tl)
        p.feed({"v": 1, "rec": "enqueue", "event_id": "--room=other", "agent_id": "look-agent"})
        p.feed({"v": 1, "rec": "enqueue", "event_id": "x-1", "agent_id": "Bad Agent; rm"})
        p.feed({"v": 2, "rec": "enqueue"})
        self.assertEqual(len(p.s["anomalies"]), 3)
        self.assertEqual(self.ledger.rows, [])


class TestTailAndClient(Base):
    def test_partial_line_waits_and_offset_resumes(self):
        path = os.path.join(self.tmp, "events.jsonl")
        with open(os.path.join(FIX, "events_complete.jsonl")) as f:
            lines = f.read().splitlines(True)
        with open(path, "w") as f:
            f.write("".join(lines[:3]) + lines[3][:20])  # torn write of the 4th record
        p = project.Projector(self.osb, self.tl)
        project.pump(p, path)
        self.assertEqual(p.s["line"], 3)
        with open(path, "a") as f:
            f.write(lines[3][20:] + "".join(lines[4:]))
        state = json.loads(json.dumps(p.s))  # restart from saved state: no record replayed
        p2 = project.Projector(self.osb, self.tl, state=state)
        project.pump(p2, path)
        p2.finalize()
        self.assertEqual(sum(1 for r in self.osb_posts() if r["act"] == "claim"), 5)
        self.assertEqual(len(self.tl_calls("close_task")), 4)

    def test_mcp_client_handshake_and_schema_guard(self):
        self.assertEqual(sorted(self.client.tools), ["close_task", "post_plan", "post_status", "post_step"])
        with self.assertRaises(project.McpError):
            self.client.call("post_step", {"room": "demo", "agent": "x"})  # missing required
        with self.assertRaises(project.McpError):
            self.client.call("no_such_tool", {})
        ok, _ = self.client.call("post_step", {"room": "demo", "agent": "x", "index": 0, "state": "bogus"})
        self.assertFalse(ok)  # isError from the server is surfaced
        self.assertFalse(self.tl.call("close_task", "x", outcome="o" * 300))
        self.assertEqual(self.tl.errors, 1)


class TestFollowMode(unittest.TestCase):
    def test_follow_tails_appends_and_finalizes_on_sigterm(self):
        import signal, subprocess, time
        tmp = tempfile.mkdtemp(prefix="vp-f3-follow-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ev = os.path.join(tmp, "events.jsonl")
        with open(os.path.join(FIX, "events_complete.jsonl")) as f:
            lines = f.read().splitlines(True)
        open(ev, "w").close()
        env = dict(os.environ, VP_PROJ_MCP_CMD=json.dumps(FAKE_MCP), VP_PROJ_OSB=os.path.join(HERE, "fake_osb.py"),
                   FAKE_OSB_LOG=os.path.join(tmp, "osb.log"))
        p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HERE), "project.py"), "--rt", tmp,
                              "--poll", "0.05"], env=env, stdout=subprocess.PIPE, text=True)
        try:
            for chunk in (lines[:8], lines[8:15], lines[15:]):
                with open(ev, "a") as f:
                    f.write("".join(chunk))
                time.sleep(0.3)
            state = os.path.join(tmp, "projection", "state.json")
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    if json.load(open(state))["line"] == len(lines):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            p.send_signal(signal.SIGTERM)  # this PID only
            out, _ = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, out)
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(summary["lines"], len(lines))
        self.assertTrue(all(a["closed"] for a in summary["agents"].values()))
        self.assertEqual(len(summary["agents"]), 4)


class TestReconciler(Base):
    def flags(self, fixture):
        self.run_fixture(fixture)
        return reconcile.reconcile(reconcile.load_jsonl(os.path.join(FIX, fixture)),
                                   reconcile.load_jsonl(self.ledger.path), room="demo")

    def test_clean_when_all_closed(self):
        self.assertEqual(self.flags("events_complete.jsonl"), [])

    def test_negative_control_missing_close_is_flagged(self):
        # camera-agent-1 was re-queued and never closed: the projector must not close
        # the card and the reconciler must flag exactly that agent.
        flags = self.flags("events_missing_close.jsonl")
        self.assertEqual([(f["agent"], f["code"]) for f in flags], [("camera-agent", "MISSING_CLOSE_TASK")])
        self.assertIn("[0]", flags[0]["detail"])

    def test_unacknowledged_close_does_not_count(self):
        ev = [{"rec": "enqueue", "agent_id": "look-agent"}, {"rec": "enqueue", "agent_id": "props-agent"}]
        led = [{"sink": "tasklight", "tool": "post_plan", "agent": "look-agent", "ok": True, "arguments": {"steps": [{}]}},
               {"sink": "tasklight", "tool": "close_task", "agent": "look-agent", "ok": False, "arguments": {}}]
        self.assertEqual([(f["agent"], f["code"]) for f in reconcile.reconcile(ev, led)],
                         [("look-agent", "MISSING_CLOSE_TASK"), ("props-agent", "NO_PLAN")])

    def test_cli_exit_codes(self):
        self.run_fixture("events_missing_close.jsonl")
        rc = reconcile.main(["--events", os.path.join(FIX, "events_missing_close.jsonl"), "--ledger", self.ledger.path])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
