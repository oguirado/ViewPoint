"""Hardening tests (w186 cell, lane sw-overlay-projection). Each group has a
negative control that restores the old behaviour and must go red."""
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
HANG_MCP = [sys.executable, os.path.join(HERE, "fake_mcp_hang.py")]
COMPLETE = os.path.join(FIX, "events_complete.jsonl")


class Crash(BaseException):
    """Stands for kill -9: nothing may catch it."""


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vp-f3-hard-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.room = os.path.join(self.tmp, "room.jsonl")
        os.environ["FAKE_OSB_ROOM"] = self.room
        os.environ["FAKE_OSB_PLAN"] = "[]"
        os.environ["FAKE_MCP_COUNT"] = os.path.join(self.tmp, "mcp.n")
        os.environ["FAKE_MCP_HANG"] = "[]"
        self.ledger_path = os.path.join(self.tmp, "ledger.jsonl")
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.stop()

    def sinks(self, mcp=FAKE_MCP, timeout=20, **osb_kw):
        ledger = project.Ledger(self.ledger_path)
        client = project.McpStdioClient(mcp, timeout=timeout)
        self.clients.append(client)
        osb = project.OsbSink("test-room", ledger, osb=os.path.join(HERE, "fake_osb_flaky.py"),
                              python=(sys.executable,), backoff=0, **osb_kw)
        return ledger, client, osb, project.TaskLightSink("demo", ledger, client)

    def room_posts(self):
        if not os.path.exists(self.room):
            return []
        with open(self.room) as f:
            return [json.loads(l) for l in f]

    def ledger_rows(self):
        with open(self.ledger_path) as f:
            return [json.loads(l) for l in f]


