"""Unit tests for eval/v2x/fl_early3.py (early declaration FlickerTracker)."""
import unittest
import numpy as np

import eval.v2x.fl_early3 as fl_early3
from eval.v2x.fl_early3 import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FLICKER,
    CHANGED,
    UNCHANGED,
    FlickerTracker,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlickerTrackerEarly3(unittest.TestCase):
    def test_blink_half_periods_over_120_frames(self):
        """Blink half-periods 7, 8, 15, 30 and 60 frames over 120 frames with exact emission frames."""
        a, b = 0x1111222233334444, 0x9999AAAABBBBCCCC

        # Half period 7: Toggles at 7, 14, 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119
        # Toggles >= 3 are frames 21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119
        half_7 = 7
        seq_7 = []
        for f in range(120):
            seq_7.append(a if (f // half_7) % 2 == 0 else b)
        tracker_7 = FlickerTracker(n_tiles=1, max_half=64)
        out_7 = _feed_sequence(tracker_7, [[x] for x in seq_7])
        expected_7 = {21, 28, 35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119}
        for f in range(120):
            st, hp = int(out_7[f][0][0]), int(out_7[f][1][0])
            if f in expected_7:
                self.assertEqual(st, C_FLICKER, f"H=7 frame {f} expected FLICKER")
                self.assertEqual(hp, 7, f"H=7 frame {f} expected half_period 7")
            else:
                self.assertNotEqual(st, C_FLICKER, f"H=7 frame {f} unexpected FLICKER")
                self.assertEqual(hp, 0)

        # Half period 8: Toggles at 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112
        # Toggles >= 3 are frames 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112
        half_8 = 8
        seq_8 = [a if (f // half_8) % 2 == 0 else b for f in range(120)]
        tracker_8 = FlickerTracker(n_tiles=1, max_half=64)
        out_8 = _feed_sequence(tracker_8, [[x] for x in seq_8])
        expected_8 = {24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112}
        for f in range(120):
            st, hp = int(out_8[f][0][0]), int(out_8[f][1][0])
            if f in expected_8:
                self.assertEqual(st, C_FLICKER, f"H=8 frame {f} expected FLICKER")
                self.assertEqual(hp, 8)
            else:
                self.assertNotEqual(st, C_FLICKER, f"H=8 frame {f} unexpected FLICKER")

        # Half period 15: Toggles at 15, 30, 45, 60, 75, 90, 105
        # Toggles >= 3 are frames 45, 60, 75, 90, 105
        half_15 = 15
        seq_15 = [a if (f // half_15) % 2 == 0 else b for f in range(120)]
        tracker_15 = FlickerTracker(n_tiles=1, max_half=64)
        out_15 = _feed_sequence(tracker_15, [[x] for x in seq_15])
        expected_15 = {45, 60, 75, 90, 105}
        for f in range(120):
            st, hp = int(out_15[f][0][0]), int(out_15[f][1][0])
            if f in expected_15:
                self.assertEqual(st, C_FLICKER, f"H=15 frame {f} expected FLICKER")
                self.assertEqual(hp, 15)
            else:
                self.assertNotEqual(st, C_FLICKER, f"H=15 frame {f} unexpected FLICKER")

        # Half period 30: Toggles at 30, 60, 90
        # Toggle 3 is frame 90
        half_30 = 30
        seq_30 = [a if (f // half_30) % 2 == 0 else b for f in range(120)]
        tracker_30 = FlickerTracker(n_tiles=1, max_half=64)
        out_30 = _feed_sequence(tracker_30, [[x] for x in seq_30])
        expected_30 = {90}
        for f in range(120):
            st, hp = int(out_30[f][0][0]), int(out_30[f][1][0])
            if f in expected_30:
                self.assertEqual(st, C_FLICKER, f"H=30 frame {f} expected FLICKER")
                self.assertEqual(hp, 30)
            else:
                self.assertNotEqual(st, C_FLICKER, f"H=30 frame {f} unexpected FLICKER")

        # Half period 60: Toggle 1 at frame 60. Over 120 frames, no toggle 3 occurs.
        half_60 = 60
        seq_60 = [a if (f // half_60) % 2 == 0 else b for f in range(120)]
        tracker_60 = FlickerTracker(n_tiles=1, max_half=64)
        out_60 = _feed_sequence(tracker_60, [[x] for x in seq_60])
        for f in range(120):
            st, hp = int(out_60[f][0][0]), int(out_60[f][1][0])
            self.assertNotEqual(st, C_FLICKER, f"H=60 frame {f} unexpected FLICKER")
            self.assertEqual(hp, 0)

    def test_jitter_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter across consecutive phases."""
        a, b = 100, 200
        # Phase lengths: 15, 15, 16, 15, 14, 15
        run_lengths = [15, 15, 16, 15, 14, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        tracker = FlickerTracker(n_tiles=1, max_half=64)
        out = _feed_sequence(tracker, [[x] for x in seq])

        # Toggle 1 at end of run 0 (frame 15)
        # Toggle 2 at end of run 1 (frame 15 + 15 = 30)
        # Toggle 3 at end of run 2 (frame 30 + 16 = 46) -> EMITS FLICKER!
        # Toggle 4 at end of run 3 (frame 46 + 15 = 61) -> EMITS FLICKER!
        t3_frame = 15 + 15 + 16
        t4_frame = t3_frame + 15
        self.assertEqual(int(out[t3_frame][0][0]), C_FLICKER)
        self.assertEqual(int(out[t4_frame][0][0]), C_FLICKER)

        # Held frames in between are UNCHANGED (0)
        self.assertEqual(int(out[t3_frame + 1][0][0]), C_UNCHANGED)
        self.assertEqual(int(out[t3_frame + 1][1][0]), 0)

    def test_asymmetric_duty_cycle_jitter_ok(self):
        """Asymmetric duty cycle with +/-1 difference (e.g. 10 and 11 frames) is detected."""
        a, b = 10, 20
        # Phases 10, 11, 10, 11, 10, 11
        run_lengths = [10, 11, 10, 11, 10, 11]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        tracker = FlickerTracker(n_tiles=1, max_half=64)
        out = _feed_sequence(tracker, [[x] for x in seq])

        t3_frame = 10 + 11 + 10
        self.assertEqual(int(out[t3_frame][0][0]), C_FLICKER)

    @unittest.expectedFailure
    def test_asymmetric_duty_cycle_large_difference_limitation(self):
        """Algorithm limitation: asymmetric duty cycle with phase difference > 1 (e.g. 10 and 15 frames) is rejected."""
        a, b = 10, 20
        # Phases 10, 15, 10, 15, 10, 15
        run_lengths = [10, 15, 10, 15, 10, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        tracker = FlickerTracker(n_tiles=1, max_half=64)
        out = _feed_sequence(tracker, [[x] for x in seq])

        t3_frame = 10 + 15 + 10
        # Algorithm requires abs(r1 - r2) <= 1, so abs(15 - 10) = 5 fails this test, exposing the limitation.
        self.assertEqual(int(out[t3_frame][0][0]), C_FLICKER)

    def test_held_phases_never_emitted(self):
        """Held frames where hash does not change NEVER emit FLICKER (state 4)."""
        a, b = 1, 2
        seq = ([a] * 10 + [b] * 10) * 4
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f in range(len(seq)):
            st, hp = int(out[f][0][0]), int(out[f][1][0])
            if f == 0 or seq[f] == seq[f - 1]:
                # Held frame
                self.assertNotEqual(st, C_FLICKER, f"frame {f} held phase emitted FLICKER")
                self.assertEqual(st, C_UNCHANGED, f"frame {f} held phase state")
                self.assertEqual(hp, 0)

    def test_third_state_breaks_cycle(self):
        """A third distinct hash breaks the 2-state cycle and blocks the tile for 2 * max_half frames."""
        a, b, c = 111, 222, 333
        tracker = FlickerTracker(n_tiles=1, max_half=64)

        # Establish cycle: A (10) -> B (10) -> A (10) -> B (10)
        seq1 = ([a] * 10 + [b] * 10) * 2
        out1 = _feed_sequence(tracker, [[x] for x in seq1])
        # Frame 30 (toggle 3) emitted FLICKER
        self.assertEqual(int(out1[30][0][0]), C_FLICKER)

        # Introduce third state C
        st_c, hp_c = tracker.update([c])
        self.assertEqual(int(st_c[0]), C_CHANGED)
        self.assertEqual(int(hp_c[0]), 0)

        # Subsequent toggles within 2 * max_half (128) frames cannot emit FLICKER
        seq2 = ([c] * 10 + [b] * 10) * 4
        out2 = _feed_sequence(tracker, [[x] for x in seq2])
        for f, (st, hp) in enumerate(out2):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f} after third state emitted FLICKER")

    def test_scrolling_sequence_of_distinct_hashes(self):
        """A scrolling sequence producing distinct hashes on every frame never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[1000 + i] for i in range(120)]
        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"scrolling frame {f}")

    def test_slow_fade_of_40_distinct_hashes(self):
        """A slow fade producing a new distinct hash every 3 frames never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for i in range(40):
            seq.extend([[5000 + i]] * 3)
        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"fade frame {f}")

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice (A -> B -> A) then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[10]] * 10 + [[20]] * 10 + [[10]] * 50
        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"toggle twice static frame {f}")

    def test_max_half_boundaries_1_and_64(self):
        """Boundaries max_half=1 and max_half=64 are recognized correctly."""
        # max_half = 1: toggles every frame
        tracker1 = FlickerTracker(n_tiles=1, max_half=1)
        seq1 = [[10], [20], [10], [20], [10], [20]]
        out1 = _feed_sequence(tracker1, seq1)
        # f=0: init (0)
        # f=1: toggle 1 (B) -> changed
        # f=2: toggle 2 (A) -> changed
        # f=3: toggle 3 (B) -> FLICKER!
        # f=4: toggle 4 (A) -> FLICKER!
        self.assertEqual(int(out1[3][0][0]), C_FLICKER)
        self.assertEqual(int(out1[4][0][0]), C_FLICKER)

        # max_half = 64
        tracker64 = FlickerTracker(n_tiles=1, max_half=64)
        seq64 = [[10]] * 64 + [[20]] * 64 + [[10]] * 64 + [[20]] * 64
        out64 = _feed_sequence(tracker64, seq64)
        # T3 at frame 64*3 = 192
        self.assertEqual(int(out64[192][0][0]), C_FLICKER)
        self.assertEqual(int(out64[192][1][0]), 64)

    def test_ops_increases_and_resets(self):
        """OPS increases on tracker update calls and resets to 0 with reset_ops()."""
        reset_ops()
        self.assertEqual(fl_early3.OPS, 0)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        tracker.update([1, 2, 3, 4])
        self.assertGreater(fl_early3.OPS, 0)

        ops_after_one = fl_early3.OPS
        tracker.update([1, 2, 3, 5])
        self.assertGreater(fl_early3.OPS, ops_after_one)

        reset_ops()
        self.assertEqual(fl_early3.OPS, 0)

    def test_invalid_inputs(self):
        """Invalid inputs raise ValueError like the prototype."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-1)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=True)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=1, max_half=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=1, max_half=-5)

        tracker = FlickerTracker(n_tiles=2)
        with self.assertRaises(ValueError):
            tracker.update([1])  # Wrong length
        with self.assertRaises(ValueError):
            tracker.update(["invalid", 2])  # Non-integer dtype


if __name__ == "__main__":
    unittest.main()
