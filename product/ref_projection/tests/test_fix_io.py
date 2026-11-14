"""fix-io lane (xr-io findings P1, P3, P5). Each test was red before its fix."""
import contextlib
import io
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

FAKE_MCP = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]
FAKE_OSB = os.path.join(HERE, "fake_osb_flaky.py")
ENQ = {"v": 1, "rec": "enqueue", "event_id": "modeler-agent-1", "agent_id": "modeler-agent",
       "lane": "script", "kind": "job", "based_on_frame": 1, "write_set": ["col:CHAR_mesh"],
       "expect": "ok", "ts_ms": 1000}
DEEP = "[" * 200000 + "]" * 200000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vp-f3-fixio-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.room = os.path.join(self.tmp, "room.jsonl")
        self.events = os.path.join(self.tmp, "events.jsonl")
        self.out = os.path.join(self.tmp, "projection")
        env = {"FAKE_OSB_ROOM": self.room, "FAKE_OSB_PLAN": "[]",
               "FAKE_MCP_COUNT": os.path.join(self.tmp, "mcp.n"), "FAKE_MCP_HANG": "[]",
               "VP_PROJ_OSB": FAKE_OSB, "VP_PROJ_MCP_CMD": json.dumps(FAKE_MCP)}
        for k, v in env.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.pop(k, None) if old is None
                            else os.environ.__setitem__(k, old))

    def write_events(self, lines):
        with open(self.events, "w") as f:
            for l in lines:
                f.write((l if isinstance(l, str) else json.dumps(l)) + "\n")

    def run_main(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = project.main(["--rt", self.tmp, "--events", self.events, "--out", self.out, "--once"])
        return rc, json.loads(buf.getvalue().strip().splitlines()[-1])

    def room_rows(self):
        if not os.path.exists(self.room):
            return []
        with open(self.room) as f:
            return [json.loads(l) for l in f]


class TestP1PoisonLine(Base):
    def test_deep_nesting_is_an_anomaly_and_the_next_record_is_projected(self):
        self.write_events([DEEP, ENQ])
        rc, summary = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(summary["anomalies"], 1)
        self.assertEqual([r["act"] for r in self.room_rows()], ["claim"])
        with open(os.path.join(self.out, "state.json")) as f:
            self.assertEqual(json.load(f)["offset"], os.path.getsize(self.events))

    def test_oversized_line_is_skipped_without_parsing(self):
        big = json.dumps({"v": 1, "rec": "enqueue", "pad": "x" * (project.MAX_LINE_BYTES + 1)})
        self.write_events([big, ENQ])
        rc, summary = self.run_main()
        self.assertEqual((rc, summary["anomalies"]), (0, 1))
        self.assertEqual(len(self.room_rows()), 1)

    def test_reconcile_counts_a_deep_line_as_bad(self):
        self.write_events([DEEP, ENQ])
        rows = reconcile.load_jsonl(self.events)
        self.assertEqual((len(rows), rows.bad), (1, 1))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = reconcile.main(["--events", self.events, "--ledger", self.events])
        self.assertIn(rc, (0, 1))  # a verdict, not a traceback


class TestP3LostCall(Base):
    def test_failed_osb_post_is_retried_on_restart(self):
        self.write_events([ENQ])
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["fail"] * project.OSB_ATTEMPTS)
        rc1, s1 = self.run_main()
        self.assertEqual((rc1, s1["osb_errors"], s1["unacked"]), (1, 1, 1))
        self.assertEqual(self.room_rows(), [])
        rc2, s2 = self.run_main()  # the plan is exhausted: the daemon is back
        claims = [r for r in self.room_rows() if r["act"] == "claim"]
        self.assertEqual(len(claims), 1, "the lost claim must reach the room on restart")
        self.assertEqual(claims[0]["ref"], "modeler-agent-1")
        self.assertEqual((rc2, s2["unacked"], s2["outbox_retried"]), (0, 0, 1))
        ledger = reconcile.load_jsonl(os.path.join(self.out, "ledger.jsonl"))
        codes = [f["code"] for f in reconcile.reconcile(reconcile.load_jsonl(self.events), ledger)]
        self.assertNotIn("UNACKED_CALL", codes)
        rc3, s3 = self.run_main()  # and it is not posted twice
        self.assertEqual((rc3, len(self.room_rows()), s3["outbox_retried"]), (0, 1, 0))

    def test_still_failing_call_keeps_rc_1_on_every_restart(self):
        self.write_events([ENQ])
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["fail"] * (project.OSB_ATTEMPTS * 2))
        self.assertEqual(self.run_main()[0], 1)
        rc2, s2 = self.run_main()
        self.assertEqual((rc2, s2["unacked"]), (1, 1))  # never "osb_errors 0, rc 0" with the act missing
        ledger = reconcile.load_jsonl(os.path.join(self.out, "ledger.jsonl"))
        codes = [f["code"] for f in reconcile.reconcile(reconcile.load_jsonl(self.events), ledger)]
        self.assertIn("UNACKED_CALL", codes)  # the reconciler sees the lost act

    def test_failed_tasklight_call_is_retried_and_stale_plans_are_superseded(self):
        ledger_path = os.path.join(self.tmp, "l.jsonl")

        class Flaky:
            def __init__(self, fail):
                self.fail, self.calls = fail, []
            def alive(self):
                return True
            def call(self, tool, arguments):
                self.calls.append((tool, arguments))
                return (not self.fail), {}

        led = project.Ledger(ledger_path)
        tl = project.TaskLightSink("demo", led, Flaky(True))
        tl.call("post_plan", "a-agent", key="1.1", title="t", steps=[{"text": "old"}])
        tl.call("post_step", "a-agent", key="2.1", index=0, state="done")
        tl.client.fail = False
        tl.call("post_plan", "a-agent", key="3.1", title="t", steps=[{"text": "new"}])
        led2 = project.Ledger(ledger_path)
        self.assertEqual([k[2] for k in led2.pending], ["2.1"])  # plan 1.1 superseded by 3.1
        good = Flaky(False)
        osb = project.OsbSink("test-room", led2, osb=FAKE_OSB, python=(sys.executable,))
        n = project.retry_outbox(led2, osb, project.TaskLightSink("demo", led2, good))
        self.assertEqual([c[0] for c in good.calls], ["post_step"])  # the old plan is not replayed
        self.assertEqual(good.calls[0][1], {"room": "demo", "agent": "a-agent", "index": 0, "state": "done"})
        self.assertEqual(n, 1)
        self.assertEqual(project.Ledger(ledger_path).pending, {})