class TestRestart(Tmp):
    """Crash in the middle of a record, restart from the saved state."""

    def clean_run(self):
        d = tempfile.mkdtemp(prefix="vp-f3-clean-")
        self.addCleanup(shutil.rmtree, d, True)
        os.environ["FAKE_OSB_ROOM"] = os.path.join(d, "room.jsonl")
        ledger = project.Ledger(os.path.join(d, "ledger.jsonl"))
        client = project.McpStdioClient(FAKE_MCP)
        self.clients.append(client)
        osb = project.OsbSink("test-room", ledger, osb=os.path.join(HERE, "fake_osb_flaky.py"),
                              python=(sys.executable,))
        proj = project.Projector(osb, project.TaskLightSink("demo", ledger, client))
        project.pump(proj, COMPLETE)
        proj.finalize()
        os.environ["FAKE_OSB_ROOM"] = self.room
        with open(os.path.join(d, "room.jsonl")) as f:
            posts = [json.loads(l) for l in f]
        calls = [r for r in ledger.rows if r.get("sink") == "tasklight" and r.get("ok")]
        return posts, calls, proj.s

    def crashed_then_resumed(self, crash_at, forget_ledger=False):
        state_path = os.path.join(self.tmp, "state.json")
        ledger, client, osb, tl = self.sinks()
        proj = project.Projector(osb, tl)
        real, n = tl.call, [0]

        def dying(tool, agent, key=None, **args):
            n[0] += 1
            if n[0] == crash_at:
                raise Crash()
            return real(tool, agent, key=key, **args)
        tl.call = dying
        with self.assertRaises(Crash):
            project.pump(proj, COMPLETE, checkpoint=lambda: project.save_state(state_path, proj.s))
        client.stop()
        # --- new process: everything comes from disk
        state = None  # a crash inside the very first record leaves no state at all
        if os.path.exists(state_path):
            with open(state_path) as f:
                state = json.load(f)
            self.assertLess(state["line"], 20)
        ledger2, client2, osb2, tl2 = self.sinks()
        if forget_ledger:
            ledger2.acked.clear()
        proj2 = project.Projector(osb2, tl2, state=state)
        project.pump(proj2, COMPLETE, checkpoint=lambda: project.save_state(state_path, proj2.s))
        proj2.finalize()
        return proj2, osb2, tl2

    def test_no_duplicate_posts_after_a_crash_mid_record(self):
        want_posts, want_calls, want_state = self.clean_run()
        for crash_at in (2, 7, 15):
            with self.subTest(crash_at=crash_at):
                for f in (self.room, self.room + ".n", self.ledger_path,
                          os.path.join(self.tmp, "state.json")):
                    if os.path.exists(f):
                        os.unlink(f)
                proj, osb, tl = self.crashed_then_resumed(crash_at)
                key = lambda p: (p["agent"], p["act"], p["text"], p["ref"])
                self.assertEqual([key(p) for p in self.room_posts()], [key(p) for p in want_posts])
                got = [(r["tool"], r["agent"], r["arguments"]) for r in self.ledger_rows()
                       if r.get("sink") == "tasklight" and r.get("ok")]
                self.assertEqual(got, [(r["tool"], r["agent"], r["arguments"]) for r in want_calls])
                self.assertGreater(osb.replayed, 0)  # the redo really met delivered calls
                self.assertEqual(proj.s["agents"], want_state["agents"])
                flags = reconcile.reconcile(reconcile.load_jsonl(COMPLETE),
                                            reconcile.load_jsonl(self.ledger_path))
                self.assertEqual(flags, [])

    def test_negative_control_without_ledger_keys_the_redo_posts_twice(self):
        want_posts, _, _ = self.clean_run()
        self.crashed_then_resumed(7, forget_ledger=True)
        self.assertGreater(len(self.room_posts()), len(want_posts))
        flags = reconcile.reconcile(reconcile.load_jsonl(COMPLETE),
                                    reconcile.load_jsonl(self.ledger_path))
        self.assertIn("OSB_DUPLICATE_POST", [f["code"] for f in flags])  # and the reconciler sees it

    def test_state_is_saved_after_every_record(self):
        ledger, client, osb, tl = self.sinks()
        proj, seen = project.Projector(osb, tl), []
        project.pump(proj, COMPLETE, checkpoint=lambda: seen.append((proj.s["line"], proj.s["offset"])))
        with open(COMPLETE, "rb") as f:
            data = f.read()
        self.assertEqual(seen[-1][1], len(data))
        self.assertEqual([o for _, o in seen], sorted({o for _, o in seen}))
        for _, off in seen:
            self.assertEqual(data[off - 1:off], b"\n")  # every checkpoint is a line boundary


class TestOsbRetry(Tmp):
    def post(self, plan, **kw):
        os.environ["FAKE_OSB_PLAN"] = json.dumps(plan)
        ledger, _, osb, _ = self.sinks(**kw)
        ok = osb.post("modeler-agent", "claim", "enqueue modeler-agent-1: x", ref="modeler-agent-1", key="1.1")
        return ok, osb, ledger

    def test_transient_failure_is_retried_once_delivered_once(self):
        ok, osb, ledger = self.post(["fail", "fail", "ok"])
        self.assertTrue(ok)
        self.assertEqual((osb.errors, osb.retries, len(self.room_posts())), (0, 2, 1))
        self.assertEqual(ledger.rows[-1]["attempts"], 3)

    def test_permanent_failure_is_bounded_and_counted(self):
        ok, osb, ledger = self.post(["fail"] * 10)
        self.assertFalse(ok)
        self.assertEqual((osb.errors, ledger.rows[-1]["attempts"], len(self.room_posts())), (1, 3, 0))
        self.assertFalse(ledger.is_acked("osb", "test-room", "1.1"))  # a later redo will try again

    def test_post_that_landed_but_reported_failure_is_not_posted_again(self):
        ok, osb, ledger = self.post(["land_fail", "ok"])
        self.assertTrue(ok)
        self.assertEqual(len(self.room_posts()), 1)
        self.assertIn("dedup", ledger.rows[-1])

    def test_negative_control_blind_retry_duplicates(self):
        ok, _, _ = self.post(["land_fail", "ok"], verify_before_retry=False)
        self.assertTrue(ok)
        self.assertEqual(len(self.room_posts()), 2)

    def test_same_text_from_an_older_ack_is_not_taken_for_this_post(self):
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["ok", "fail", "ok"])
        ledger, _, osb, _ = self.sinks()
        self.assertTrue(osb.post("director-agent", "verdict", "stop: hold", key="1.1"))
        self.assertTrue(osb.post("director-agent", "verdict", "stop: hold", key="9.1"))
        self.assertEqual(len(self.room_posts()), 2)  # the second stop is a new fact


