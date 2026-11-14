"""RAM-only regressions for the exposure-aware V2EVAL2 subtile variant."""

import unittest

import numpy as np

from eval.v2proto_subtile import subtile_motion as prototype_motion
from eval.v2x.st_exposure import subtile_motion


BACKGROUND = 200
FOREGROUND = 50


def blank_frame(width=128, height=128):
    return np.full((height, width), BACKGROUND, dtype=np.uint8)


def translated(frame, dx, dy):
    """Translate a RAM fixture, clipping its support at the frame boundary."""
    height, width = frame.shape
    result = blank_frame(width, height)
    dst_x0, dst_x1 = max(0, dx), min(width, width + dx)
    dst_y0, dst_y1 = max(0, dy), min(height, height + dy)
    result[dst_y0:dst_y1, dst_x0:dst_x1] = frame[
        dst_y0 - dy:dst_y1 - dy, dst_x0 - dx:dst_x1 - dx
    ]
    return result


class TestV2XStExposure(unittest.TestCase):
    def assert_motion(self, result, dx, dy):
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        for value in result:
            self.assertIs(type(value), int)
        self.assertEqual(result[:2], (dx, dy))
        self.assertNotEqual(result[:2], (0, 0))
        self.assertGreater(result[2], 0)

    def test_glyph_exposure_rejected_but_receiving_tile_claimed(self):
        """The prototype claims an L glyph's mostly exposed original tile."""
        prev = blank_frame()
        prev[8:24, 14:16] = FOREGROUND
        prev[20:24, 14:34] = FOREGROUND
        cur = translated(prev, 7, 0)

        changed = prev[:32, :32] != cur[:32, :32]
        destinations = changed & (cur[:32, :32] != BACKGROUND)
        self.assertEqual(int(np.count_nonzero(changed)), 76)
        self.assertEqual(int(np.count_nonzero(destinations)), 24)
        self.assert_motion(prototype_motion(prev, cur, 32, (0, 0), 16), 7, 0)
        self.assertIsNone(subtile_motion(prev, cur, 32, (0, 0), 16))
        self.assert_motion(subtile_motion(prev, cur, 32, (1, 0), 16), 7, 0)

    def test_thin_caret_accepts_exactly_half_destination_pixels(self):
        """A two-pixel caret displaced seven pixels meets the inclusive half rule."""
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        cur = translated(prev, 7, 0)

        changed = prev[:32, :32] != cur[:32, :32]
        destinations = changed & (cur[:32, :32] != BACKGROUND)
        self.assertEqual(int(np.count_nonzero(changed)), 64)
        self.assertEqual(int(np.count_nonzero(destinations)), 32)
        self.assert_motion(subtile_motion(prev, cur, 32, (0, 0), 16), 7, 0)

    def test_caret_crossing_boundary_leaves_pure_exposed_tile(self):
        prev = blank_frame()
        prev[8:24, 26:28] = FOREGROUND
        cur = translated(prev, 7, 0)

        self.assertTrue(np.all(cur[:32, :32] == BACKGROUND))
        self.assertIsNone(subtile_motion(prev, cur, 32, (0, 0), 16))
        self.assert_motion(subtile_motion(prev, cur, 32, (1, 0), 16), 7, 0)

    def test_odd_changed_count_does_not_round_half_down(self):
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        prev[28, 30] = FOREGROUND
        cur = translated(prev, 7, 0)

        changed = prev[:32, :32] != cur[:32, :32]
        destinations = changed & (cur[:32, :32] != BACKGROUND)
        self.assertEqual(int(np.count_nonzero(changed)), 65)
        self.assertEqual(int(np.count_nonzero(destinations)), 32)
        self.assert_motion(prototype_motion(prev, cur, 32, (0, 0), 16), 7, 0)
        self.assertIsNone(subtile_motion(prev, cur, 32, (0, 0), 16))

    def test_changed_blocks_must_match_including_unchanged_pixels(self):
        """Matching only changed pixels would miss stationary content in a block."""
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        cur = translated(prev, 7, 0)
        prev[10, 12] = 131
        cur[10, 12] = 131

        shifted_prev = translated(prev, 7, 0)
        changed = prev[:32, :32] != cur[:32, :32]
        np.testing.assert_array_equal(
            cur[:32, :32][changed], shifted_prev[:32, :32][changed]
        )
        self.assertFalse(np.array_equal(cur[8:16, 8:16], shifted_prev[8:16, 8:16]))
        self.assertIsNone(subtile_motion(prev, cur, 32, (0, 0), 16))

    def test_textured_window_drag_forward_and_reverse(self):
        rng = np.random.default_rng(9173)
        prev = blank_frame()
        prev[32:96, 32:96] = rng.integers(20, 180, (64, 64), dtype=np.uint8)
        cur = translated(prev, 7, 5)

        self.assert_motion(subtile_motion(prev, cur, 32, (2, 2), 16), 7, 5)
        self.assert_motion(subtile_motion(cur, prev, 32, (2, 2), 16), -7, -5)

    def test_partial_right_bottom_and_corner_tiles(self):
        """Both explicit tile sizes cover clipped blocks on a 1000x563 frame."""
        width, height = 1000, 563
        cases = (
            ("right", (980, 164), (7, 0)),
            ("bottom", (324, 548), (0, 7)),
            ("corner", (980, 547), (7, 7)),
        )
        for index, (name, (x, y), (dx, dy)) in enumerate(cases):
            rng = np.random.default_rng(28100 + index)
            prev = blank_frame(width, height)
            prev[y:y + 12, x:x + 16] = rng.integers(
                20, 180, (12, 16), dtype=np.uint8
            )
            cur = translated(prev, dx, dy)
            for s in (16, 32):
                tx = (width - 1) // s if dx else x // s
                ty = (height - 1) // s if dy else y // s
                with self.subTest(edge=name, tile_size=s):
                    self.assert_motion(
                        subtile_motion(prev, cur, s, (tx, ty), s - 1), dx, dy
                    )

    def test_last_pixel_of_partial_corner_block_must_be_explained(self):
        rng = np.random.default_rng(28102)
        prev = blank_frame(1000, 563)
        prev[547:559, 980:996] = rng.integers(20, 180, (12, 16), dtype=np.uint8)
        cur = translated(prev, 7, 7)
        self.assertFalse(np.any(prev == 255))
        cur[-1, -1] = 255

        for s in (16, 32):
            with self.subTest(tile_size=s):
                self.assertIsNone(subtile_motion(prev, cur, s, (999 // s, 562 // s), s - 1))

    def test_identical_uniform_and_textured_frames(self):
        rng = np.random.default_rng(74129)
        frames = (
            blank_frame(),
            rng.integers(0, 256, (128, 128), dtype=np.uint8),
        )
        for name, frame in zip(("uniform", "texture"), frames):
            with self.subTest(frame=name):
                self.assertIsNone(subtile_motion(frame, frame.copy(), 32, (1, 1), 16))

    def test_unchanged_target_next_to_motion(self):
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        cur = translated(prev, 7, 0)
        np.testing.assert_array_equal(prev[:32, 32:64], cur[:32, 32:64])
        self.assertIsNone(subtile_motion(prev, cur, 32, (1, 0), 16))

    def test_readonly_input_frames_are_not_modified(self):
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        cur = translated(prev, 7, 0)
        expected_prev, expected_cur = prev.copy(), cur.copy()
        prev.flags.writeable = False
        cur.flags.writeable = False

        self.assert_motion(subtile_motion(prev, cur, 32, (0, 0), 16), 7, 0)
        np.testing.assert_array_equal(prev, expected_prev)
        np.testing.assert_array_equal(cur, expected_cur)

    def test_default_tile_size_bounds_and_absent_motion(self):
        prev = blank_frame()
        prev[8:24, 8:10] = FOREGROUND
        cur = translated(prev, 7, 0)

        self.assert_motion(subtile_motion(prev, cur, None, (1, 0), 15), 7, 0)
        for tile in ((-1, 0), (0, -1), (4, 0), (0, 4)):
            with self.subTest(tile=tile):
                self.assertIsNone(subtile_motion(prev, cur, 32, tile, 16))
        self.assertIsNone(subtile_motion(prev, cur, 32, (0, 0), 0))
        self.assertIsNone(subtile_motion(None, cur, 32, (0, 0), 16))


if __name__ == "__main__":
    unittest.main()