class TestP2FalseDedup(Base):
    def sink(self, name="l.jsonl"):
        led = project.Ledger(os.path.join(self.tmp, name))
        return led, project.OsbSink("test-room", led, osb=FAKE_OSB, python=(sys.executable,), backoff=0)

    def test_identical_text_from_an_earlier_run_does_not_count_as_delivered(self):
        _, old = self.sink("old.jsonl")
        self.assertTrue(old.post("a-agent", "ack", "turn 1 closed OK", key="5.1"))  # the earlier run
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["ok", "fail", "ok"])  # entry 0 was the old post
        led, new = self.sink()
        self.assertTrue(new.post("a-agent", "ack", "turn 1 closed OK", key="5.1"))
        self.assertEqual(len(self.room_rows()), 2, "the new act must really be posted")
        self.assertNotIn("dedup", led.rows[-1])

    def test_identical_text_twice_in_one_run_is_posted_twice(self):
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["ok", "fail", "ok"])
        led, s = self.sink()
        self.assertTrue(s.post("a-agent", "ack", "same", key="1.1"))
        self.assertTrue(s.post("a-agent", "ack", "same", key="2.1"))
        self.assertEqual(len(self.room_rows()), 2)

    def test_landed_but_failed_is_still_not_posted_twice(self):
        _, old = self.sink("old.jsonl")
        old.post("a-agent", "ack", "same", key="1.1")
        os.environ["FAKE_OSB_PLAN"] = json.dumps(["ok", "land_fail", "ok"])
        led, s = self.sink()
        self.assertTrue(s.post("a-agent", "ack", "same", key="1.1"))
        self.assertEqual(len(self.room_rows()), 2)
        self.assertIn("dedup", led.rows[-1])
        self.assertTrue(s.post("a-agent", "ack", "same", key="2.1"))  # a later twin is a new act
        self.assertEqual(len(self.room_rows()), 3)


class TestP5Claims(Base):
    def test_non_string_claim_elements_do_not_kill_the_run(self):
        with open(os.path.join(self.tmp, "claims.json"), "w") as f:
            json.dump({"modeler-agent": [{"x": 1}, 7, "col:CHAR_mesh", "not a claim"], "bad": "str"}, f)
        self.write_events([ENQ])
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        with open(os.path.join(self.out, "state.json")) as f:
            self.assertEqual(json.load(f)["agents"]["modeler-agent"]["title"],
                             "[ViewPoint capsule] modeler-agent on col:CHAR_mesh")


if __name__ == "__main__":
    unittest.main()
