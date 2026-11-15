"""tests/test_fx2_f5_j2_gm_chrome_clock_and_competing_motions.py

Test suite for top-vs-bottom chrome symmetry, sequential state-leak independence,
clock span and separation boundary sweeps, and competing motion runner vetoes.
"""

import unittest
import numpy as np

from eval.v2x.gm_bands_hv import estimate_global_motion, consensus_tiles, reset_ops


def _build_chrome_clock_scene(bottom=False, rng_seed=2025):
    """Build a deterministic scrolling-body scene with flat header/footer and a clock.

    Dimensions: H=256, W=320 (grid-aligned to 16x16 tiles).
    Header: rows 0..31 (flat luma 60).
    Footer: rows 224..255 (flat luma 60).
    Body: rows 32..223 (192 rows = 12 tile rows).
    If bottom is False:
        - dy = -16 (scroll up 16 rows).
        - Clock in cur at rows 8..16, cols 10..42 (span width 32 <= W // 8 = 40).
    If bottom is True:
        - Vertically reflected partner.
        - dy = +16 (scroll down 16 rows).
        - Clock in cur at rows 240..248, cols 10..42 (span width 32 <= W // 8 = 40).
    """
    H, W = 256, 320
    rng = np.random.default_rng(rng_seed)

    prev = np.empty((H, W), dtype=np.uint8)
    cur = np.empty((H, W), dtype=np.uint8)

    # Header and Footer (32 rows each)
    header_footer_luma = 60
    prev[:32, :] = header_footer_luma
    cur[:32, :] = header_footer_luma
    prev[224:, :] = header_footer_luma
    cur[224:, :] = header_footer_luma

    # Body (rows 32..223, 192 rows)
    body_prev = rng.integers(0, 256, size=(192, W), dtype=np.uint8)
    prev[32:224, :] = body_prev

    if not bottom:
        # Scroll up 16 rows (dy = -16)
        # cur[32:208] gets prev[48:224]
        cur[32:208, :] = prev[48:224, :]
        cur[208:224, :] = rng.integers(0, 256, size=(16, W), dtype=np.uint8)
        # Compact top clock change in header (rows 8..16, cols 10..42, span=32)
        cur[8:16, 10:42] ^= 255
    else:
        # Scroll down 16 rows (dy = +16)
        # cur[48:224] gets prev[32:208]
        cur[48:224, :] = prev[32:208, :]
        cur[32:48, :] = rng.integers(0, 256, size=(16, W), dtype=np.uint8)
        # Compact bottom clock change in footer (rows 240..248, cols 10..42, span=32)
        cur[240:248, 10:42] ^= 255

    return prev, cur


def _build_span_sweep_scene(clock_span, clock_row=8, broad_start_row=80, rng_seed=3030):
    """Build a frame pair with a specific clock span and interior broad change starting row.

    Dimensions: H=256, W=320.
    Header: rows 0..31 (flat 60).
    Footer: rows 224..255 (flat 60).
    Body scroll: dy = -16.
    Clock change in cur at clock_row, cols 10..(10 + clock_span).
    Broad interior changes start at broad_start_row.
    """
    H, W = 256, 320
    rng = np.random.default_rng(rng_seed)

    prev = np.full((H, W), 60, dtype=np.uint8)
    cur = np.full((H, W), 60, dtype=np.uint8)

    # Textured body starting at row 32 up to 224
    body = rng.integers(0, 256, size=(192, W), dtype=np.uint8)
    prev[32:224, :] = body
    cur[32:208, :] = prev[48:224, :]
    cur[208:224, :] = rng.integers(0, 256, size=(16, W), dtype=np.uint8)

    # Broad interior change control if broad_start_row != 32
    if broad_start_row > 32:
        # Make rows 32..broad_start_row static
        prev[32:broad_start_row, :] = 60
        cur[32:broad_start_row, :] = 60

    # Clock change
    cur[clock_row:clock_row + 8, 10:10 + clock_span] ^= 255

    return prev, cur


