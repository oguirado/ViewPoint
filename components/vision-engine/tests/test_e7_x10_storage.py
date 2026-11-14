"""FX6 lane K: X10 G-E8 storage is scoped to CLEAN 200-frame runs (SPEC_VMEM l.254 / SPEC_TIME l.276) measured through the
real analyze -> vmem.ingest path before/after clean compaction; the heavy flicker run is an advisory row only."""
import unittest

from eval import x10_compaction as x10

SC = x10.CLEAN_SCENARIOS


def clean(before=1000, after=100, drop=None):
    return lambda: [{"scenario": s, "before_bytes": before, "after_bytes": after, "info": {}} for s in SC if s != drop]


def verdict(measure, advisory=None):
    return x10.run(None, measure=measure, advisory=advisory)


class TestX10CleanStorage(unittest.TestCase):
    def test_clean_scenarios_are_the_spec_ones(self):
        self.assertEqual(SC, ("clean_static", "gpu_jitter"))
        self.assertEqual((x10.L2_LIMIT_BYTES, x10.L2_AFTER_LIMIT_BYTES), (968768, 81952))  # AMD-L2-1/2: 720p n_I*26*N + 32,768

    def test_limits_boundaries(self):
        self.assertTrue(x10.clean_storage_verdict(clean(968768, 81952)()))
        self.assertFalse(x10.clean_storage_verdict(clean(968769, 100)()))
        self.assertFalse(x10.clean_storage_verdict(clean(1000, 81953)()))
        self.assertFalse(x10.clean_storage_verdict(clean(0, 100)()))
        self.assertFalse(x10.clean_storage_verdict(None))
        self.assertFalse(x10.clean_storage_verdict("x"))

    def test_pass_in_limit(self):
        rows, s = verdict(clean())
        self.assertEqual((s["verdict"], s["l2_storage_ok"], s["cycles_ok"]), ("PASS", True, 3))

    def test_clean_over_before_limit_fails(self):
        rows, s = verdict(clean(before=968769))
        self.assertEqual((s["verdict"], s["l2_storage_ok"]), ("FAIL", False))
        self.assertEqual(s["cycles_ok"], 3)  # compaction/FG checks preserved
        self.assertTrue(all(r["rc"] == 5 for r in rows if r["item"].startswith("l2_clean_")))

    def test_clean_over_after_limit_fails(self):
        self.assertEqual(verdict(clean(after=81953))[1]["verdict"], "FAIL")

    def test_one_clean_scenario_over_fails(self):
        def m():
            a = clean()()
            a[1]["before_bytes"] = 968769
            return a
        rows, s = verdict(m)
        self.assertEqual(s["verdict"], "FAIL")
        by = {r["item"]: r["ok"] for r in rows}
        self.assertEqual((by["l2_clean_clean_static"], by["l2_clean_gpu_jitter"]), (True, False))

    def test_absent_clean_measurement_fails(self):
        self.assertEqual(verdict(None)[1]["verdict"], "FAIL")
        self.assertEqual(verdict(clean(drop="gpu_jitter"))[1]["verdict"], "FAIL")
        self.assertEqual(verdict(lambda: [])[1]["verdict"], "FAIL")

    def test_raising_measurement_fails(self):
        def boom():
            raise RuntimeError("no ingest")
        rows, s = verdict(boom)
        self.assertEqual(s["verdict"], "FAIL")
        self.assertTrue(any("RuntimeError" in r.get("error", "") for r in rows))

    def test_heavy_row_cannot_flip_verdict(self):
        for adv in (lambda: (5000000, {"frames": 200}), lambda: (1, {}), lambda: 1 / 0):
            rows, s = verdict(clean(), adv)
            self.assertEqual(s["verdict"], "PASS")
            h = [r for r in rows if r["item"] == "l2_heavy_flicker_advisory"][0]
            self.assertTrue(h["advisory"] and h["ok"] and h["rc"] == 0)
        rows, s = verdict(clean(before=968769), lambda: (1, {}))
        self.assertEqual(s["verdict"], "FAIL")  # a tiny heavy row cannot rescue failing clean runs

    def test_heavy_breakdown_recorded(self):
        rows, _ = verdict(clean(), lambda: (1234, {"breakdown": {"blobs": 1000, "gen": 234}}))
        h = [r for r in rows if r["item"] == "l2_heavy_flicker_advisory"][0]
        self.assertEqual((h["l2_bytes_measured"], h["breakdown"]["blobs"]), (1234, 1000))

    def test_real_tiny_clean_run_through_ingest(self):
        m = x10.measure_clean_l2("gpu_jitter", 256, 160, 16)
        self.assertGreater(m["before_bytes"], 0)
        self.assertGreater(m["after_bytes"], 0)
        self.assertGreater(m["info"]["retired"], 0)
        self.assertIn("blobs", m["info"]["breakdown"])

    def test_real_tiny_heavy_measure_has_breakdown(self):
        b, info = x10.measure_run_l2(256, 160, 16, "flicker")
        self.assertGreater(b, 0)
        self.assertGreater(info["regions"], 0)
        self.assertGreater(sum(info["breakdown"].values()), 0)


if __name__ == "__main__":
    unittest.main()
