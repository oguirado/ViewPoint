"""v12ar ADV_W9 (BFX9d lane T): every declared seed supplies rows, FIT FP population nonempty, one row per
(bench, seed), GATE rows at the frozen T0, per-seed success over every stratum row, B5o metric scoped to B5o."""
import unittest

from blender_visual_qa.truth import calibrate
from tests.test_calibrate import good_rows, split_set


def run(rows, sd, meta, still=(4,)):
    return calibrate.calibrate(rows, list(still), "blender", "r", "g", "ab", seeds=sd, still_meta=meta)


def base(still=(4,)):
    return split_set(good_rows(), good_rows(), still=still)


class PopulationTests(unittest.TestCase):  # ADV-W9-02 (3)
    def test_control_passes(self):
        p, rep = run(*base())
        self.assertIsNotNone(p, rep["split_violations"])

    def test_every_declared_seed_needs_rows(self):
        for arm, extra in (("fit", 50), ("gate", 150)):
            rows, sd, meta = base()
            sd = dict(sd, **{arm: sd[arm] + [extra]})
            p, rep = run(rows, sd, meta)
            with self.subTest(arm=arm):
                self.assertIsNone(p)
                self.assertEqual(rep["refusal"], "split_leak")
                self.assertIn("arm_seed_without_rows", rep["split_violations"])

    def test_fit_fp_population_must_be_nonempty(self):
        rows, sd, meta = base()
        rows = [dict(r, fp_pop_n=0) if r["seed"] in sd["fit"] else r for r in rows]
        p, rep = run(rows, sd, meta)
        self.assertIsNone(p)
        self.assertIn("fit_fp_pop_empty", rep["split_violations"])


class FrozenRowTests(unittest.TestCase):  # ADV-W9-03 (1)
    def test_duplicate_bench_seed_refused(self):
        rows, sd, meta = base()
        dup = dict(next(r for r in rows if r["seed"] == sd["gate"][0] and r["bench"] == "B1"), capture_sha256="gate-dup")
        p, rep = run(rows + [dup], sd, meta)
        self.assertIsNone(p)
        self.assertIn("duplicate_bench_seed", rep["split_violations"])
        fdup = dict(next(r for r in rows if r["seed"] == sd["fit"][0] and r["bench"] == "B1"), capture_sha256="fit-dup")
        p, rep = run(rows + [fdup], sd, meta)  # a FIT duplicate cannot move T0 (maxima): not a leak
        self.assertIsNotNone(p, rep["split_violations"])

    def test_gate_rows_off_the_frozen_t0_refused(self):
        rows, sd, meta = base(still=(100,))
        p, rep = run(rows, sd, meta, still=(100,))
        self.assertIsNotNone(p, rep["refusal"])
        self.assertEqual(p["T0_Q4"], 125)
        for bad in (32, None):  # scored at the default / another threshold, or no threshold recorded
            r2 = [dict(r, t0_q4=bad) if r["seed"] == sd["gate"][0] else r for r in rows]
            if bad is None:
                r2 = [{k: v for k, v in r.items() if k != "t0_q4"} for r in r2]
            p, rep = run(r2, sd, meta, still=(100,))
            with self.subTest(bad=bad):
                self.assertIsNone(p)
                self.assertEqual(rep["refusal"], "t0_not_frozen")
                self.assertIn("gate_row_t0_not_frozen", rep["split_violations"])


class SeedSuccessTests(unittest.TestCase):  # ADV-W9-04 (1)
    def test_failing_b4f_not_masked_by_passing_b4(self):
        rows, sd, meta = base()
        b4f = []
        for g in sd["gate"]:
            src = next(r for r in rows if r["seed"] == g and r["bench"] == "B4")
            b4f.append(dict(src, bench="B4f", capture_sha256="ev-b4f-%d" % g, tl={"10": {"n": 20, "dirty": 0, "cls_ok": 0}},
                            frozen_hit={"ge300k": {"n": 20, "hit": 0}, "lt50k": {"n": 0, "hit": 0}, "50k_150k": {"n": 0, "hit": 0}}))
        p, rep = run(rows + b4f, sd, meta)
        self.assertIsNone(p)
        c = {c["name"]: c for c in rep["checks"]}
        self.assertEqual((c["frozen_ge300k_seed_success"]["value"], c["frozen_ge300k_seed_success"]["status"]), (0, "FAIL"))
        self.assertEqual(c["flicker_seed_success"]["status"], "PASS")  # B4f carries no FLICKER_T stratum: not judged on it

    def test_one_seed_failing_one_of_two_flicker_rows(self):
        rows, sd, meta = base()
        g = sd["gate"][0]
        src = next(r for r in rows if r["seed"] == g and r["bench"] == "B4")
        bad = dict(src, bench="B4b", capture_sha256="ev-b4b", tl={"9": {"n": 4, "dirty": 4, "cls_ok": 3}})
        p, rep = run(rows + [bad], sd, meta)
        c = {c["name"]: c for c in rep["checks"]}
        self.assertEqual(c["flicker_seed_success"]["value"], len(sd["gate"]) - 1)
        self.assertIsNone(p)


class B5oScopeTests(unittest.TestCase):  # ADV-W9-01 (2): b5o_global_mv counted on B5o rows only
    def test_b5o_metric_ignores_other_benches(self):
        rows, sd, meta = base()
        rows = [dict(r, b5o_global_mv={"n": 1000, "exact": 0}) if r["bench"] == "B2" else r for r in rows]
        p, rep = run(rows, sd, meta)
        c = {c["name"]: c for c in rep["checks"]}
        self.assertEqual(c["b5o_global_mv_ppm"]["value"], 1000000)
        self.assertIsNotNone(p)


if __name__ == "__main__":
    unittest.main()
