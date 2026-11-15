"""tests/test_jq1_a1_micro_scroll_dy_neg4_flat_page.py - Reproduction test for defect D-A1-MICRO4-JUMP960."""

import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles


def _generate_test_frames():
    H, W = 480, 640
    prev = np.full((H, W), 245, dtype=np.uint8)
    cur = np.full((H, W), 245, dtype=np.uint8)

    rng = np.random.default_rng(707)
    prev_rows = [60, 120, 180, 240, 300, 360]
    cur_rows = [56, 116, 176, 236, 296, 356]

    for pr, cr in zip(prev_rows, cur_rows):
        strip = rng.integers(10, 90, size=(2, 200), dtype=np.uint8)
        prev[pr:pr + 2, 140:340] = strip
        cur[cr:cr + 2, 140:340] = strip

    prev[12, 8] = 40
    cur[8, 8] = 40
    prev[460, 630] = 40
    cur[456, 630] = 40

    return prev, cur


class TestJq1A1MicroScrollDyNeg4FlatPage(unittest.TestCase):

    def test_estimate_global_motion_micro_scroll_dy_neg4(self):
        """Pin D-A1-MICRO4-JUMP960: sparse -4-row micro-scroll returns (0, -4, 0)."""
        prev, cur = _generate_test_frames()
        res = estimate_global_motion(prev, cur, max_d=400)
        self.assertEqual(res, (0, -4, 0), "D-A1-MICRO4-JUMP960: expected (0, -4, 0)")

    def test_consensus_tiles_micro_scroll_dy_neg4(self):
        """Verify consensus_tiles against independent pixel oracle."""
        prev, cur = _generate_test_frames()
        s = 16
        dx, dy = 0, -4
        H, W = cur.shape
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s

        mask = consensus_tiles(prev, cur, s=s, dx=dx, dy=dy)
        self.assertEqual(mask.shape, (gh, gw))

        # Compute independent pixel oracle mask using changed-in-place and valid exact translated correspondence
        oracle_mask = np.zeros((gh, gw), dtype=bool)
        for ty in range(gh):
            for tx in range(gw):
                y0, y1 = ty * s, min((ty + 1) * s, H)
                x0, x1 = tx * s, min((tx + 1) * s, W)

                tile_changed = np.any(cur[y0:y1, x0:x1] != prev[y0:y1, x0:x1])

                match = True
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        sy, sx = y - dy, x - dx
                        if 0 <= sy < H and 0 <= sx < W:
                            if cur[y, x] != prev[sy, sx]:
                                match = False
                                break
                        else:
                            match = False
                            break
                    if not match:
                        break

                oracle_mask[ty, tx] = tile_changed and match

        np.testing.assert_array_equal(mask, oracle_mask)

        # Require all static background tiles false
        for ty in range(gh):
            for tx in range(gw):
                y0, y1 = ty * s, min((ty + 1) * s, H)
                x0, x1 = tx * s, min((tx + 1) * s, W)
                if np.all(prev[y0:y1, x0:x1] == 245) and np.all(cur[y0:y1, x0:x1] == 245):
                    self.assertFalse(mask[ty, tx], f"Static background tile at ({ty}, {tx}) must be False")


if __name__ == "__main__":
    unittest.main()