def _build_competing_motions_scene(n1_tiles, n2_tiles, reflected=False, rng_seed=4040):
    """Build a scene with two independently moving disjoint blocks on 16x16 grid.

    Dimensions: H=256, W=320 (16x20 tiles = 320 tiles).
    Background: flat luma 40.

    Primary block B1:
        - Consists of n1_tiles 16x16 tiles.
        - Vector v1 = (16, 0).
        - Placed in top-left tile area: e.g. tile rows 2..3 (Y=32..64), tile cols 2..(2 + n1_tiles//2).
    Runner block B2:
        - Consists of n2_tiles 16x16 tiles.
        - Vector v2 = (0, -16) if not reflected else (0, 16).
        - Placed in bottom-right tile area: e.g. tile rows 10..11 (Y=160..192), tile cols 10..(10 + n2_tiles//2).
    """
    H, W = 256, 320
    rng = np.random.default_rng(rng_seed)

    prev = np.full((H, W), 40, dtype=np.uint8)
    cur = prev.copy()

    # Primary block B1: n1_tiles on 16x16 grid
    r1_cols = 4
    y1_start, x1_start = 32, 32  # tile row 2, tile col 2
    for t in range(n1_tiles):
        tr = t // r1_cols
        tc = t % r1_cols
        ty = y1_start + tr * 16
        tx = x1_start + tc * 16
        b1_tile = rng.integers(0, 256, size=(16, 16), dtype=np.uint8)
        prev[ty:ty + 16, tx:tx + 16] = b1_tile
        # Primary vector v1 = (16, 0)
        cur[ty:ty + 16, tx + 16:tx + 32] = b1_tile

    # Runner block B2: n2_tiles on 16x16 grid
    r2_cols = 4
    y2_start, x2_start = 160, 160  # tile row 10, tile col 10
    dy2 = -16 if not reflected else 16
    for t in range(n2_tiles):
        tr = t // r2_cols
        tc = t % r2_cols
        ty = y2_start + tr * 16
        tx = x2_start + tc * 16
        b2_tile = rng.integers(0, 256, size=(16, 16), dtype=np.uint8)
        prev[ty:ty + 16, tx:tx + 16] = b2_tile
        # Runner vector v2 = (0, dy2)
        cur[ty + dy2:ty + dy2 + 16, tx:tx + 16] = b2_tile

    return prev, cur


