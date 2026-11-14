"""AMD-L2-3 (operator option 1): X10 limits follow n_I, N, n_look, n_lu.
before <= n_I*26*N + 32*n_look + 1,600*n_lu + 32,768; after <= VM_GEN_KEEP*(18,432 + 1,600*n_lu) + 32*n_look + 8,192."""
import unittest

from eval import x10_compaction as x10
from vmem import gen as G


def ms(before, after, n_look=3728, n_lu=62, **info):
    i = {"resolution": "1280x720", "frames": 200, "n_look": n_look, "n_lu": n_lu, "n_look_before": n_look,
         "n_lu_before": n_lu, "window_ok": True}
    i.update(info)
    return [{"scenario": s, "before_bytes": before, "after_bytes": after, "info": i} for s in x10.CLEAN_SCENARIOS]


class TestFormula(unittest.TestCase):
    def test_720p_reference(self):
        self.assertEqual(x10.l2_before_limit(1280, 720, 200, 3728, 62), 1187264)
        self.assertEqual(x10.l2_after_limit(3728, n_lu=62), 598016)
        self.assertEqual((x10.L2_LIMIT_FULL_BYTES, x10.L2_AFTER_LIMIT_FULL_BYTES), (1187264, 598016))

    def test_terms(self):
        self.assertEqual(x10.l2_before_limit(1280, 720, 200, 0, 0), 968768)
        self.assertEqual(x10.l2_before_limit(1280, 720, 200, 1, 0) - 968768, 32)
        self.assertEqual(x10.l2_before_limit(1280, 720, 200, 0, 1) - 968768, 1600)
        self.assertEqual(x10.l2_after_limit(0, n_lu=1) - x10.l2_after_limit(0), G.VM_GEN_KEEP * 1600)
        self.assertEqual(x10.l2_before_limit(1920, 1080, 200, 2040 + 64, 5), 10 * 26 * 2040 + 32 * 2104 + 8000 + 32768)

    def test_boundaries(self):
        self.assertTrue(x10.clean_storage_verdict(ms(1187264, 598016)))
        self.assertFalse(x10.clean_storage_verdict(ms(1187265, 598016)))
        self.assertFalse(x10.clean_storage_verdict(ms(1187264, 598017)))
        self.assertTrue(x10.clean_storage_verdict(ms(1120641, 508951)))  # FX7-O full-look measurement

    def test_limit_follows_n_lu(self):
        self.assertFalse(x10.clean_storage_verdict(ms(1120641, 508951, n_lu=0, n_look_before=3728, n_lu_before=0)))
        self.assertFalse(x10.clean_storage_verdict(ms(1000, 100, n_lu=-1)))
        self.assertFalse(x10.clean_storage_verdict(ms(1000, 100, n_lu_before=True)))


class TestRealFullLookRun(unittest.TestCase):
    def test_720p_clean_static_passes_and_metadata_below_allowance(self):
        m = x10.measure_clean_l2("clean_static")
        i = m["info"]
        self.assertEqual((i["n_lu"], i["n_look"]), (62, 3728))
        self.assertTrue(x10.clean_storage_verdict([m, dict(m, scenario="gpu_jitter")]))
        gen_b = i["breakdown"]["gen"] // len(i["gens"])
        self.assertLessEqual(gen_b, x10.AFTER_GEN_B + x10.LU_META_B * i["n_lu"])
        self.assertLessEqual(-(-(gen_b) // i["n_lu"]), x10.LU_META_B)  # measured total B/look-unit rounded up


if __name__ == "__main__":
    unittest.main()
