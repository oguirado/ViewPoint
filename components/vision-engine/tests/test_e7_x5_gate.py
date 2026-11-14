"""FX4 JC2-6: G-E4 verdict requires a verified one-core pin plus median AND p95 budgets (failing-first on BASE_E)."""
import os
import unittest
from unittest import mock

from eval import x5_cpu as x5

MED = {"1280x720": 8000, "1920x1080": 17000}
P95 = {"1280x720": 12000, "1920x1080": 22000}


class TestX5Gate(unittest.TestCase):
    def test_in_budget_pinned_passes(self):
        self.assertTrue(x5.gate_verdict(MED, P95, True))

    def test_x5_p95_over_budget_is_rejected(self):
        self.assertFalse(x5.gate_verdict(MED, dict(P95, **{"1280x720": 50000}), True))
        self.assertFalse(x5.gate_verdict(MED, dict(P95, **{"1920x1080": 40001}), True))
        self.assertTrue(x5.gate_verdict(MED, dict(P95, **{"1280x720": 25000, "1920x1080": 40000}), True))

    def test_x5_unpinned_is_not_accepted(self):
        self.assertFalse(x5.gate_verdict(MED, P95, False))
        self.assertFalse(x5.gate_verdict(MED, P95, None))

    def test_median_over_budget_and_missing_size_rejected(self):
        self.assertFalse(x5.gate_verdict(dict(MED, **{"1280x720": 15001}), P95, True))
        self.assertFalse(x5.gate_verdict({"1280x720": 8000}, P95, True))

    def test_pin_is_verified_not_assumed(self):
        with mock.patch.object(os, "sched_setaffinity", lambda *a: None), \
                mock.patch.object(os, "sched_getaffinity", lambda pid: {1, 2, 3}):
            self.assertFalse(x5.pin_one_core())

    def test_run_end_to_end_tiny(self):
        sizes = ((256, 160, 10 ** 9),)
        lim = {"256x160": 10 ** 9}
        with mock.patch.object(x5, "pin_one_core", lambda: True):
            _, s = x5.run(None, sizes=sizes, n=16, repeats=1, p95_limits=lim)
            self.assertEqual(s["verdict"], "PASS")
            _, s = x5.run(None, sizes=sizes, n=16, repeats=1, p95_limits={"256x160": 0})
            self.assertEqual(s["verdict"], "FAIL")
            _, s = x5.run(None, sizes=sizes, n=16, repeats=1, p95_limits={})
            self.assertEqual(s["verdict"], "FAIL")
        with mock.patch.object(x5, "pin_one_core", lambda: False):
            rows, s = x5.run(None, sizes=sizes, n=16, repeats=1, p95_limits=lim)
        self.assertEqual((s["verdict"], rows[0]["pinned_one_core"]), ("FAIL", False))


if __name__ == "__main__":
    unittest.main()
