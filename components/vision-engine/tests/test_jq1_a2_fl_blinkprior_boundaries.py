"""Boundary and edge test suite for FlickerTracker symmetry and overmax resets.

Pins symmetry and held-run reset boundaries of FlickerTracker for FL v3.
"""
import unittest
import numpy as np

from eval.v2x.fl_blinkprior import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FlickerTracker,
    reset_ops,
)


class TestFLBlinkpriorBoundaries(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_symmetry_diff_boundary(self):
        """Alternating lengths 14 and 16 (diff 2) declare FLICKER; lengths 14 and 17 (diff 3) reject."""
        hash_a = 0x123456789ABCDEF0
        hash_b = 0xFEDCBA9876543210

        # Case 1: Lengths 14 and 16 (diff = 2 <= 2 -> passes)
        # Run 0: A for 14 frames (0..13)
        # Run 1: B for 16 frames (14..29)
        # Run 2: A for 14 frames (30..43)
        # Toggle 3 at frame 44: B
        t14_16 = FlickerTracker(n_tiles=1, max_half=64)
        seq1 = [hash_a] * 14 + [hash_b] * 16 + [hash_a] * 14 + [hash_b] * 16
        out1 = [t14_16.update([h]) for h in seq1]

        self.assertEqual(int(out1[14][0][0]), C_CHANGED)
        self.assertEqual(int(out1[30][0][0]), C_CHANGED)
        self.assertEqual(int(out1[44][0][0]), C_FLICKER)
        self.assertEqual(int(out1[44][1][0]), 15)

        # Case 2: Lengths 14 and 17 (diff = 3 > 2 -> fails diff_ok)
        # Run 0: A for 14 frames (0..13)
        # Run 1: B for 17 frames (14..30)
        # Run 2: A for 14 frames (31..44)
        # Toggle 3 at frame 45: B
        t14_17 = FlickerTracker(n_tiles=1, max_half=64)
        seq2 = [hash_a] * 14 + [hash_b] * 17 + [hash_a] * 14 + [hash_b] * 17
        out2 = [t14_17.update([h]) for h in seq2]

        for f in range(len(seq2)):
            self.assertNotEqual(int(out2[f][0][0]), C_FLICKER, f"Frame {f} emitted FLICKER unexpectedly")

    def test_held_run_continuation_and_overmax_reset(self):
        """Test continuation at length 9 vs overmax reset at length 10 and 3-toggle reentry."""
        hash_a = 0x1111222233334444
        hash_b = 0x5555666677778888

        # 1. Continuation tracker: A:8, B:8, A:8, B:9, then toggle to A
        # Run 0: A:8 (0..7)
        # Run 1: B:8 (8..15)
        # Run 2: A:8 (16..23)
        # Run 3: B:9 (24..32)
        t_cont = FlickerTracker(n_tiles=1, max_half=64)
        seq_cont = [hash_a] * 8 + [hash_b] * 8 + [hash_a] * 8 + [hash_b] * 9 + [hash_a] * 1
        out_cont = [t_cont.update([h]) for h in seq_cont]

        # Frame 24: 3rd qualifying toggle -> FLICKER, hp=8
        self.assertEqual(int(out_cont[24][0][0]), C_FLICKER)
        self.assertEqual(int(out_cont[24][1][0]), 8)

        # Frame 32: held frame (9th B frame) -> UNCHANGED
        self.assertEqual(int(out_cont[32][0][0]), C_UNCHANGED)
        self.assertEqual(int(out_cont[32][1][0]), 8)

        # Frame 33: toggle to A -> FLICKER, hp=9
        self.assertEqual(int(out_cont[33][0][0]), C_FLICKER)
        self.assertEqual(int(out_cont[33][1][0]), 9)
        self.assertTrue(t_cont.in_flicker[0])

        # 2. Reset tracker: A:8, B:8, A:8, B:10, then A:8, B:8, A:8, B:8, A:8
        t_reset = FlickerTracker(n_tiles=1, max_half=64)
        seq_reset = (
            [hash_a] * 8 + [hash_b] * 8 + [hash_a] * 8 + [hash_b] * 10 +
            [hash_a] * 8 + [hash_b] * 8 + [hash_a] * 8 + [hash_b] * 8 + [hash_a] * 8
        )

        for f, h in enumerate(seq_reset):
            st, hp = t_reset.update([h])

            if f == 24:
                self.assertEqual(int(st[0]), C_FLICKER)
                self.assertEqual(int(hp[0]), 8)
            elif f == 32:
                # Held B frame 9 -> UNCHANGED
                self.assertEqual(int(st[0]), C_UNCHANGED)
                self.assertEqual(int(hp[0]), 8)
                self.assertTrue(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.cur_len[0]), 9)
            elif f == 33:
                # Held B frame 10 -> over_max triggered on held frame!
                self.assertEqual(int(st[0]), C_UNCHANGED)
                self.assertEqual(int(hp[0]), 0)
                self.assertFalse(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 0)
                self.assertFalse(t_reset.has_b[0])
                self.assertEqual(int(t_reset.cur_len[0]), 10)
            elif f == 34:
                # Toggle to A at frame 34 -> CHANGED
                self.assertEqual(int(st[0]), C_CHANGED)
                self.assertEqual(int(hp[0]), 0)
                self.assertFalse(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 1)
            elif f == 42:
                # Post-reset toggle 1 to B at frame 42 -> CHANGED
                self.assertEqual(int(st[0]), C_CHANGED)
                self.assertEqual(int(hp[0]), 0)
                self.assertFalse(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 0)
            elif f == 50:
                # Post-reset toggle 1 qualifying run to A at frame 50 -> CHANGED
                self.assertEqual(int(st[0]), C_CHANGED)
                self.assertEqual(int(hp[0]), 0)
                self.assertFalse(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 1)
            elif f == 58:
                # Post-reset toggle 2 qualifying run to B at frame 58 -> CHANGED
                self.assertEqual(int(st[0]), C_CHANGED)
                self.assertEqual(int(hp[0]), 0)
                self.assertFalse(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 2)
            elif f == 66:
                # Post-reset toggle 3 qualifying run to A at frame 66 -> FLICKER redeclared!
                self.assertEqual(int(st[0]), C_FLICKER)
                self.assertEqual(int(hp[0]), 8)
                self.assertTrue(t_reset.in_flicker[0])
                self.assertEqual(int(t_reset.n_runs[0]), 3)

    def test_min_half_boundary_contrast(self):
        """max_half=6 (min_half=1) emits FLICKER on 3rd toggle at frame 18; max_half=7 (min_half=7) rejects run lengths 6."""
        hash_a = 0xAAAAAAAAAAAAAAA0
        hash_b = 0x5555555555555550

        # max_half = 6 -> min_half = 1
        # Run length 6 is valid (1 <= 6 <= 6)
        t6 = FlickerTracker(n_tiles=1, max_half=6)
        seq6 = ([hash_a] * 6 + [hash_b] * 6) * 3
        out6 = [t6.update([h]) for h in seq6]

        self.assertEqual(int(out6[6][0][0]), C_CHANGED)
        self.assertEqual(int(out6[12][0][0]), C_CHANGED)
        self.assertEqual(int(out6[18][0][0]), C_FLICKER)
        self.assertEqual(int(out6[18][1][0]), 6)

        # max_half = 7 -> min_half = 7
        # Run length 6 is invalid (6 < 7)
        t7 = FlickerTracker(n_tiles=1, max_half=7)
        seq7 = ([hash_a] * 6 + [hash_b] * 6) * 3
        out7 = [t7.update([h]) for h in seq7]

        for f in range(len(seq7)):
            self.assertNotEqual(int(out7[f][0][0]), C_FLICKER, f"max_half=7 emitted FLICKER on frame {f}")


if __name__ == "__main__":
    unittest.main()