class TestChromeClockAndCompetingMotions(unittest.TestCase):

    def setUp(self):
        reset_ops()

    # fx2-f5-gm-bands-hv-v21-and-constellation
    def test_top_and_bottom_chrome_clock_symmetry(self):
        """Top vs bottom chrome clock symmetry and vertical reflection recovery."""
        # Top clock scene
        p_top, c_top = _build_chrome_clock_scene(bottom=False)
        res_top = estimate_global_motion(p_top, c_top, max_d=64)
        self.assertIsNotNone(res_top, "Top chrome clock scene returned None")
        dx_top, dy_top, score_top = res_top
        self.assertEqual((dx_top, dy_top), (0, -16))
        self.assertIsInstance(score_top, int)

        # Bottom clock scene (reflected partner)
        p_bot, c_bot = _build_chrome_clock_scene(bottom=True)
        res_bot = estimate_global_motion(p_bot, c_bot, max_d=64)
        self.assertIsNotNone(res_bot, "Bottom chrome clock scene returned None")
        dx_bot, dy_bot, score_bot = res_bot
        self.assertEqual((dx_bot, dy_bot), (0, 16))
        self.assertIsInstance(score_bot, int)

        # Consensus tiles safety assertions
        c_mask_top = consensus_tiles(p_top, c_top, 16, 0, -16)
        # Header (rows 0..31 -> tile rows 0..1) must have no claims
        self.assertFalse(np.any(c_mask_top[:2, :]), "Top header tiles must be False")
        # Footer (rows 224..255 -> tile rows 14..15) must have no claims
        self.assertFalse(np.any(c_mask_top[14:, :]), "Top footer tiles must be False")

        c_mask_bot = consensus_tiles(p_bot, c_bot, 16, 0, 16)
        self.assertFalse(np.any(c_mask_bot[:2, :]), "Bottom header tiles must be False")
        self.assertFalse(np.any(c_mask_bot[14:, :]), "Bottom footer tiles must be False")

    # fx2-f5-gm-bands-hv-v21-and-constellation
    def test_sequential_state_leak_independence(self):
        """Unchanged results when a refused two-motion scene is evaluated between repeated calls."""
        p_top, c_top = _build_chrome_clock_scene(bottom=False)
        p_bot, c_bot = _build_chrome_clock_scene(bottom=True)
        p_2m, c_2m = _build_competing_motions_scene(n1_tiles=8, n2_tiles=3)

        # Call 1: top clock scene (must recover (0, -16))
        res_top1 = estimate_global_motion(p_top, c_top, max_d=64)
        self.assertIsNotNone(res_top1)
        self.assertEqual(res_top1[:2], (0, -16))

        # Intervening call: refused two-motion scene
        res_2m = estimate_global_motion(p_2m, c_2m, max_d=64)
        self.assertIsNone(res_2m, "Two-motion scene must return None")

        # Call 2: top clock scene again
        res_top2 = estimate_global_motion(p_top, c_top, max_d=64)
        self.assertEqual(res_top1, res_top2, "State leaked between top clock evaluations")

        # Call 1: bottom clock scene (must recover (0, 16))
        res_bot1 = estimate_global_motion(p_bot, c_bot, max_d=64)
        self.assertIsNotNone(res_bot1)
        self.assertEqual(res_bot1[:2], (0, 16))

        # Intervening call: refused two-motion scene
        res_2m_2 = estimate_global_motion(p_2m, c_2m, max_d=64)
        self.assertIsNone(res_2m_2, "Two-motion scene must return None")

        # Call 2: bottom clock scene again
        res_bot2 = estimate_global_motion(p_bot, c_bot, max_d=64)
        self.assertEqual(res_bot1, res_bot2, "State leaked between bottom clock evaluations")

    # fx2-f5-gm-bands-hv-v21-and-constellation
    def test_clock_span_and_separation_sweep(self):
        """Sweep clock span just below, at, and above W//8 and edge-interior separation boundaries."""
        W = 320

        # Span = 38 (below W//8 = 40)
        p38, c38 = _build_span_sweep_scene(clock_span=38)
        res38 = estimate_global_motion(p38, c38, max_d=64)
        self.assertIsNotNone(res38)
        self.assertEqual(res38[:2], (0, -16))

        # Span = 40 (at W//8 = 40)
        p40, c40 = _build_span_sweep_scene(clock_span=40)
        res40 = estimate_global_motion(p40, c40, max_d=64)
        self.assertIsNotNone(res40)
        self.assertEqual(res40[:2], (0, -16))

        # Span = 42 (above W//8 = 40) - distractor stays unmasked, but body scroll recovered by support
        p42, c42 = _build_span_sweep_scene(clock_span=42)
        res42 = estimate_global_motion(p42, c42, max_d=64)
        self.assertIsNotNone(res42)
        self.assertEqual(res42[:2], (0, -16))

        # Separation boundary: edge depth = min(64, 256//4) = 64
        # Test clock at row 63 vs row 64 with broad interior starting at row 80
        # Row 63: y=63 < 64 and y+16 = 79 < 80 -> separated!
        p_sep, c_sep = _build_span_sweep_scene(clock_span=32, clock_row=63, broad_start_row=80)
        res_sep = estimate_global_motion(p_sep, c_sep, max_d=64)
        self.assertIsNotNone(res_sep)
        self.assertEqual(res_sep[:2], (0, -16))

        # Row 64: y=64 >= 64 -> not separated!
        p_unsep, c_unsep = _build_span_sweep_scene(clock_span=32, clock_row=64, broad_start_row=80)
        res_unsep = estimate_global_motion(p_unsep, c_unsep, max_d=64)
        self.assertIsNotNone(res_unsep)
        self.assertEqual(res_unsep[:2], (0, -16))

    def test_competing_motions_capped_tile_support_vetoes(self):
        """Unequal competing blocks on disjoint layout with calculated capped tile support."""
        # Tile weight cap = 128 per 16x16 tile

        # Case 1: n1_tiles = 8 (Primary), n2_tiles = 3 (Runner)
        # Predicted support calculations:
        # Tile weight per tile = min(128, 256) = 128
        # N_changed = (8 + 3) * 128 = 1408
        # P_primary = 8 * 128 = 1024
        # Primary ppt = 1024 * 1000 // 1408 = 727 ppt (>= 600 ppt acceptance threshold)
        # R_runner = 3 * 128 = 384
        # Runner ppt = 384 * 1000 // 1408 = 272 ppt (>= 250 ppt veto threshold)
        # Ratio check: P_primary (1024) < 3 * R_runner (1152) -> triggers 3x veto!
        pred_n_changed_1 = (8 + 3) * 128
        pred_p_primary_1 = 8 * 128
        pred_r_runner_1 = 3 * 128
        primary_ppt_1 = (pred_p_primary_1 * 1000) // pred_n_changed_1
        runner_ppt_1 = (pred_r_runner_1 * 1000) // pred_n_changed_1

        self.assertEqual(pred_n_changed_1, 1408)
        self.assertEqual(pred_p_primary_1, 1024)
        self.assertEqual(pred_r_runner_1, 384)
        self.assertEqual(primary_ppt_1, 727)
        self.assertEqual(runner_ppt_1, 272)
        self.assertGreaterEqual(primary_ppt_1, 600)
        self.assertGreaterEqual(runner_ppt_1, 250)
        self.assertLess(pred_p_primary_1, 3 * pred_r_runner_1)

        # Original layout refusal assertion
        p_c1, c_c1 = _build_competing_motions_scene(n1_tiles=8, n2_tiles=3, reflected=False)
        self.assertIsNone(estimate_global_motion(p_c1, c_c1, max_d=64))

        # Reflected layout refusal assertion
        p_c1_r, c_c1_r = _build_competing_motions_scene(n1_tiles=8, n2_tiles=3, reflected=True)
        self.assertIsNone(estimate_global_motion(p_c1_r, c_c1_r, max_d=64))

        # Case 2: Boundary Case - n1_tiles = 9, n2_tiles = 3
        # N_changed = (9 + 3) * 128 = 1536
        # P_primary = 9 * 128 = 1152
        # Primary ppt = 1152 * 1000 // 1536 = 750 ppt (>= 600 ppt)
        # R_runner = 3 * 128 = 384
        # Runner ppt = 384 * 1000 // 1536 = 250 ppt (>= 250 ppt triggers 250-ppt veto!)
        # Ratio check: P_primary (1152) == 3 * R_runner (1152) -> exactly 3x, passes ratio but fails 250 ppt veto
        pred_n_changed_2 = (9 + 3) * 128
        pred_p_primary_2 = 9 * 128
        pred_r_runner_2 = 3 * 128
        primary_ppt_2 = (pred_p_primary_2 * 1000) // pred_n_changed_2
        runner_ppt_2 = (pred_r_runner_2 * 1000) // pred_n_changed_2

        self.assertEqual(pred_n_changed_2, 1536)
        self.assertEqual(pred_p_primary_2, 1152)
        self.assertEqual(pred_r_runner_2, 384)
        self.assertEqual(primary_ppt_2, 750)
        self.assertEqual(runner_ppt_2, 250)
        self.assertGreaterEqual(runner_ppt_2, 250)

        p_c2, c_c2 = _build_competing_motions_scene(n1_tiles=9, n2_tiles=3, reflected=False)
        self.assertIsNone(estimate_global_motion(p_c2, c_c2, max_d=64))

        p_c2_r, c_c2_r = _build_competing_motions_scene(n1_tiles=9, n2_tiles=3, reflected=True)
        self.assertIsNone(estimate_global_motion(p_c2_r, c_c2_r, max_d=64))

        # Case 3: Passing Paired Fixture - n1_tiles = 10, n2_tiles = 2
        # N_changed = (10 + 2) * 128 = 1536
        # P_primary = 10 * 128 = 1280
        # Primary ppt = 1280 * 1000 // 1536 = 833 ppt (>= 600 ppt)
        # R_runner = 2 * 128 = 256
        # Runner ppt = 256 * 1000 // 1536 = 166 ppt (< 250 ppt)
        # Ratio check: P_primary (1280) >= 3 * R_runner (768) -> passes ratio!
        pred_n_changed_3 = (10 + 2) * 128
        pred_p_primary_3 = 10 * 128
        pred_r_runner_3 = 2 * 128
        primary_ppt_3 = (pred_p_primary_3 * 1000) // pred_n_changed_3
        runner_ppt_3 = (pred_r_runner_3 * 1000) // pred_n_changed_3

        self.assertEqual(pred_n_changed_3, 1536)
        self.assertEqual(pred_p_primary_3, 1280)
        self.assertEqual(pred_r_runner_3, 256)
        self.assertEqual(primary_ppt_3, 833)
        self.assertEqual(runner_ppt_3, 166)
        self.assertLess(runner_ppt_3, 250)
        self.assertGreaterEqual(pred_p_primary_3, 3 * pred_r_runner_3)

        # Must recover known primary vector (16, 0)
        p_pass, c_pass = _build_competing_motions_scene(n1_tiles=10, n2_tiles=2, reflected=False)
        res_pass = estimate_global_motion(p_pass, c_pass, max_d=64)
        self.assertIsNotNone(res_pass)
        self.assertEqual(res_pass[:2], (16, 0))

        p_pass_r, c_pass_r = _build_competing_motions_scene(n1_tiles=10, n2_tiles=2, reflected=True)
        res_pass_r = estimate_global_motion(p_pass_r, c_pass_r, max_d=64)
        self.assertIsNotNone(res_pass_r)
        self.assertEqual(res_pass_r[:2], (16, 0))


if __name__ == "__main__":
    unittest.main()
