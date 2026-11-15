"""Unit tests for eval/oracle_score.py"""
import unittest
import numpy as np

from eval.oracle_score import score_oracle, ScoreError


class TestOracleScore(unittest.TestCase):

    def test_exact_alignment(self):
        # 1 frame, grid size s=16 -> W=32, H=32 => gw=2, gh=2
        # Label covers tile (0, 0): x=0, y=0, w=16, h=16 with CHANGED (class 2)
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[0, 0, 16, 16]],
                "ideal": 2,  # CHANGED
                "confidence": "exact",
            }
        ]
        # Engine output: tile (0,0) is CHANGED (2), others UNCHANGED (0)
        cls = np.zeros((1, 2, 2), dtype=int)
        cls[0, 0, 0] = 2

        res = score_oracle(records, cls, W=32, H=32)

        conf = res["confusion"]
        self.assertEqual(conf[2][2], 1)  # Ideal CHANGED, Predicted CHANGED
        self.assertEqual(conf[0][0], 3)  # Ideal UNCHANGED, Predicted UNCHANGED
        self.assertEqual(res["support"]["CHANGED"], 1)
        self.assertEqual(res["support"]["UNCHANGED"], 3)
        self.assertEqual(res["straddle_count"], 0)

    def test_straddling_rects(self):
        # 1 frame, W=32, H=16 => gw=2, gh=1 (s=16)
        # Record 1: covers x=8..24 (intersects tile 0: 0..16 and tile 1: 16..32) with CHANGED (2)
        # Record 2: covers x=8..24 with JITTER (1)
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[8, 0, 16, 16]],
                "ideal": 2,  # CHANGED
                "confidence": "exact",
            },
            {
                "f0": 0,
                "f1": 0,
                "rects": [[8, 0, 16, 16]],
                "ideal": 1,  # JITTER
                "confidence": "exact",
            },
        ]
        cls = np.zeros((1, 1, 2), dtype=int)

        res = score_oracle(records, cls, W=32, H=16)

        # Both tile (0,0) and tile (0,1) intersect two records with different ideal classes (2 and 1)
        self.assertEqual(res["straddle_count"], 2)
        # Neither tile is scored in confusion matrix
        self.assertEqual(sum(sum(row) for row in res["confusion"]), 0)

    def test_non_multiple_of_s_edges(self):
        # W=20, H=20 => s=16, gw=2, gh=2.
        # Tile 0: 0..16, Tile 1: 16..32 (image edge at 20)
        # Rect: x=17, y=17, w=2, h=2 (inside tile (1,1))
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[17, 17, 2, 2]],
                "ideal": "MOVED",
                "mv": [3, 4],
                "confidence": "exact",
            }
        ]
        cls = np.zeros((1, 2, 2), dtype=int)
        cls[0, 1, 1] = 3  # MOVED
        mv_dx = np.zeros((1, 2, 2), dtype=int)
        mv_dy = np.zeros((1, 2, 2), dtype=int)
        mv_dx[0, 1, 1] = 3
        mv_dy[0, 1, 1] = 4

        res = score_oracle(records, cls, W=20, H=20, mv_dx=mv_dx, mv_dy=mv_dy)

        self.assertEqual(res["support"]["MOVED"], 1)
        self.assertEqual(res["support"]["UNCHANGED"], 3)
        self.assertEqual(res["mv_exact"], 1)
        self.assertEqual(res["mv_total"], 1)

    def test_moved_with_right_and_wrong_vectors(self):
        # 1 frame, W=32, H=16 => gw=2, gh=1 (s=16)
        # Tile 0: ideal MOVED mv=[5, -2], predicted MOVED mv=[5, -2] (exact)
        # Tile 1: ideal MOVED mv=[5, -2], predicted MOVED mv=[5, 0] (wrong vector)
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[0, 0, 32, 16]],
                "ideal": 3,
                "mv": [5, -2],
                "confidence": "exact",
            }
        ]
        cls = np.full((1, 1, 2), 3, dtype=int)
        mv_dx = np.full((1, 1, 2), 5, dtype=int)
        mv_dy = np.array([[[ -2, 0 ]]], dtype=int)

        res = score_oracle(records, cls, W=32, H=16, mv_dx=mv_dx, mv_dy=mv_dy)

        self.assertEqual(res["support"]["MOVED"], 2)
        self.assertEqual(res["confusion"][3][3], 2)
        self.assertEqual(res["mv_total"], 2)
        self.assertEqual(res["mv_exact"], 1)

    def test_zero_support_classes(self):
        # CHANGED label on tile 0, tile 1 is uncovered (UNCHANGED)
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[0, 0, 16, 16]],
                "ideal": 2,
                "confidence": "exact",
            }
        ]
        cls = np.zeros((1, 1, 2), dtype=int)
        cls[0, 0, 0] = 2

        res = score_oracle(records, cls, W=32, H=16)

        untested = res["untested"]
        self.assertIn("JITTER", untested)
        self.assertIn("MOVED", untested)
        self.assertIn("FLICKER", untested)
        self.assertIn("FROZEN", untested)
        self.assertIn("DRIFT", untested)
        self.assertNotIn("CHANGED", untested)
        self.assertNotIn("UNCHANGED", untested)

    def test_derived_records_excluded(self):
        records = [
            {
                "f0": 0,
                "f1": 0,
                "rects": [[0, 0, 16, 16]],
                "ideal": 2,  # CHANGED
                "confidence": "derived",
            },
            {
                "f0": 0,
                "f1": 0,
                "rects": [[0, 0, 16, 16]],
                "ideal": 1,  # JITTER
                "confidence": "exact",
            },
        ]
        cls = np.zeros((1, 1, 1), dtype=int)
        cls[0, 0, 0] = 1

        res = score_oracle(records, cls, W=16, H=16)

        # Derived record is ignored for scoring, so no straddle occurs with exact record
        self.assertEqual(res["straddle_count"], 0)
        self.assertEqual(res["support"]["JITTER"], 1)
        self.assertEqual(res["support"]["CHANGED"], 0)
        self.assertEqual(res["diagnostics"]["derived_records_count"], 1)
        self.assertEqual(res["diagnostics"]["exact_records_count"], 1)


if __name__ == "__main__":
    unittest.main()
