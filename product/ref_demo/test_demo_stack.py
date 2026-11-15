"""int-demo tests (claude-fable-5-2, Track P). Offline: wiring of the stack and the saved live evidence."""
import glob
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EV = sorted(d for d in glob.glob(os.path.join(HERE, "evidence", "live*")) if os.path.exists(os.path.join(d, "live.json")))


class Wiring(unittest.TestCase):
    def test_refuses_foreign_rt_and_job(self):
        for extra in (["--rt", "/dev/shm/vqa-cu/swarm/x"], ["--rt", "/dev/shm/vqa-cu/ref-int-demo-t", "--job", "hybrid"]):
            p = subprocess.run([sys.executable, os.path.join(HERE, "demo_live.py"), "--evidence", "/nonexistent"] + extra,
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("refused", p.stdout)

    def test_auction_fills_rewind_broker_seam_without_editing_broker(self):
        code = ("import sys; sys.path.insert(0, %r); import demo_live as D; AD = D.install_seamed_broker();"
                "sys.path.insert(0, D.PRODUCT + '/ref_rewind_demo'); import rewind_live, rewind_broker as RB;"
                "plain = RB.RewindBroker; pol = D.wire_auction(AD);"
                "assert RB.RewindBroker is not plain and issubclass(RB.RewindBroker, plain);"
                "assert hasattr(plain, '_pick_agent'); print(pol)" % HERE)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "vickrey_aging_v1")
        with open(os.path.join(HERE, "..", "ref_broker", "broker.py")) as f:
            self.assertNotIn("_pick_agent", f.read())   # the seam is applied in memory only


class Settle(unittest.TestCase):
    """The restore frame must be taken from a settled display (live flake: evidence/flake_live2, flake_live4)."""

    def looks(self, seq):
        import tempfile
        d = tempfile.mkdtemp()
        it = iter(seq)
        state = {"shown": None}

        def look():
            state["shown"] = next(it)
            p = os.path.join(d, "f.png")
            with open(p, "w") as f:
                f.write(state["shown"])
            return 0, {"frame": p}
        return look, state

    def run_settle(self, seq, **kw):
        sys.path.insert(0, HERE)
        import demo_live as D
        look, state = self.looks(seq)
        n = D.settle(look, sleep=lambda s: None, **kw)
        return n, state["shown"]

    def test_waits_past_the_stale_frame(self):
        n, shown = self.run_settle(["stale", "redrawing", "restored", "restored", "x"])
        self.assertEqual((n, shown), (4, "restored"))

    def test_negative_control_no_settle_keeps_the_stale_frame(self):
        n, shown = self.run_settle(["stale", "restored", "restored"], tries=0)
        self.assertEqual((n, shown), (0, None))      # nothing waited: the broker's own look would see "stale"

    def test_bounded_when_the_display_never_settles(self):
        n, _ = self.run_settle([str(i) for i in range(50)], tries=5)
        self.assertEqual(n, 5)

    def test_restore_capture_goes_through_settle(self):
        code = ("import sys; sys.path.insert(0, %r); import demo_live as D; AD = D.install_seamed_broker();"
                "sys.path.insert(0, D.PRODUCT + '/ref_rewind_demo'); import rewind_live, rewind_broker as RB;"
                "base = RB.RewindBroker; calls = []; base._capture = lambda self, e, r: calls.append(('cap', e));"
                "D.settle = lambda look, tries=8: calls.append(('settle', tries)) or 1; D.wire_settle(8);"
                "b = RB.RewindBroker.__new__(RB.RewindBroker); b.vision = type('V', (), {'look': None})();"
                "b._capture('ctl.restore.p1.e2', None); b._capture('m1.a1', None); print(calls)" % HERE)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "[('settle', 8), ('cap', 'ctl.restore.p1.e2'), ('cap', 'm1.a1')]")


@unittest.skipUnless(EV, "no live evidence saved")
class Evidence(unittest.TestCase):
    def setUp(self):
        self.d = EV[-1]
        with open(os.path.join(self.d, "events.jsonl")) as f:
            self.ev = [json.loads(x) for x in f if x.strip()]
        with open(os.path.join(self.d, "live.json")) as f:
            self.live = json.load(f)

    def test_live_run_green(self):
        self.assertTrue(self.live["ok"])
        self.assertEqual(self.live["red"], [])
        self.assertGreaterEqual(len(self.live["checks"]), 21)
        self.assertEqual(self.live["stages"]["scheduler"], "vickrey_aging_v1")

    def test_wrong_edit_rejected_and_rewound(self):
        rej = [r for r in self.ev if r.get("rec") == "judge" and r.get("pick") is False]
        rew = [r for r in self.ev if r.get("rec") == "control" and r.get("op") == "rewind"]
        self.assertTrue(rej, "no judge reject in the log")
        self.assertTrue(rew and all(r.get("digest_match") is True for r in rew), rew)

    def test_every_turn_was_ordered_by_an_auction_record(self):
        n = lambda k: sum(1 for r in self.ev if r.get("rec") == k)  # noqa: E731
        self.assertGreater(n("auction"), 0)
        self.assertEqual(n("auction"), n("turn"))
        self.assertEqual(n("auction_settle"), n("auction"))

    def test_audit_clean_on_saved_log(self):
        p = subprocess.run([sys.executable, os.path.join(HERE, "..", "ref_rewind_demo", "rewindctl.py"), "audit",
                            "--events", os.path.join(self.d, "events.jsonl")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout[-400:])

    def test_artifacts_present(self):
        for n in ("demo_composed.mp4", "frame_e1a1_wrong.png", "frame_restored_p1.png", "projection"):
            self.assertTrue(os.path.exists(os.path.join(self.d, n)), n)
        self.assertGreater(os.path.getsize(os.path.join(self.d, "demo_composed.mp4")), 100000)


if __name__ == "__main__":
    unittest.main()