class TestMcpRecovery(Tmp):
    def test_timeout_restarts_the_server_and_retries_state_setting_tools(self):
        os.environ["FAKE_MCP_HANG"] = "[2]"
        ledger, client, _, tl = self.sinks(mcp=HANG_MCP, timeout=0.6)
        self.assertTrue(tl.call("post_plan", "look-agent", title="t", steps=[{"text": "s"}]))
        self.assertTrue(tl.call("post_step", "look-agent", index=0, state="done", outcome="o"))
        self.assertEqual((tl.errors, tl.retries, client.starts), (0, 1, 2))
        self.assertEqual(ledger.rows[-1]["attempts"], 2)
        # the answer accepted for the retry is the retry's own (call 3), not the late call 2
        self.assertEqual(json.loads(ledger.rows[-1]["response"]["content"][0]["text"])["n"], 3)

    def test_status_is_not_retried_and_the_next_call_gets_its_own_answer(self):
        os.environ["FAKE_MCP_HANG"] = "[1]"
        ledger, client, _, tl = self.sinks(mcp=HANG_MCP, timeout=0.6)
        self.assertFalse(tl.call("post_status", "look-agent", text="waiting: x"))
        self.assertEqual((tl.errors, tl.retries), (1, 0))
        self.assertTrue(tl.call("post_status", "camera-agent", text="waiting: y"))
        got = json.loads(ledger.rows[-1]["response"]["content"][0]["text"])
        self.assertEqual(got, {"n": 2, "agent": "camera-agent"})
        with open(os.environ["FAKE_MCP_COUNT"] + ".calls") as f:
            self.assertEqual(len(f.readlines()), 2)  # exactly one delivery attempt each

    def test_negative_control_no_retry_set_loses_the_step(self):
        os.environ["FAKE_MCP_HANG"] = "[1]"
        ledger, client, _, _ = self.sinks(mcp=HANG_MCP, timeout=0.6)
        tl = project.TaskLightSink("demo", ledger, client, retry_safe=())
        self.assertFalse(tl.call("post_step", "look-agent", index=0, state="done", outcome="o"))
        self.assertEqual(tl.errors, 1)

    def test_server_that_cannot_start_is_an_error_row_not_a_crash(self):
        ledger, client, _, tl = self.sinks(mcp=["/nonexistent/mcp-server"])
        self.assertFalse(tl.call("post_step", "look-agent", index=0, state="done"))
        self.assertTrue(ledger.rows[-1]["response"]["transport"])


