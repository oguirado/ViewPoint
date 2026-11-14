"""RAM-only regression tests for the multi-model global-motion variant."""

import math
import unittest

import numpy as np

from eval.v2x import gm_multimodel, gm_rowhash


def _move_band(prev, cur, rng, start, stop, dx=0, dy=0):
    """Translate a band, filling newly exposed pixels with independent noise."""
    width = prev.shape[1]
    cur[start:stop] = rng.integers(
        0, 256, (stop - start, width), dtype=np.uint8
    )
    y0, y1 = max(start, start + dy), min(stop, stop + dy)
    x0, x1 = max(0, dx), min(width, width + dx)
    if y0 < y1 and x0 < x1:
        cur[y0:y1, x0:x1] = prev[y0 - dy:y1 - dy, x0 - dx:x1 - dx]


def _scene(height, width, *, dx=0, dy=0, header=0, footer=0, seed):
    rng = np.random.default_rng(seed)
    prev = rng.integers(0, 256, (height, width), dtype=np.uint8)
    cur = prev.copy()
    _move_band(prev, cur, rng, header, height - footer, dx, dy)
    return prev, cur


def _freeze(prev, cur):
    prev.setflags(write=False)
    cur.setflags(write=False)
    return prev, cur


def _expected_tiles(prev, cur, s, dx, dy, start, stop):
    """Independent, direct pixel oracle for one known qualifying band."""
    height, width = prev.shape
    expected = np.zeros(
        ((height + s - 1) // s, (width + s - 1) // s), dtype=np.bool_
    )
    for ty in range(expected.shape[0]):
        y0, y1 = ty * s, (ty + 1) * s
        for tx in range(expected.shape[1]):
            x0, x1 = tx * s, (tx + 1) * s
            if y0 < start or y1 > stop or y1 > height or x1 > width:
                continue
            if y0 - dy < 0 or y1 - dy > height:
                continue
            if x0 - dx < 0 or x1 - dx > width:
                continue
            expected[ty, tx] = np.array_equal(
                cur[y0:y1, x0:x1],
                prev[y0 - dy:y1 - dy, x0 - dx:x1 - dx],
            )
    return expected


class TestGMMultimodel(unittest.TestCase):
    def assert_motion(self, prev, cur, expected, max_d, expected_score=None):
        _freeze(prev, cur)
        result = gm_multimodel.estimate_global_motion(prev, cur, max_d)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        dx, dy, score = result
        self.assertIsInstance(dx, (int, np.integer))
        self.assertIsInstance(dy, (int, np.integer))
        self.assertEqual((dx, dy), expected)
        self.assertIsInstance(score, int)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 1_000_000)
        if expected_score is not None:
            self.assertEqual(score, expected_score)
        return result

    def assert_mask_contract(self, mask, height, width, s):
        self.assertIsInstance(mask, np.ndarray)
        self.assertEqual(mask.dtype, np.dtype(np.bool_))
        self.assertEqual(
            mask.shape, ((height + s - 1) // s, (width + s - 1) // s)
        )
        self.assertTrue(mask.flags.c_contiguous)

    def test_middle_scroll_with_static_header_and_footer(self):
        for width, height in ((640, 360), (1920, 1080)):
            for dy in (1, 7, -13):
                with self.subTest(width=width, height=height, dy=dy):
                    prev, cur = _scene(
                        height, width, dy=dy, header=64, footer=64,
                        seed=1000 + height + dy,
                    )
                    self.assert_motion(
                        prev, cur, (0, dy), max(width, height) - 1,
                        (height - 128 - abs(dy)) * 1_000_000 // height,
                    )

    def test_full_frame_scroll(self):
        for dy in (7, -11):
            with self.subTest(dy=dy):
                prev, cur = _scene(160, 256, dy=dy, seed=2000 + dy)
                self.assert_motion(
                    prev, cur, (0, dy), 255, (160 - abs(dy)) * 1_000_000 // 160,
                )

    def test_horizontal_pan(self):
        for dx in (7, -13):
            with self.subTest(dx=dx):
                prev, cur = _scene(96, 160, dx=dx, seed=3000 + dx)
                self.assert_motion(
                    prev, cur, (dx, 0), 159, (160 - abs(dx)) * 1_000_000 // 160,
                )

    def test_pure_static_returns_none_and_no_consensus(self):
        rng = np.random.default_rng(4001)
        prev = rng.integers(0, 256, (96, 160), dtype=np.uint8)
        cur = prev.copy()
        _freeze(prev, cur)
        self.assertIsNone(gm_multimodel.estimate_global_motion(prev, cur, 16))
        mask = gm_multimodel.consensus_tiles(prev, cur, 8, 0, 0)
        self.assert_mask_contract(mask, 96, 160, 8)
        self.assertFalse(mask.any())

    def test_independent_noise_returns_none(self):
        rng = np.random.default_rng(4002)
        prev = rng.integers(0, 256, (128, 192), dtype=np.uint8)
        cur = rng.integers(0, 256, (128, 192), dtype=np.uint8)
        _freeze(prev, cur)
        self.assertIsNone(gm_multimodel.estimate_global_motion(prev, cur, 16))

    def test_rowhash_middle_band_regression(self):
        # Reproduce the bound rowhash test's fixed-seed expected failure.
        rng = np.random.default_rng(2024)
        prev = rng.integers(0, 256, (400, 600), dtype=np.uint8)
        cur = prev.copy()
        cur[120:300] = prev[100:280]
        cur[100:120] = rng.integers(0, 256, (20, 600), dtype=np.uint8)
        _freeze(prev, cur)
        baseline = gm_rowhash.estimate_global_motion(prev, cur, 599)
        self.assertTrue(
            baseline is None or tuple(baseline[:2]) != (0, 20),
            msg="This scene must exhibit the documented rowhash limitation.",
        )
        self.assert_motion(prev, cur, (0, 20), 599, 180 * 1_000_000 // 400)

    def test_consensus_excludes_chrome_boundaries_and_partial_tiles(self):
        for dy in (3, -3):
            with self.subTest(dy=dy):
                prev, cur = _scene(
                    87, 103, dy=dy, header=8, footer=8, seed=5000 + dy,
                )
                self.assert_motion(prev, cur, (0, dy), 6)
                mask = gm_multimodel.consensus_tiles(prev, cur, 8, 0, dy)
                self.assert_mask_contract(mask, 87, 103, 8)
                np.testing.assert_array_equal(
                    mask, _expected_tiles(prev, cur, 8, 0, dy, 8, 79)
                )
                self.assertTrue(mask[2, 0])
                self.assertFalse(mask[0].any())
                self.assertFalse(mask[9:].any())
                self.assertFalse(mask[:, -1].any())

    def test_horizontal_consensus_requires_in_bounds_shifted_source(self):
        for dx in (5, -5):
            with self.subTest(dx=dx):
                prev, cur = _scene(64, 103, dx=dx, seed=5100 + dx)
                self.assert_motion(prev, cur, (dx, 0), 8)
                mask = gm_multimodel.consensus_tiles(prev, cur, 8, dx, 0)
                self.assert_mask_contract(mask, 64, 103, 8)
                np.testing.assert_array_equal(
                    mask, _expected_tiles(prev, cur, 8, dx, 0, 0, 64)
                )
                self.assertTrue(mask[:, 2].all())
                self.assertFalse(mask[:, -1].any())
                if dx > 0:
                    self.assertFalse(mask[:, 0].any())

    def test_corrupted_tile_is_false_without_hiding_its_neighbor(self):
        prev, cur = _scene(
            160, 192, dy=3, header=16, footer=16, seed=5201,
        )
        cur[59, 43] ^= np.uint8(1)
        self.assert_motion(prev, cur, (0, 3), 6, 124 * 1_000_000 // 160)
        mask = gm_multimodel.consensus_tiles(prev, cur, 8, 0, 3)
        self.assert_mask_contract(mask, 160, 192, 8)
        np.testing.assert_array_equal(
            mask, _expected_tiles(prev, cur, 8, 0, 3, 16, 144)
        )
        self.assertFalse(mask[7, 5])
        self.assertTrue(mask[7, 6])

    def test_largest_verified_pixel_model_wins(self):
        rng = np.random.default_rng(6001)
        prev = rng.integers(0, 256, (240, 256), dtype=np.uint8)
        cur = prev.copy()
        _move_band(prev, cur, rng, 16, 96, dy=1)
        # Wrapping preserves this band's row means exactly; verification still
        # counts only the in-bounds horizontal overlap, 192 of 256 columns.
        cur[128:224] = np.roll(prev[128:224], 64, axis=1)
        self.assert_motion(prev[128:224], cur[128:224], (64, 0), 64, 192 * 1_000_000 // 256)
        # The 80-row band's 79*256 pixels beat the 96-row band's 96*192.
        self.assert_motion(prev, cur, (0, 1), 64, 79 * 1_000_000 // 240)
        mask = gm_multimodel.consensus_tiles(prev, cur, 8, 0, 1)
        np.testing.assert_array_equal(
            mask, _expected_tiles(prev, cur, 8, 0, 1, 16, 96)
        )
        self.assertFalse(mask[12:].any())
        self.assertTrue(mask[3:12].all())

    def test_verification_threshold_counts_all_original_run_rows(self):
        # Exposed rows count against the original 20-row run's threshold.
        for dy, accepted in ((2, True), (3, False)):
            with self.subTest(dy=dy):
                prev, cur = _scene(
                    40, 128, dy=dy, header=8, footer=12, seed=7000 + dy,
                )
                if accepted:
                    self.assert_motion(prev, cur, (0, dy), 5, 18 * 1_000_000 // 40)
                else:
                    _freeze(prev, cur)
                    self.assertIsNone(
                        gm_multimodel.estimate_global_motion(prev, cur, 5)
                    )
                    mask = gm_multimodel.consensus_tiles(prev, cur, 4, 0, dy)
                    self.assertFalse(mask.any())

    def test_runs_shorter_than_eight_rows_are_ignored(self):
        for run_height in (7, 8):
            with self.subTest(run_height=run_height):
                prev, cur = _scene(
                    40, 256, dx=1, header=8, footer=32 - run_height,
                    seed=7100 + run_height,
                )
                if run_height == 8:
                    self.assert_motion(prev, cur, (1, 0), 4, 8 * 255 * 1_000_000 // (40 * 256))
                else:
                    _freeze(prev, cur)
                    self.assertIsNone(
                        gm_multimodel.estimate_global_motion(prev, cur, 4)
                    )
                    mask = gm_multimodel.consensus_tiles(prev, cur, 4, 1, 0)
                    self.assertFalse(mask.any())

    def test_equal_gradient_signatures_still_compare_pixels(self):
        prev = np.repeat(
            ((np.arange(40) % 2) * 255).astype(np.uint8)[:, None], 64, axis=1
        )
        cur = np.uint8(255) - prev
        # Every horizontal-gradient signature is zero in both frames.
        # Both +/-1 match; the specified displacement tie-break selects -1.
        self.assert_motion(prev, cur, (0, -1), 3, 39 * 1_000_000 // 40)

    def test_projection_ties_and_mean_error_normalization(self):
        cases = (
            ([7] * 8, [7] * 8, 0),
            ([0, 1] * 4, [1, 0] * 4, -1),
            ([0] * 6, [2, 2, 2, 2, 2, 10], -1),
        )
        for prev_values, cur_values, expected in cases:
            with self.subTest(prev=prev_values, cur=cur_values):
                prev = np.array(prev_values, dtype=np.int64)
                cur = np.array(cur_values, dtype=np.int64)
                self.assertEqual(
                    gm_multimodel._projection_shift(prev, cur, 3), expected
                )

    def test_projection_requires_at_least_half_overlap(self):
        cases = (
            ([1, 4, 9, 16, 25, 36], [100, 101, 102, 103, 1, 4], 3),
            ([1, 4, 9, 16, 25], [101, 102, 103, 1, 4], 2),
        )
        for prev_values, cur_values, expected in cases:
            with self.subTest(length=len(prev_values)):
                prev = np.array(prev_values, dtype=np.int64)
                cur = np.array(cur_values, dtype=np.int64)
                self.assertEqual(
                    gm_multimodel._projection_shift(prev, cur, 100), expected
                )

    def test_read_only_noncontiguous_inputs_remain_unchanged(self):
        prev_dense, cur_dense = _scene(
            96, 160, dx=-3, dy=4, header=16, footer=16, seed=8001,
        )
        prev_backing = np.full((192, 320), 17, dtype=np.uint8)
        cur_backing = np.full((192, 320), 29, dtype=np.uint8)
        prev = prev_backing[::2, ::2]
        cur = cur_backing[::2, ::2]
        prev[:] = prev_dense
        cur[:] = cur_dense
        prev_before = prev_backing.copy()
        cur_before = cur_backing.copy()
        self.assertFalse(prev.flags.c_contiguous)
        self.assertFalse(cur.flags.c_contiguous)
        self.assert_motion(prev, cur, (-3, 4), 8, 60 * 157 * 1_000_000 // (96 * 160))
        mask = gm_multimodel.consensus_tiles(prev, cur, 8, -3, 4)
        self.assert_mask_contract(mask, 96, 160, 8)
        np.testing.assert_array_equal(
            mask, _expected_tiles(prev, cur, 8, -3, 4, 16, 80)
        )
        np.testing.assert_array_equal(prev_backing, prev_before)
        np.testing.assert_array_equal(cur_backing, cur_before)
        self.assertFalse(prev.flags.writeable)
        self.assertFalse(cur.flags.writeable)

    def test_ops_counter_is_integer_monotonic_and_resettable(self):
        prev, cur = _scene(
            64, 96, dy=3, header=8, footer=8, seed=9001,
        )
        _freeze(prev, cur)
        gm_multimodel.reset_ops()
        self.assertIs(type(gm_multimodel.OPS), int)
        self.assertEqual(gm_multimodel.OPS, 0)
        self.assertIsNotNone(gm_multimodel.estimate_global_motion(prev, cur, 6))
        first = gm_multimodel.OPS
        self.assertIs(type(first), int)
        self.assertGreater(first, 0)
        gm_multimodel.consensus_tiles(prev, cur, 8, 0, 3)
        self.assertIs(type(gm_multimodel.OPS), int)
        self.assertGreaterEqual(gm_multimodel.OPS, first)
        gm_multimodel.reset_ops()
        self.assertIs(type(gm_multimodel.OPS), int)
        self.assertEqual(gm_multimodel.OPS, 0)


if __name__ == "__main__":
    unittest.main()
