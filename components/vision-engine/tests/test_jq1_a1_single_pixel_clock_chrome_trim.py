"""tests/test_jq1_a1_single_pixel_clock_chrome_trim.py

Pin the one-pixel variant of D-A1-F4-CLOCK, beyond the existing unanimated chrome test.
"""

import unittest
import numpy as np
from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles, reset_ops


def _build_frame_pair(textured=True):
    """Build RAM uint8 (480,640) frame pairs with default_rng(404).

    50-row header, 40-row footer starting at row 440, random body rows 50 through 439.
    Translate the body up 24 rows, copying prev rows 74 through 439 into cur rows 50 through 415;
    fill rows 416 through 439 with fresh random bytes.
    Change only header pixel (5,5) by xor 255 in cur.
    """
    rng = np.random.default_rng(404)
    H, W = 480, 640
    prev = np.empty((H, W), dtype=np.uint8)
    cur = np.empty((H, W), dtype=np.uint8)

    # 50-row header (rows 0..49)
    if textured:
        header = rng.integers(0, 256, size=(50, W), dtype=np.uint8)
    else:
        header = np.full((50, W), 60, dtype=np.uint8)

    prev[:50, :] = header
    cur[:50, :] = header.copy()
    cur[5, 5] ^= 255

    # 40-row footer starting at row 440 (rows 440..479)
    if textured:
        footer = rng.integers(0, 256, size=(40, W), dtype=np.uint8)
    else:
        footer = np.full((40, W), 60, dtype=np.uint8)

    prev[440:, :] = footer
    cur[440:, :] = footer

    # Random body rows 50 through 439 (390 rows)
    prev_body = rng.integers(0, 256, size=(390, W), dtype=np.uint8)
    prev[50:440, :] = prev_body

    # Translate body up 24 rows: copy prev rows 74 through 439 into cur rows 50 through 415
    cur[50:416, :] = prev[74:440, :]

    # Fill rows 416 through 439 with fresh random bytes
    cur[416:440, :] = rng.integers(0, 256, size=(24, W), dtype=np.uint8)

    return prev, cur


class TestSinglePixelClockChromeTrim(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_exact_body_between_static_chrome_keeps_zero_score(self):
        prev, cur = _build_frame_pair(textured=True)
        cur[5, 5] = prev[5, 5]
        for name, p, c, expected in (
            ("up", prev, cur, (0, -24, 0)),
            ("down", cur, prev, (0, 24, 0)),
            ("horizontal", prev.T, cur.T, (-24, 0, 0)),
        ):
            with self.subTest(direction=name):
                self.assertEqual(estimate_global_motion(p, c, 64), expected)

    def test_body_disagreement_cannot_receive_exact_viewport_score(self):
        prev, cur = _build_frame_pair(textured=True)
        cur[5, 5] = prev[5, 5]
        cur[150, 100] ^= 1
        result = estimate_global_motion(prev, cur, 64)
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], (0, -24))
        self.assertGreater(result[2], 0)

    def test_changing_textured_header_keeps_positive_residual(self):
        prev, cur = _build_frame_pair(textured=True)
        result = estimate_global_motion(prev, cur, 64)
        self.assertIsNotNone(result)
        self.assertEqual(result[:2], (0, -24))
        self.assertGreater(result[2], 0)

    def test_textured_chrome_single_pixel_clock(self):
        """Require estimate_global_motion at max_d=200 to emit (0,-24) with integer score

        as a normal passing test on the seeded textured chrome variant.
        """
        prev, cur = _build_frame_pair(textured=True)
        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res)
        dx, dy, score = res
        self.assertEqual((dx, dy), (0, -24))
        self.assertIsInstance(score, int)

    def test_flat_chrome_single_pixel_clock_d_a1_f4_clock(self):
        """D-A1-F4-CLOCK: Flat luma-60 chrome with a single-pixel header clock change

        currently returns None from estimate_global_motion because header/footer static
        chrome scanning stops at pixel (5,5), treating remaining header/footer as part of
        the moving band.
        """
        prev, cur = _build_frame_pair(textured=False)
        res = estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(res)
        dx, dy, score = res
        self.assertEqual((dx, dy), (0, -24))
        self.assertIsInstance(score, int)

    def test_consensus_tiles_chrome_and_footer(self):
        """Require consensus_tiles at the truth vector (0,-24) and s=16 to leave

        the first three tile rows and all fully footer-contained tiles false.
        """
        prev, cur = _build_frame_pair(textured=True)
        mask = consensus_tiles(prev, cur, s=16, dx=0, dy=-24)

        # First three tile rows (tile rows 0, 1, 2 covering Y 0..47)
        self.assertFalse(np.any(mask[:3, :]), "First three tile rows must be all False")

        # Fully footer-contained tiles: footer is rows 440..479.
        # Tile row ty spans Y [ty*16, (ty+1)*16).
        # Fully footer-contained in [440, 480) implies ty*16 >= 440 => ty >= 28 (tile rows 28, 29).
        self.assertFalse(np.any(mask[28:, :]), "Fully footer-contained tiles must be all False")


if __name__ == "__main__":
    unittest.main()