class TestHostileRecords(Tmp):
    def feed(self, recs):
        ledger, _, osb, tl = self.sinks()
        proj = project.Projector(osb, tl)
        for r in recs:
            proj.feed(r)
        return proj, ledger

    def test_control_characters_surrogates_and_wrong_types_do_not_crash(self):
        enq = {"v": 1, "rec": "enqueue", "event_id": "look-agent-1", "agent_id": "look-agent",
               "lane": ["x"], "kind": {"k": 1}, "write_set": 5, "based_on_frame": float("nan"),
               "expect": "red\x00cube \ud800 ‮gnp.exe \x1b[31m"}
        close = {"v": 1, "rec": "close", "event_id": "look-agent-1", "verdict": "STALE_FRAME",
                 "drops": "abc", "spill_regions": 7, "turn": {"a": 1}, "tick": float("inf")}
        close2 = dict(close, drops=float("nan"), verdict="VERIFIED")
        proj, ledger = self.feed([enq, {"v": 1, "rec": "turn", "agent_id": "look-agent", "turn": "\x00"},
                                  close, close2, {"v": 1, "rec": ["x"]}, {"v": 1, "rec": "control", "op": {"o": 1}}])
        posts = self.room_posts()
        self.assertEqual([p["act"] for p in posts], ["claim", "ack", "refute", "evidence"])
        for p in posts:
            self.assertTrue(p["text"].isprintable(), p["text"])
            p["text"].encode("utf-8")
        self.assertIn("red cube ? gnp.exe [31m", posts[0]["text"])
        self.assertEqual(proj.osb.errors + proj.tl.errors, 0)
        json.dumps(proj.s)  # the state must stay serialisable

    def test_repeated_close_does_not_post_evidence_twice_or_reopen(self):
        enq = {"v": 1, "rec": "enqueue", "event_id": "look-agent-1", "agent_id": "look-agent"}
        ok = {"v": 1, "rec": "close", "event_id": "look-agent-1", "verdict": "VERIFIED", "turn": 1}
        late = {"v": 1, "rec": "close", "event_id": "look-agent-1", "verdict": "STALE_FRAME", "turn": 2}
        proj, _ = self.feed([enq, ok, ok, late])
        self.assertEqual([p["act"] for p in self.room_posts()], ["claim", "evidence"])
        self.assertEqual(proj.s["agents"]["look-agent"]["steps"][0]["state"], "done")
        self.assertEqual(proj.s["anomaly_count"], 2)

    def test_anomaly_list_is_bounded(self):
        proj, _ = self.feed([{"v": 2}] * (project.MAX_ANOMALIES + 50))
        self.assertEqual(len(proj.s["anomalies"]), project.MAX_ANOMALIES)
        self.assertEqual(proj.s["anomaly_count"], project.MAX_ANOMALIES + 50)


class TestEventsIdentity(Tmp):
    def projected(self):
        self.events = os.path.join(self.tmp, "events.jsonl")
        shutil.copy(COMPLETE, self.events)
        ledger, _, osb, tl = self.sinks()
        proj = project.Projector(osb, tl)
        project.pump(proj, self.events)
        return proj

    def other_file(self, size):
        line = json.dumps({"v": 1, "rec": "enqueue", "event_id": "props-agent-77", "agent_id": "props-agent"})
        with open(self.events, "w") as f:
            f.write(line + "\n")
            f.write(" " * (size - len(line) - 2) + "\n")
            f.write(line.replace("77", "78") + "\n")

    def test_truncated_file_is_refused(self):
        proj = self.projected()
        with open(self.events, "w") as f:
            f.write("")
        with self.assertRaises(project.EventsFileChanged):
            project.pump(proj, self.events)

    def test_replaced_file_of_the_same_size_is_refused(self):
        proj = self.projected()
        n = len(self.room_posts())
        self.other_file(proj.s["offset"])
        with self.assertRaises(project.EventsFileChanged):
            project.pump(proj, self.events)
        self.assertEqual(len(self.room_posts()), n)

    def test_negative_control_without_the_head_hash_the_new_file_is_read_mid_way(self):
        proj = self.projected()
        n = len(self.room_posts())
        self.other_file(proj.s["offset"])
        proj.s["head"] = None
        project.pump(proj, self.events)  # no refusal: props-agent-78 is projected from the middle
        self.assertEqual(len(self.room_posts()), n + 1)

    def test_append_is_still_accepted(self):
        proj = self.projected()
        with open(self.events, "a") as f:
            f.write(json.dumps({"v": 1, "rec": "enqueue", "event_id": "props-agent-9",
                                "agent_id": "props-agent"}) + "\n")
        self.assertEqual(project.pump(proj, self.events), 1)

    def test_cli_exits_3_on_a_changed_file(self):
        import subprocess
        rt = os.path.join(self.tmp, "rt")
        os.makedirs(rt)
        ev = os.path.join(rt, "events.jsonl")
        shutil.copy(COMPLETE, ev)
        env = dict(os.environ, VP_PROJ_MCP_CMD=json.dumps(FAKE_MCP),
                   VP_PROJ_OSB=os.path.join(HERE, "fake_osb_flaky.py"))
        cmd = [sys.executable, os.path.join(os.path.dirname(HERE), "project.py"), "--rt", rt, "--once",
               "--osb-room", "test-room"]
        # project.py runs osb with "python3 -I": same interpreter family, fine for the double
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        first = json.loads(p.stdout)
        self.assertEqual((first["replayed"], first["partial_tail_bytes"]), (0, 0))
        n = len(self.room_posts())
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)  # same file again: nothing new
        self.assertEqual(len(self.room_posts()), n)
        with open(ev, "w") as f:
            f.write('{"v":1}\n')
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("error", json.loads(p.stdout))
        self.assertEqual(len(self.room_posts()), n)


