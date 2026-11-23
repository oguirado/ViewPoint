"""FX round 2 on W9 (JW9b-1, JW9b-2): the still probe capture identities must be bound to FIT B6 rows (empty, unrelated
and altered identities refuse split_leak, API and CLI) and acceptance refuses metrics missing for any declared GATE seed."""
import json
import unittest

from blender_visual_qa.truth import calibrate
from tests.test_calibrate import good_rows, split_set
from tests.test_calibrate_fx import cli, run


class StillMembershipTests(unittest.TestCase):  # JW9b-1
    def base(self):
        return split_set(good_rows(), good_rows())

    def test_bound_still_passes(self):
        rows, sd, meta = self.base()
        p, rep = run(rows, sd, meta)
        self.assertIsNotNone(p)
        self.assertEqual(rep["split_violations"], [])

    def _refused(self, caps, reason):
        rows, sd, meta = self.base()
        meta = dict(meta, capture_sha256=caps)
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertEqual(rep["refusal"], "split_leak")
        self.assertIn(reason, rep["split_violations"])
        rc, wrote, rep = cli(rows, meta, "fit:%d-%d,gate:%d-%d" % (sd["fit"][0], sd["fit"][-1], sd["gate"][0], sd["gate"][-1]))
        self.assertEqual((rc, wrote, rep["refusal"]), (5, False, "split_leak"))
        self.assertIn(reason, rep["split_violations"])

    def test_empty_identities_refuse(self):
        self._refused([], "still_probe_capture_absent")
        self._refused([""], "still_probe_capture_absent")
        self._refused(None, "still_probe_capture_absent")

    def test_unrelated_identity_refuses(self):
        self._refused(["unrelated-capture"], "still_probe_capture_unbound")

    def test_altered_identity_refuses(self):
        rows, sd, meta = self.base()
        self._refused([meta["capture_sha256"][0] + "0"], "still_probe_capture_unbound")

    def test_fit_capture_of_another_bench_refuses(self):
        rows, sd, meta = self.base()
        other = next(r["capture_sha256"] for r in rows if r["seed"] == sd["fit"][0] and r["bench"] != "B6")
        self._refused([other], "still_probe_capture_unbound")

    def test_declared_still_seed_without_capture_refuses(self):
        rows, sd, meta = self.base()
        meta = dict(meta, seeds=sd["fit"][:2])
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertIn("still_probe_seed_unbound", rep["split_violations"])


class PopulationTests(unittest.TestCase):  # JW9b-2
    def test_metric_present_for_one_gate_seed_only_refuses(self):
        rows, sd, meta = split_set(good_rows(n=3), good_rows(n=3))
        keys = {"c1b_class_flips": "c1b_flips", "c5_select_ppm": "c5_select", "b5o_global_mv_ppm": "b5o_global_mv",
                "s4_iou_median_ppm": "s4_iou_ppm"}
        for name, key in keys.items():
            part = [r if r.get("seed") == sd["gate"][0] else {k: v for k, v in r.items() if k != key}
                    for r in json.loads(json.dumps(rows))]
            p, rep = run(part, sd, meta)
            self.assertIsNone(p, name)
            c = next(c for c in rep["checks"] if c["name"] == name)
            self.assertEqual((c["status"], c["missing_seeds"]), ("FAIL", sd["gate"][1:]), name)

    def test_b4n_control_required_per_gate_seed(self):
        rows, sd, meta = split_set(good_rows(n=2), good_rows(n=2))
        rows = [r for r in rows if not (r["bench"] == "B4n" and r["seed"] == sd["gate"][1])]
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertEqual(next(c for c in rep["checks"] if c["name"] == "b4n_temporal_fp_tiles")["missing_seeds"], [sd["gate"][1]])


if __name__ == "__main__":
    unittest.main()
