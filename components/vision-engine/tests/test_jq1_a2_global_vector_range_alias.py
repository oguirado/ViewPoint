"""tests/test_jq1_a2_global_vector_range_alias.py - Unit tests for global_vector search-range and periodic-alias risk."""

import unittest
import numpy as np

from visioncore.change import global_vector


class TestGlobalVectorRangeAlias(unittest.TestCase):
    """Characterization tests pinning global_vector search-range and periodic-alias risk."""

    def test_periodic_alias_in_range(self):
        """64x64 int16 luma plane with period-16 pattern translated by +20 returns in-range alias (4, 0)."""
        x = np.arange(64)
        pattern_1d = np.where(x % 16 < 8, np.int16(200), np.int16(0))
        y_prev = np.tile(pattern_1d, (64, 1))
        y_cur = np.roll(y_prev, 20, axis=1)

        s = 16
        vec = global_vector(y_cur, y_prev, s)

        # Observable contract: in-range alias (4, 0), not physical (20, 0) and not (0, 0)
        self.assertEqual(vec, (4, 0))
        self.assertNotEqual(vec, (20, 0))
        self.assertNotEqual(vec, (0, 0))

        # Check shifted-overlap SAD vs unshifted SAD
        c = y_cur.sum(axis=0, dtype=np.int64)
        p = y_prev.sum(axis=0, dtype=np.int64)
        n = len(c)  # 64
        d = vec[0]  # 4

        sad_shifted = int(np.abs(c[d:] - p[:n - d]).sum())
        cost_shifted = sad_shifted * n // (n - abs(d))  # 0

        sad_unshifted = int(np.abs(c - p).sum())
        cost_unshifted = sad_unshifted * n // n  # positive cost

        self.assertEqual(sad_shifted, 0)
        self.assertEqual(cost_shifted, 0)
        self.assertGreater(sad_unshifted, 0)
        # Strict threefold improvement required by _axis_shift cost gate
        self.assertTrue(3 * cost_shifted < cost_unshifted)

    def test_nonperiodic_step_edge_out_of_range(self):
        """Nonperiodic step edge translated 24 pixels cannot return (24, 0) due to search bound min(s, 64)."""
        x = np.arange(64)
        step_1d = np.where(x >= 32, np.int16(200), np.int16(0))
        y_prev = np.tile(step_1d, (64, 1))
        y_cur = np.roll(y_prev, 24, axis=1)

        s = 16
        vec = global_vector(y_cur, y_prev, s)

        # Returned vector cannot be physical (24, 0) because search is bounded by dmax = min(16, 64) = 16
        self.assertNotEqual(vec, (24, 0))

        # Verify cost gate behavior: no in-range vector satisfies 3 * best_cost < cost0, so returns (0, 0)
        c = y_cur.sum(axis=0, dtype=np.int64)
        p = y_prev.sum(axis=0, dtype=np.int64)
        dmax = min(s, 64)  # 16

        best, cost0 = None, None
        for d in range(-dmax, dmax + 1):
            if d >= 0:
                sad = int(np.abs(c[d:] - p[:64 - d]).sum())
            else:
                sad = int(np.abs(c[:64 + d] - p[-d:]).sum())
            cost = sad * 64 // (64 - abs(d))
            if d == 0:
                cost0 = cost
            key = (cost, abs(d), d)
            if best is None or key < best:
                best = key

        # Cost gate check
        self.assertFalse(3 * best[0] < cost0 if best else False)
        self.assertEqual(vec, (0, 0))

    def test_sign_reflected_periodic_alias(self):
        """Translating by -20 pixels produces sign-reflected alias (-4, 0) and pins direction."""
        x = np.arange(64)
        pattern_1d = np.where(x % 16 < 8, np.int16(200), np.int16(0))
        y_prev = np.tile(pattern_1d, (64, 1))
        y_cur = np.roll(y_prev, -20, axis=1)

        s = 16
        vec = global_vector(y_cur, y_prev, s)

        # Direction pinned to (-4, 0)
        self.assertEqual(vec, (-4, 0))

    def test_tie_break_ordering(self):
        """Tie-breaking by cost, absolute displacement, and signed displacement."""
        x = np.arange(64)
        pattern_1d = np.where(x % 16 < 8, np.int16(200), np.int16(0))
        y_prev = np.tile(pattern_1d, (64, 1))

        # Shift +8: d = -8 and d = +8 both yield cost 0 and abs(d) = 8.
        # Signed displacement tie-breaker key (cost, abs(d), d) prefers d = -8 since -8 < +8.
        y_cur_8 = np.roll(y_prev, 8, axis=1)
        vec_8 = global_vector(y_cur_8, y_prev, 16)
        self.assertEqual(vec_8, (-8, 0))

        # Shift +20: d = +4 (abs 4) vs d = -12 (abs 12) both yield cost 0.
        # Absolute displacement tie-breaker prefers d = +4 since 4 < 12.
        y_cur_20 = np.roll(y_prev, 20, axis=1)
        vec_20 = global_vector(y_cur_20, y_prev, 16)
        self.assertEqual(vec_20, (4, 0))


if __name__ == "__main__":
    unittest.main()
