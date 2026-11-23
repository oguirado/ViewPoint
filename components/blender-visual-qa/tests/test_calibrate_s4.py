"""JW9c-3: S4 acceptance is B1-only (BSPEC_BTRUTH §5.2 "S4 IoU median >= 0.8 on B1"). B2 IoU is C6 diagnostic evidence:
it never substitutes absent B1 evidence and is never pooled into the acceptance median."""
import json
import unittest

from blender_visual_qa.truth import calibrate
from tests.test_calibrate import good_rows, split_set
from tests.test_calibrate_fx import cli, run


def s4_set(b1, b2):
    """GATE rows (3 seeds) with B1 IoU `b1[i]` (None = no B1 observation) and B2 IoU `b2[i]` (None = no B2 observation)."""
    rows, sd, meta = split_set(good_rows(), good_rows())
    for r in rows:
        if r.get("seed") not in sd["gate"] or r.get("bench") not in ("B1", "B2"):
            continue
        r.pop("s4_iou_ppm", None)
        v = (b1 if r["bench"] == "B1" else b2)[sd["gate"].index(r["seed"])]
        if v is not None:
            r["s4_iou_ppm"], r["s4_iou_n"] = v, 1
    return rows, sd, meta


def s4(rep):
    return next(c for c in rep["checks"] if c["name"] == "s4_iou_median_ppm")


GOOD = (900000, 900000, 900000)
NONE = (None, None, None)


class S4B1Tests(unittest.TestCase):
    def test_absent_b1_good_b2_refuses(self):  # the review's b1_probe: certified from B2 alone before the fix
        rows, sd, meta = s4_set(NONE, GOOD)
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        c = s4(rep)
        self.assertEqual((c["status"], c["value"], c.get("absent"), c["missing_seeds"]), ("FAIL", None, True, sd["gate"]))

    def test_absent_b1_good_b2_cli_rc5_no_profile(self):
        rows, sd, meta = s4_set(NONE, GOOD)
        rc, wrote, rep = cli(rows, meta, "fit:%d-%d,gate:%d-%d" % (sd["fit"][0], sd["fit"][-1], sd["gate"][0], sd["gate"][-1]))
        self.assertEqual((rc, wrote), (5, False))
        self.assertEqual(s4(rep)["status"], "FAIL")

    def test_partial_b1_good_b2_refuses_missing_seeds(self):
        rows, sd, meta = s4_set((900000, None, None), GOOD)
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertEqual(s4(rep)["missing_seeds"], sd["gate"][1:])

    def test_poor_b1_good_b2_not_pooled(self):  # pooled B1+B2 median = 900000 (PASS); B1-only median = 700000
        rows, sd, meta = s4_set((700000, 700000, 900000), GOOD)
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        c = s4(rep)
        self.assertEqual((c["status"], c["value"], c["den"]), ("FAIL", 700000, 3))

    def test_complete_b1_positive_without_b2(self):
        rows, sd, meta = s4_set(GOOD, NONE)
        p, rep = run(rows, sd, meta)
        self.assertIsNotNone(p, rep.get("refusal"))
        c = s4(rep)
        self.assertEqual((c["status"], c["value"], c["den"]), ("PASS", 900000, 3))
        self.assertNotIn("missing_seeds", c)

    def test_good_b1_poor_b2_passes(self):  # B2 is diagnostic: a poor B2 never drags the B1 acceptance median
        rows, sd, meta = s4_set(GOOD, (100000, 100000, 100000))
        p, rep = run(rows, sd, meta)
        self.assertIsNotNone(p, rep.get("refusal"))
        self.assertEqual(s4(rep)["value"], 900000)

    def test_bound_not_lowered_and_population_b1_only(self):
        self.assertEqual(calibrate.IOU_MIN_PPM, 800000)
        self.assertEqual(calibrate.S4_BENCHES, ("B1",))
        self.assertEqual(dict((n, b) for n, b, _ in calibrate.POPULATION)["s4_iou_median_ppm"], ("B1",))

    def test_b2_iou_kept_on_rows(self):  # C6 diagnostics stay on the rows (never stripped, never acceptance)
        rows, sd, meta = s4_set(GOOD, (100000, 100000, 100000))
        before = json.dumps([r for r in rows if r.get("bench") == "B2"], sort_keys=True)
        run(rows, sd, meta)
        self.assertEqual(json.dumps([r for r in rows if r.get("bench") == "B2"], sort_keys=True), before)

    def test_required_checks_median_sorted_b1(self):  # lower median of the sorted B1 values (bt.argon.05 on B1)
        rows = [{"bench": "B1", "seed": s, "s4_iou_ppm": v} for s, v in zip((1, 2, 3), (950000, 600000, 900000))]
        rows += [{"bench": "B2", "seed": s, "s4_iou_ppm": 100000} for s in (1, 2, 3)]
        self.assertEqual(dict((n, v) for n, v, *_ in calibrate.required_checks({}, rows, [1, 2, 3]))["s4_iou_median_ppm"], 900000)


if __name__ == "__main__":
    unittest.main()
