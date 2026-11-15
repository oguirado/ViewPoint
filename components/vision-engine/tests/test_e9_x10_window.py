"""AMD-L2-2 / DECISION_L2b (1): X10 measures 'after compaction' at rollback-window expiry (compaction gen + VM_GEN_KEEP-1
acked gens, gc, journal checkpoint) and asserts list_gens == window; after limit = VM_GEN_KEEP*18,432 + 32*n_look + 8,192."""
import unittest

from eval import x10_compaction as x10
from vmem import gen as G


def ms(after, n_look=1, window_ok=True):
    return [{"scenario": s, "before_bytes": 1000, "after_bytes": after,
             "info": {"resolution": "1280x720", "frames": 200, "n_look": n_look, "window_ok": window_ok}}
            for s in x10.CLEAN_SCENARIOS]


class TestAfterLimit(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(x10.l2_after_limit(1), 81952)
        self.assertEqual(x10.l2_after_limit(0), 81920)
        self.assertEqual(x10.l2_after_limit(10), 4 * 18432 + 320 + 8192)
        self.assertEqual(x10.l2_after_limit(1, keep=2), 2 * 18432 + 32 + 8192)

    def test_boundary(self):
        self.assertTrue(x10.clean_storage_verdict(ms(81952)))
        self.assertFalse(x10.clean_storage_verdict(ms(81953)))
        self.assertTrue(x10.clean_storage_verdict(ms(85932 - 4000)))
        self.assertFalse(x10.clean_storage_verdict(ms(85932)))  # HEAD pre-FX-GC2 measurement

    def test_limit_follows_n_look(self):
        self.assertTrue(x10.clean_storage_verdict(ms(81984, n_look=2)))
        self.assertFalse(x10.clean_storage_verdict(ms(81984, n_look=1)))
        self.assertFalse(x10.clean_storage_verdict(ms(100, n_look=-1)))

    def test_window_mismatch_fails(self):
        self.assertFalse(x10.clean_storage_verdict(ms(1000, window_ok=False)))


class TestWindowExpiryRun(unittest.TestCase):
    def test_real_tiny_run_leaves_exactly_the_window(self):
        m = x10.measure_clean_l2("clean_static", 256, 160, 16)
        i = m["info"]
        self.assertEqual(len(i["gens"]), G.VM_GEN_KEEP)
        self.assertEqual(i["gens"], i["window"])
        self.assertTrue(i["window_ok"])
        self.assertGreaterEqual(i["n_look"], 1)


if __name__ == "__main__":
    unittest.main()
