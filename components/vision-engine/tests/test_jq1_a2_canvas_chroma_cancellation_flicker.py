"""Regression test for DEF-A2-03: non-repeating canvas texture mislabelled FLICKER by perceptual lag-two return."""
import unittest
import numpy as np

from visioncore.change import (
    ClassState,
    classify_first,
    classify_tiles,
    residual,
    sig_conf,
    thresholds,
    C_UNCHANGED,
    C_CHANGED,
    C_FLICKER,
)
from visioncore.desc import describe_padded, luma_of_padded
from visioncore.grid import Grid


def noise_tile(w, h, seed, ox=0, oy=0):
    """Exact texture band and noise generator from eval/gui_synth/raster.py without text mask."""
    x = (np.arange(w, dtype=np.uint32) + np.uint32(ox & 0xFFFFFFFF))[None, :]
    y = (np.arange(h, dtype=np.uint32) + np.uint32(oy & 0xFFFFFFFF))[:, None]
    z = (x * np.uint32(374761393)) ^ (y * np.uint32(668265263)) ^ np.uint32(seed & 0xFFFFFFFF)
    z = (z ^ (z >> np.uint32(13))) * np.uint32(1274126177)
    z ^= z >> np.uint32(16)
    np.bitwise_and(z, 31, out=z)
    grey = z.astype(np.uint8)
    bands = ((x // 8) % 3).astype(np.uint8) + ((y // 13) % 3).astype(np.uint8)
    np.subtract(bands, 3, out=bands, where=bands >= 3)
    bands *= 23
    grey += bands
    grey += 45
    a = np.empty((h, w, 3), np.uint8)
    a[:, :, 0] = grey
    np.add(grey, 12, out=a[:, :, 1])
    np.add(grey, 24, out=a[:, :, 2])
    return a


class TestCanvasChromaCancellationFlicker(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(16, 16)
        self.t_enter, self.t_exit, _ = thresholds()

    def test_chroma_cancellation_precondition(self):
        """Verify describe_padded gives co_h == -12 and cg_h == 0 for independent fixed-seed noise fields."""
        f0 = noise_tile(16, 16, seed=10)
        f1 = noise_tile(16, 16, seed=20)

        p0 = self.grid.pad(f0)
        p1 = self.grid.pad(f1)

        d0, _ = describe_padded(p0, self.grid)
        d1, _ = describe_padded(p1, self.grid)

        self.assertEqual(d0["co_h"][0], -12)
        self.assertEqual(d0["cg_h"][0], 0)
        self.assertEqual(d1["co_h"][0], -12)
        self.assertEqual(d1["cg_h"][0], 0)

    def test_three_frame_witness_preconditions(self):
        """Verify 3-frame witness (seeds 0, 1, 2) has distinct RGB, distinct eager xh64, r1 > t_enter, r2 <= t_exit."""
        seeds = (0, 1, 2)
        frames = [noise_tile(16, 16, s) for s in seeds]

        # Verify pairwise distinct RGB bytes
        self.assertFalse(np.array_equal(frames[0], frames[1]))
        self.assertFalse(np.array_equal(frames[1], frames[2]))
        self.assertFalse(np.array_equal(frames[0], frames[2]))

        pads = [self.grid.pad(f) for f in frames]
        lumas = [luma_of_padded(p) for p in pads]

        d0, w0 = describe_padded(pads[0], self.grid)
        d1, w1 = describe_padded(pads[1], self.grid, words_prev=pads[0], prev_tiles=d0)
        d2, w2 = describe_padded(pads[2], self.grid, words_prev=pads[1], prev_tiles=d1)

        # Verify pairwise distinct eager xh64 hashes
        hashes = {d0["xh64"][0], d1["xh64"][0], d2["xh64"][0]}
        self.assertEqual(len(hashes), 3)

        cf0 = sig_conf(lumas[0], self.grid.s, self.grid.gw, self.grid.gh, d0["morton"], 24)
        cf1 = sig_conf(lumas[1], self.grid.s, self.grid.gw, self.grid.gh, d1["morton"], 24)
        cf2 = sig_conf(lumas[2], self.grid.s, self.grid.gw, self.grid.gh, d2["morton"], 24)

        r1 = residual(d2, d1, mask=cf2 | cf1)[0]
        r2 = residual(d2, d0, mask=cf2 | cf0)[0]

        # Pin distance preconditions
        self.assertGreater(r1, self.t_enter)
        self.assertLessEqual(r2, self.t_exit)

    @unittest.expectedFailure
    def test_def_a2_03_canvas_flicker_regression(self):
        """DEF-A2-03: Non-repeating canvas texture mislabelled FLICKER by perceptual lag-two return.

        Frame 2 should be classified as CHANGED (numeric class 2), not FLICKER (numeric class 4).
        Note: Hash inequality is witness evidence here, not a universal rule that every changed hash forbids real blinking.
        """
        seeds = (0, 1, 2)
        frames = [noise_tile(16, 16, s) for s in seeds]
        pads = [self.grid.pad(f) for f in frames]

        d0, w0 = describe_padded(pads[0], self.grid)
        d1, w1 = describe_padded(pads[1], self.grid, words_prev=pads[0], prev_tiles=d0)
        d2, w2 = describe_padded(pads[2], self.grid, words_prev=pads[1], prev_tiles=d1)

        st = ClassState.init(d0)
        o0, st, _ = classify_first(st, d0)
        o1, st, _ = classify_tiles(st, d1, w1)
        o2, st, _ = classify_tiles(st, d2, w2)

        self.assertEqual(o0["cls"][0], C_UNCHANGED)
        self.assertEqual(o1["cls"][0], C_CHANGED)
        # DEF-A2-03: frame 2 is currently falsely mislabelled FLICKER (4).
        # We assert the correct class C_CHANGED (2).
        self.assertEqual(o2["cls"][0], C_CHANGED)


if __name__ == "__main__":
    unittest.main()
