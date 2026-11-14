"""Unit tests for eval.honesty.metrics (stdlib unittest only, hand-computed cases)."""

import unittest

from eval.honesty.metrics import (
    CHANGED,
    CLASSES,
    DRIFT,
    FLICKER,
    FROZEN,
    JITTER,
    MOVED,
    UNCHANGED,
    class_honesty,
    silent_miss_ppm,
)


def _zero_matrix():
    return [[0 for _ in range(7)] for _ in range(7)]


class TestHonestyMetrics(unittest.TestCase):
    def test_all_diagonal_matrix(self):
        """Hand-computed all-diagonal matrix: 0 ppm silent miss, 1000 permille precision, no flags."""
        counts = [120, 30, 50, 40, 20, 10, 5]
        matrix = _zero_matrix()
        for idx, count in enumerate(counts):
            matrix[idx][idx] = count

        self.assertEqual(silent_miss_ppm(matrix), 0)

        honesty = class_honesty(matrix)
        self.assertEqual(tuple(honesty.keys()), CLASSES)
        for idx, name in enumerate(CLASSES):
            expected_count = counts[idx]
            entry = honesty[name]
            self.assertEqual(entry["predicted"], expected_count)
            self.assertEqual(entry["support"], expected_count)
            self.assertEqual(entry["tp"], expected_count)
            self.assertEqual(entry["precision"], f"{expected_count}/{expected_count}")
            self.assertEqual(entry["permille"], 1000)
            self.assertFalse(entry["zero_support_emitted"])
            self.assertFalse(entry["low_precision"])
            self.assertFalse(entry["flagged"])
            self.assertEqual(entry["flags"], ())

    def test_real_changes_predicted_as_jitter_exact_ppm(self):
        """Hand-computed real changes (CHANGED, MOVED, FLICKER) predicted as JITTER or UNCHANGED."""
        matrix = _zero_matrix()
        matrix[UNCHANGED][UNCHANGED] = 500
        matrix[JITTER][JITTER] = 100
        # Real-change classes:
        # CHANGED support = 100 (25 -> JITTER, 75 -> CHANGED)
        matrix[CHANGED][JITTER] = 25
        matrix[CHANGED][CHANGED] = 75
        # MOVED support = 200 (50 -> JITTER, 10 -> UNCHANGED, 140 -> MOVED)
        matrix[MOVED][JITTER] = 50
        matrix[MOVED][UNCHANGED] = 10
        matrix[MOVED][MOVED] = 140
        # FLICKER support = 100 (15 -> JITTER, 85 -> FLICKER)
        matrix[FLICKER][JITTER] = 15
        matrix[FLICKER][FLICKER] = 85

        # Total real-change support = 100 + 200 + 100 = 400.
        # Silent misses (predicted UNCHANGED or JITTER) = 25 + (50 + 10) + 15 = 100.
        # Exact ppm = (100 * 1_000_000) // 400 = 250_000.
        self.assertEqual(silent_miss_ppm(matrix), 250_000)

        # JITTER predicted count = 100 (true) + 25 + 50 + 15 = 190, tp = 100.
        # JITTER permille = (100 * 1000) // 190 = 526 (< 900 -> flagged).
        honesty = class_honesty(matrix)
        self.assertEqual(honesty["JITTER"]["predicted"], 190)
        self.assertEqual(honesty["JITTER"]["support"], 100)
        self.assertEqual(honesty["JITTER"]["precision"], "100/190")
        self.assertEqual(honesty["JITTER"]["permille"], 526)
        self.assertTrue(honesty["JITTER"]["low_precision"])
        self.assertFalse(honesty["JITTER"]["zero_support_emitted"])
        self.assertTrue(honesty["JITTER"]["flagged"])

        # Test integer floor truncation on non-divisible real-change denominator:
        # 1 out of 3 CHANGED tile-frames predicted as JITTER -> (1 * 1_000_000) // 3 = 333_333 ppm.
        matrix_floor = _zero_matrix()
        matrix_floor[CHANGED][JITTER] = 1
        matrix_floor[CHANGED][CHANGED] = 2
        self.assertEqual(silent_miss_ppm(matrix_floor), 333_333)

    def test_zero_support_emitted_class(self):
        """Hand-computed zero-support emitted class is flagged."""
        matrix = _zero_matrix()
        matrix[UNCHANGED][UNCHANGED] = 95
        # 5 ideal UNCHANGED tile-frames falsely emitted as FLICKER, while FLICKER has 0 support.
        matrix[UNCHANGED][FLICKER] = 5
        matrix[CHANGED][CHANGED] = 40

        self.assertEqual(silent_miss_ppm(matrix), 0)

        honesty = class_honesty(matrix)
        flicker = honesty["FLICKER"]
        self.assertEqual(flicker["support"], 0)
        self.assertEqual(flicker["predicted"], 5)
        self.assertEqual(flicker["tp"], 0)
        self.assertEqual(flicker["precision"], "0/5")
        self.assertEqual(flicker["permille"], 0)
        self.assertTrue(flicker["zero_support_emitted"])
        self.assertTrue(flicker["low_precision"])
        self.assertTrue(flicker["flagged"])
        self.assertIn("zero_support", flicker["flags"])

        # FROZEN and DRIFT have 0 support and 0 predicted (not emitted), so they must NOT be flagged.
        for unemitted in ("FROZEN", "DRIFT"):
            self.assertEqual(honesty[unemitted]["support"], 0)
            self.assertEqual(honesty[unemitted]["predicted"], 0)
            self.assertFalse(honesty[unemitted]["zero_support_emitted"])
            self.assertFalse(honesty[unemitted]["flagged"])

    def test_empty_matrix_defined_behaviour(self):
        """Empty (all-zero) 7x7 matrix has defined behaviour and raises no ZeroDivisionError."""
        empty = _zero_matrix()
        self.assertEqual(silent_miss_ppm(empty), 0)

        honesty = class_honesty(empty)
        self.assertEqual(tuple(honesty.keys()), CLASSES)
        for name in CLASSES:
            entry = honesty[name]
            self.assertEqual(entry["predicted"], 0)
            self.assertEqual(entry["support"], 0)
            self.assertEqual(entry["tp"], 0)
            self.assertEqual(entry["precision"], "0/0")
            self.assertEqual(entry["permille"], 0)
            self.assertFalse(entry["zero_support_emitted"])
            self.assertFalse(entry["low_precision"])
            self.assertFalse(entry["flagged"])
            self.assertEqual(entry["flags"], ())

    def test_precision_threshold_boundary_900_permille(self):
        """Precision of 900 permille is not flagged; 899 permille is flagged."""
        matrix_pass = _zero_matrix()
        matrix_pass[CHANGED][CHANGED] = 9
        matrix_pass[UNCHANGED][CHANGED] = 1
        matrix_pass[UNCHANGED][UNCHANGED] = 90
        honesty_pass = class_honesty(matrix_pass)
        self.assertEqual(honesty_pass["CHANGED"]["precision"], "9/10")
        self.assertEqual(honesty_pass["CHANGED"]["permille"], 900)
        self.assertFalse(honesty_pass["CHANGED"]["flagged"])

        matrix_fail = _zero_matrix()
        matrix_fail[CHANGED][CHANGED] = 899
        matrix_fail[UNCHANGED][CHANGED] = 101
        matrix_fail[UNCHANGED][UNCHANGED] = 1000
        honesty_fail = class_honesty(matrix_fail)
        self.assertEqual(honesty_fail["CHANGED"]["precision"], "899/1000")
        self.assertEqual(honesty_fail["CHANGED"]["permille"], 899)
        self.assertTrue(honesty_fail["CHANGED"]["low_precision"])
        self.assertTrue(honesty_fail["CHANGED"]["flagged"])

    def test_validation_rejects_invalid_inputs(self):
        """Non-7x7 shapes, negative counts, and non-integers are rejected."""
        with self.assertRaises(ValueError):
            silent_miss_ppm([[0] * 6 for _ in range(7)])
        with self.assertRaises(ValueError):
            class_honesty([[0] * 7 for _ in range(6)])

        neg = _zero_matrix()
        neg[FROZEN][DRIFT] = -1
        with self.assertRaises(ValueError):
            silent_miss_ppm(neg)

        bad_bool = _zero_matrix()
        bad_bool[0][0] = True
        with self.assertRaises(TypeError):
            class_honesty(bad_bool)


if __name__ == "__main__":
    unittest.main()
