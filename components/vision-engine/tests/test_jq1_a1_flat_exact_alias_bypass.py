"""Test D-A1-ALIAS: flat exact alias bypass for sparse block translation in gm_bands_hv."""

import unittest
import numpy as np
from eval.v2x import gm_bands_hv


def make_alias_scene():
    """Construct the exact sparse-block translation test scene."""
    rng = np.random.default_rng(101)
    prev = np.full((480, 640), 250, dtype=np.uint8)
    cur = np.full((480, 640), 250, dtype=np.uint8)

    # Three 1-row by 128-column glyph lines
    for p_row, c_row in [(180, 156), (188, 164), (196, 172)]:
        glyph = rng.integers(0, 150, (1, 128), dtype=np.uint8)
        prev[p_row, 256:384] = glyph
        cur[c_row, 256:384] = glyph

    # 1-pixel translating edge anchors of luma 80
    prev[32, 8] = 80
    cur[8, 8] = 80
    prev[460, 630] = 80
    cur[436, 630] = 80

    return prev, cur


class TestFlatExactAliasBypass(unittest.TestCase):
    def test_estimate_global_motion_alias(self):
        prev, cur = make_alias_scene()
        result = gm_bands_hv.estimate_global_motion(prev, cur, max_d=200)
        self.assertIsNotNone(result)
        dx, dy, score = result
        self.assertIs(type(dx), int)
        self.assertIs(type(dy), int)
        self.assertIs(type(score), int)
        self.assertEqual((dx, dy, score), (0, -24, 0))

    def test_exact_sparse_translation_other_directions(self):
        prev, cur = make_alias_scene()
        for name, p, c, expected in (
            ("reverse", cur, prev, (0, 24, 0)),
            ("horizontal", prev.T, cur.T, (-24, 0, 0)),
        ):
            with self.subTest(direction=name):
                self.assertEqual(gm_bands_hv.estimate_global_motion(p, c, 64), expected)

    def test_exact_sparse_translation_different_glyphs_and_grid_origin(self):
        rng = np.random.default_rng(118)
        prev = np.full((384, 512), 250, dtype=np.uint8)
        cur = prev.copy()
        for row in (180, 188, 196):
            glyph = rng.integers(0, 150, 96, dtype=np.uint8)
            prev[row, 260:356] = glyph
            cur[row - 24, 260:356] = glyph
        self.assertEqual(gm_bands_hv.estimate_global_motion(prev, cur, 64), (0, -24, 0))

    def test_sparse_translation_with_one_pixel_error_abstains(self):
        prev, cur = make_alias_scene()
        cur[156, 300] ^= 1
        self.assertIsNone(gm_bands_hv.estimate_global_motion(prev, cur, 64))

    def test_sparse_translation_retains_stationary_texture(self):
        prev, cur = make_alias_scene()
        texture = np.random.default_rng(2026).integers(0, 256, (32, 32), dtype=np.uint8)
        prev[250:282, 150:182] = texture
        cur[250:282, 150:182] = texture
        self.assertIsNone(gm_bands_hv.estimate_global_motion(prev, cur, 64))

    def test_sparse_adjacent_exact_tie_abstains(self):
        prev = np.full((288, 160), 250, dtype=np.uint8)
        prev[::3, 60] = 80
        prev[::3, 92] = 140
        cur = np.roll(prev, 1, axis=0)
        # Both legal translations are byte-exact, only three pixels apart.
        for dy in (-2, 1):
            lo, hi = max(0, dy), min(prev.shape[0], prev.shape[0] + dy)
            np.testing.assert_array_equal(cur[lo:hi], prev[lo - dy:hi - dy])
        self.assertIsNone(gm_bands_hv.estimate_global_motion(prev, cur, 2))

    def test_localized_patch_cannot_borrow_boundary_support(self):
        for side in (16, 22):
            for row, col in ((63, 31), (64, 32), (65, 33)):
                with self.subTest(side=side, row=row, col=col):
                    prev = np.full((128, 160), 250, dtype=np.uint8)
                    cur = prev.copy()
                    patch = np.random.default_rng(73).integers(
                        0, 150, (side, side), dtype=np.uint8)
                    prev[row:row + side, col:col + side] = patch
                    cur[row - 24:row - 24 + side, col:col + side] = patch
                    self.assertIsNone(gm_bands_hv.estimate_global_motion(prev, cur, 40))

    def test_consensus_tiles(self):
        prev, cur = make_alias_scene()
        s = 16
        dx, dy = 0, -24

        ct = gm_bands_hv.consensus_tiles(prev, cur, s, dx, dy)

        H, W = cur.shape
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s

        oracle_mask = np.zeros((gh, gw), dtype=bool)

        for r in range(gh):
            for c in range(gw):
                y_min, y_max = r * s, min((r + 1) * s, H)
                x_min, x_max = c * s, min((c + 1) * s, W)

                # Tile changed in place
                tile_cur = cur[y_min:y_max, x_min:x_max]
                tile_prev = prev[y_min:y_max, x_min:x_max]
                if not np.any(tile_cur != tile_prev):
                    oracle_mask[r, c] = False
                    continue

                # Every in-bounds destination pixel has a valid translated source with identical luma
                valid = True
                for y in range(y_min, y_max):
                    for x in range(x_min, x_max):
                        sy, sx = y - dy, x - dx
                        if not (0 <= sy < H and 0 <= sx < W):
                            valid = False
                            break
                        if cur[y, x] != prev[sy, sx]:
                            valid = False
                            break
                    if not valid:
                        break
                oracle_mask[r, c] = valid

        np.testing.assert_array_equal(ct, oracle_mask)


if __name__ == '__main__':
    unittest.main()
