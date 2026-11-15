"""tests/test_fx2_f5_j1_gm_bands_hv_flat_and_micro_scroll.py

Unit tests for sparse-page motion under noncontiguous memory layouts and luma complementation.
Owned by fix task_id: fx2-f5-gm-bands-hv-v21-and-constellation.

Why noncontiguous memory layouts and luma complementation expose a separate failure mode:
1. Noncontiguous arrays (e.g. Fortran-order transposed storage with non-unit stride in the leading
   dimension, or sliced views from larger padded arrays) test buffer indexing, memory strides, and
   C-contiguous assumptions in sub-band sampling or vectorised SAD calculations.
2. Luma complementation (255 - luma) tests that estimation algorithms do not rely on implicit luma
   thresholds, background assumptions (e.g., assuming white background vs dark background), or unsigned
   byte arithmetic overflow/underflow when gradients are inverted.
3. Exercising dy = -4, -8, -16, -200, -960 across all three layouts and complement transformations
   verifies that sparse-glyph field detection is robust against layout variations and background polarity,
   which is a distinct failure mode from single canonical -4, -960, or repeated-row fixtures in existing pins.
"""

import unittest
import numpy as np


def _make_layouts(arr):
    """Return identical logical pixels presented as (C-contiguous, Fortran/transposed, sliced-padded)."""
    H, W = arr.shape
    c_arr = np.ascontiguousarray(arr)
    f_arr = np.asfortranarray(arr)
    padded = np.zeros((H + 64, W + 64), dtype=arr.dtype)
    padded[32:32 + H, 16:16 + W] = arr
    sliced_arr = padded[32:32 + H, 16:16 + W]
    return [
        ("c_contiguous", c_arr),
        ("fortran_transposed", f_arr),
        ("sliced_padded", sliced_arr),
    ]


def _generate_sparse_pair(H=1080, W=1920, dy=-4, bg=240, complement=False):
    """Generate prev and cur frames with deterministic sparse glyph fields (<1% of frame).

    Distributed across multiple tile rows and columns and retained inside observable overlap.
    """
    effective_bg = 255 - bg if complement else bg
    prev = np.full((H, W), effective_bg, dtype=np.uint8)
    cur = np.full((H, W), effective_bg, dtype=np.uint8)

    # Calculate valid prev row bounds such that both prev[pr:pr+16] and cur[pr+dy:pr+dy+16]
    # are within [0, H]
    pr_min = max(0, -dy)
    pr_max = min(H - 16, H - 16 - dy)

    rng = np.random.default_rng(42 + abs(dy) + (100 if complement else 0))

    # Place 10 sparse glyphs (16x16 each = 2560 pixels total, ~0.12% of 1080x1920)
    prs = np.linspace(pr_min, pr_max, 5, dtype=int)
    cols = [150, 550, 950, 1350, 1750]

    for pr in prs:
        for c in cols[:2]:
            cr = pr + dy
            glyph = rng.integers(10, 200, size=(16, 16), dtype=np.uint8)
            if complement:
                glyph = 255 - glyph
            prev[pr:pr + 16, c:c + 16] = glyph
            cur[cr:cr + 16, c:c + 16] = glyph

    return prev, cur


def _compute_reference_overlap_sad(prev, cur, dx, dy):
    """Compute exact SAD on the overlapping rectangle using pixel loops."""
    H, W = cur.shape
    cy_start, cy_end = max(0, dy), min(H, H + dy)
    py_start, py_end = cy_start - dy, cy_end - dy
    cx_start, cx_end = max(0, dx), min(W, W + dx)
    px_start, px_end = cx_start - dx, cx_end - dx

    c_sub = cur[cy_start:cy_end, cx_start:cx_end]
    p_sub = prev[py_start:py_end, px_start:px_end]

    sad = 0
    sub_h, sub_w = c_sub.shape
    for y in range(sub_h):
        for x in range(sub_w):
            sad += abs(int(c_sub[y, x]) - int(p_sub[y, x]))
    return sad


def _compute_oracle_mask(prev, cur, s=16, dx=0, dy=0):
    """Compute independent pixel oracle mask for consensus_tiles using pixel loops."""
    H, W = cur.shape
    gh = (H + s - 1) // s
    gw = (W + s - 1) // s
    oracle_mask = np.zeros((gh, gw), dtype=bool)

    for r in range(gh):
        for c in range(gw):
            y0, y1 = r * s, min((r + 1) * s, H)
            x0, x1 = c * s, min((c + 1) * s, W)

            # Check if tile changed in place
            changed = False
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if cur[y, x] != prev[y, x]:
                        changed = True
                        break
                if changed:
                    break

            if not changed:
                oracle_mask[r, c] = False
                continue

            # Verify valid translated correspondence across all pixels in tile
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

            oracle_mask[r, c] = match

    return oracle_mask


