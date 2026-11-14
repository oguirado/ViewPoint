import contextlib, copy, io, json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WL = os.path.dirname(HERE)
sys.path.insert(0, WL)
import labels_v2 as L2
import labels_v3 as L
import recorder as R

MAN3 = os.path.join(WL, "prereg_wl_v3.json")
JIT = 700  # measured actuator offset over the plan, us


def act_rows(m, iid, jitter=JIT):
    it = L2._item(m, iid)
    return [{"schema": L2.ACT_SCHEMA, "item": iid, "actuator_seq": i + 1, "step": s, "point": p, "op": op,
             "t_plan_us": t, "t_us": t + jitter + i, "rc": 0} for i, (s, p, op, t) in enumerate(L.plan_events(it))]


class T(unittest.TestCase):
    def setUp(self):
        self.m = L.load_manifest(MAN3)

    def test_v3_labels_from_act(self):
        for iid in ("WL-1s", "WL-1c", "WL-1L", "WL-1h", "WL-4", "WL-3"):
            rows = act_rows(self.m, iid)
            got = L.labels_from_act(self.m, iid, 1920, 1080, rows)
            self.assertGreater(len(got), 0, iid)
            self.assertEqual(got, L.labels_from_act(self.m, iid, 1920, 1080, copy.deepcopy(rows)))
        # labels take actuator time, not plan time
        got = L.labels_from_act(self.m, "WL-1h", 1920, 1080, act_rows(self.m, "WL-1h"))
        self.assertEqual(got[0]["t_us"], 1000000 + JIT)
        self.assertEqual(got[0]["actuator_seq"], 1)
        # WL-4: true per-event dx with the actuator timebase of each move
        rows = act_rows(self.m, "WL-4")
        got = [r for r in L.labels_from_act(self.m, "WL-4", 1920, 1080, rows) if r["expected_kind"] == "MOVED"]
        self.assertEqual(len(got), 90)
        self.assertEqual(got[0]["t_us"], 1016666 + JIT + 1)
        self.assertEqual(got[0]["dx"], 3)
        # WL-1c per-frame rows anchored at the actuator t_us
        got = [r for r in L.labels_from_act(self.m, "WL-1c", 1920, 1080, act_rows(self.m, "WL-1c")) if r["expected_kind"] == "MOVED"]
        self.assertEqual(got[0]["frame"], L2.frame_of(1000000 + JIT))
        self.assertEqual(got[23]["frame"], L2.frame_of(1000000 + JIT) + 23)
        # divergence / failure / disorder are rc 5, malformed rows rc 2
        rows = act_rows(self.m, "WL-1h")
        for bad in (rows[:-1], [dict(rows[0], t_plan_us=1)] + rows[1:], [dict(rows[0], rc=5)] + rows[1:],
                    [dict(rows[0], item="WL-1s")] + rows[1:], list(reversed(rows))):
            with self.assertRaises(R.WLError) as c:
                L.labels_from_act(self.m, "WL-1h", 1920, 1080, bad)
            self.assertEqual(c.exception.rc, 5)
        with self.assertRaises(R.WLError) as c:
            L.labels_from_act(self.m, "WL-1h", 1920, 1080, [dict(rows[0], extra=1)] + rows[1:])
        self.assertEqual(c.exception.rc, 2)
        with self.assertRaises(R.WLError) as c:
            L.labels_for(self.m, "WL-4", 3840, 2160)
        self.assertEqual(c.exception.rc, 2)

    def test_labels_v3_no_rc1(self):
        def run(argv):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                return L.main(argv)
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "x.json")
            with open(bad, "w") as f:
                f.write("{not json")
            codes = [run(["validate", MAN3]), run([]), run(["validate"]), run(["bogus", MAN3]),
                     run(["validate", os.path.join(td, "absent.json")]), run(["validate", bad]),
                     run(["labels", MAN3, "WL-9", "1920", "1080"]), run(["labels", MAN3, "WL-4", "x", "1080"]),
                     run(["labels", MAN3, "WL-4", "640", "480"]), run(["labels-act", MAN3, "WL-4", "1920", "1080", bad]),
                     run(["labels", MAN3, "WL-4", "1920", "1080"])]
            self.assertEqual(codes, [0, 2, 2, 2, 2, 2, 2, 2, 2, 2, 0])
            self.assertNotIn(1, codes)
            with open(MAN3) as f:
                m = json.load(f)
            m["items"][0]["scene_sha256"] = "0" * 64
            shutil.copytree(os.path.join(WL, "scenes"), os.path.join(td, "scenes"))  # relative pins resolve in td
            drift = os.path.join(td, "drift.json")
            with open(drift, "w") as f:
                json.dump(m, f)
            self.assertEqual(run(["validate", drift]), 5)
        p = subprocess.run([sys.executable, "-B", os.path.join(WL, "labels_v3.py"), "validate", MAN3], capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, L.PINNED_PREREG_V3_SHA256))
        p = subprocess.run([sys.executable, "-B", os.path.join(WL, "labels_v3.py")], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