class TestReconciler(unittest.TestCase):
    ENQ = [{"v": 1, "rec": "enqueue", "agent_id": "a-agent", "event_id": "a-agent-1"}]

    def tl(self, tool, **args):
        return {"sink": "tasklight", "room": "demo", "agent": "a-agent", "tool": tool, "ok": True,
                "arguments": args}

    def osb(self, act="claim", ref="a-agent-1", rc=0):
        return {"sink": "osb", "room": "r", "agent": "a-agent", "act": act, "ref": ref, "rc": rc,
                "argv": ["post", "--text=x"]}

    def codes(self, events, ledger):
        return [f["code"] for f in reconcile.reconcile(events, ledger)]

    def good(self):
        return [self.osb(), self.tl("post_plan", steps=[{"text": "s"}]),
                self.tl("post_step", index=0, state="done"), self.tl("close_task", outcome="o")]

    def test_clean_baseline(self):
        self.assertEqual(self.codes(self.ENQ, self.good()), [])

    def test_close_with_a_step_never_acknowledged_terminal(self):
        led = self.good()
        led[2]["ok"] = False  # the server refused the step, the card was closed anyway
        self.assertEqual(self.codes(self.ENQ, led), ["CLOSED_WITH_OPEN_STEPS"])

    def test_event_missing_from_the_plan(self):
        enq = self.ENQ + [dict(self.ENQ[0], event_id="a-agent-2")]
        led = self.good() + [self.osb(ref="a-agent-2")]
        self.assertEqual(self.codes(enq, led), ["PLAN_MISSING_EVENTS"])

    def test_duplicate_and_missing_osb_posts(self):
        self.assertEqual(self.codes(self.ENQ, self.good() + [self.osb()]), ["OSB_DUPLICATE_POST"])
        led = self.good()
        led[0]["rc"] = 1
        self.assertEqual(self.codes(self.ENQ, led), ["OSB_MISSING_CLAIM"])
        # a failed attempt followed by the acknowledged one is not a duplicate
        self.assertEqual(self.codes(self.ENQ, [self.osb(rc=1)] + self.good()), [])

    def test_unparseable_enqueue_line_is_flagged_other_garbage_is_counted(self):
        d = tempfile.mkdtemp(prefix="vp-f3-rec-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "events.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps(self.ENQ[0]) + "\n")
            f.write("garbage\n")
            f.write('{"v":1,"rec":"enqueue","agent_id":"b-agent","event_id":"b-agent-1"\n')  # torn
        rows = reconcile.load_jsonl(p)
        self.assertEqual((rows.bad, rows.bad_enqueue), (2, 1))
        self.assertEqual(self.codes(rows, self.good()), ["UNPARSEABLE_ENQUEUE"])


if __name__ == "__main__":
    unittest.main()