class TestFx2F5J1GmBandsHvFlatAndMicroScroll(unittest.TestCase):
    """Test suite for sparse-page motion, layout independence, and luma complementation."""

    # Fix owned by task_id: fx2-f5-gm-bands-hv-v21-and-constellation
    @unittest.expectedFailure
    def test_sparse_page_motion_and_layouts_and_complements(self):
        """Exercise dy in [-4, -8, -16, -200, -960] across layouts and complements.

        Owned by fix task_id: fx2-f5-gm-bands-hv-v21-and-constellation.
        Exposes sparse-page motion detection gaps under noncontiguous layouts and complementation.
        """
        from eval.v2x import gm_bands_hv

        dy_list = [-4, -8, -16, -200, -960]
        max_d = 1100

        for dy in dy_list:
            for complement in [False, True]:
                prev_base, cur_base = _generate_sparse_pair(dy=dy, complement=complement)

                # Reference overlap SAD must be exactly zero for true motion (0, dy)
                ref_sad = _compute_reference_overlap_sad(prev_base, cur_base, dx=0, dy=dy)
                self.assertEqual(ref_sad, 0, f"Reference overlap SAD must be 0 for dy={dy}")

                # Reference oracle tile mask
                oracle_mask = _compute_oracle_mask(prev_base, cur_base, s=16, dx=0, dy=dy)

                p_layouts = _make_layouts(prev_base)
                c_layouts = _make_layouts(cur_base)

                canonical_mask = None

                for (p_label, p_arr), (c_label, c_arr) in zip(p_layouts, c_layouts):
                    # Save copies to verify input buffers unchanged
                    p_orig = p_arr.copy()
                    c_orig = c_arr.copy()

                    res = gm_bands_hv.estimate_global_motion(p_arr, c_arr, max_d=max_d)

                    # Assert input buffers unchanged
                    np.testing.assert_array_equal(p_arr, p_orig, f"Input prev modified for {p_label}")
                    np.testing.assert_array_equal(c_arr, c_orig, f"Input cur modified for {c_label}")

                    self.assertIsNotNone(
                        res,
                        f"estimate_global_motion returned None for dy={dy}, complement={complement}, layout={p_label}"
                    )
                    dx_est, dy_est, score_est = res

                    # Require exact integer motion tuples with zero score
                    self.assertIsInstance(dx_est, (int, np.integer))
                    self.assertIsInstance(dy_est, (int, np.integer))
                    self.assertIsInstance(score_est, (int, np.integer))

                    self.assertEqual(
                        (int(dx_est), int(dy_est), int(score_est)),
                        (0, dy, 0),
                        f"Motion mismatch for dy={dy}, complement={complement}, layout={p_label}"
                    )

                    # consensus_tiles mask check
                    mask = gm_bands_hv.consensus_tiles(p_arr, c_arr, s=16, dx=0, dy=dy)
                    np.testing.assert_array_equal(
                        mask,
                        oracle_mask,
                        err_msg=f"Consensus mask failed oracle check for dy={dy}, layout={p_label}"
                    )

                    if canonical_mask is None:
                        canonical_mask = mask.copy()
                    else:
                        np.testing.assert_array_equal(
                            mask,
                            canonical_mask,
                            err_msg=f"Consensus mask byte mismatch across layouts for dy={dy}, layout={p_label}"
                        )

    def test_periodic_sparse_glyph_refusal(self):
        """Assert refusal for nonidentical periodic sparse-glyph fixture with competing zero-SAD offsets.

        Kills exact-bypass mutant that drops its tie guard across noncontiguous layouts.
        """
        from eval.v2x import gm_bands_hv

        H, W = 1080, 1920
        period = 64
        rng = np.random.default_rng(202)

        # Build 64-row periodic sparse strip
        sparse_strip = np.full((period, W), 240, dtype=np.uint8)
        sparse_strip[28:36, 100:300] = rng.integers(10, 200, size=(8, 200), dtype=np.uint8)
        sparse_strip[28:36, 1000:1200] = rng.integers(10, 200, size=(8, 200), dtype=np.uint8)

        prev_base = np.tile(sparse_strip, (H // period + 1, 1))[:H, :]
        cur_base = np.roll(prev_base, -8, axis=0)

        # Prove dy=-8 and dy=56 both have zero reference overlap SAD
        sad_neg8 = _compute_reference_overlap_sad(prev_base, cur_base, 0, -8)
        sad_pos56 = _compute_reference_overlap_sad(prev_base, cur_base, 0, 56)
        self.assertEqual(sad_neg8, 0, "dy=-8 must have zero SAD")
        self.assertEqual(sad_pos56, 0, "dy=56 must have zero SAD")
        self.assertGreaterEqual(abs(-8 - 56), 4, "Offsets must differ by at least 4")

        p_layouts = _make_layouts(prev_base)
        c_layouts = _make_layouts(cur_base)

        for (p_label, p_arr), (c_label, c_arr) in zip(p_layouts, c_layouts):
            p_orig = p_arr.copy()
            c_orig = c_arr.copy()

            res = gm_bands_hv.estimate_global_motion(p_arr, c_arr, max_d=200)

            # Assert input buffers unchanged
            np.testing.assert_array_equal(p_arr, p_orig)
            np.testing.assert_array_equal(c_arr, c_orig)

            # Refusal required due to competing exact zero-SAD offsets
            self.assertIsNone(
                res,
                f"estimate_global_motion must refuse (return None) for periodic tie under layout {p_label}"
            )


if __name__ == "__main__":
    unittest.main()
