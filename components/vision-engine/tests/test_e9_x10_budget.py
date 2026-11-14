"""AMD-L2-1 (DECISION_L2 option a): the G-E8 before-compaction L2 limit is derived from the grid, not fixed at 500 KB:
n_I * 26 * N + 32,768 B, n_I = ceil(frames / K_GOP). 720p (N = 3,600, 10 I-frames) -> 968,768 B."""
import unittest

from eval import x10_compaction as x10


class TestX10BeforeBudget(unittest.TestCase):
    def test_formula_values(self):
        self.assertEqual(x10.l2_before_limit(1280, 720), 968768)
        self.assertEqual(x10.l2_before_limit(1920, 1080), 563168)
        self.assertEqual(x10.l2_before_limit(1024, 1024), 1097728)
        self.assertEqual(x10.l2_before_limit(1280, 720, 201), 11 * 26 * 3600 + 32768)

    def test_default_constant_is_720p(self):
        self.assertEqual(x10.L2_LIMIT_BYTES, 968768)
        self.assertEqual(x10.L2_AFTER_LIMIT_BYTES, 81952)  # AMD-L2-2

    def test_measured_fx6_values_pass_before(self):
        def m(b):
            return [{"scenario": s, "before_bytes": b, "after_bytes": 40000, "info": {"resolution": "1280x720", "frames": 200}}
                    for s in x10.CLEAN_SCENARIOS]
        self.assertTrue(x10.clean_storage_verdict(m(927174)))
        self.assertTrue(x10.clean_storage_verdict(m(968768)))
        self.assertFalse(x10.clean_storage_verdict(m(968769)))

    def test_limit_follows_grid_of_measurement(self):
        ms = [{"scenario": s, "before_bytes": 600000, "after_bytes": 1000, "info": {"resolution": "1920x1080", "frames": 200}}
              for s in x10.CLEAN_SCENARIOS]
        self.assertFalse(x10.clean_storage_verdict(ms))  # 600,000 > 563,168 at 1080p


if __name__ == "__main__":
    unittest.main()
