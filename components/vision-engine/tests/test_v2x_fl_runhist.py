"""Unit tests for eval/v2x/fl_runhist.py (FlickerTracker)."""
import unittest

import numpy as np

import eval.v2x.fl_runhist as fl_runhist
from eval.v2x.fl_runhist import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FlickerTracker,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlickerTrackerRunHist(unittest.TestCase):
    def test_blink_half_periods_7_8_15_30_60_over_120_frames(self):
        """Blink half-periods 7, 8, 15, 30, and 60 over 120 frames with exact expected emission frames."""
        halves = [7, 8, 15, 30, 60]
        n_tiles = len(halves)
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
        n_frames = 120

        # Construct frame sequence
        seq = []
        for f in range(n_frames):
            row = []
            for idx, half in enumerate(halves):
                hash_val = 1000 + idx * 10 if ((f // half) % 2 == 0) else 2000 + idx * 10
                row.append(hash_val)
            seq.append(row)

        out = _feed_sequence(tracker, seq)

        # Collect emission frames per tile
        emissions = {idx: [] for idx in range(n_tiles)}
        half_periods = {idx: [] for idx in range(n_tiles)}
        for f, (st, hp) in enumerate(out):
            for idx in range(n_tiles):
                if st[idx] == C_FLICKER:
                    emissions[idx].append(f)
                    half_periods[idx].append(hp[idx])

        # Exact expected emission frames:
        # State 4 emits ONLY on transition frames where 2 full A runs and 2 full B runs have completed.
        # Transition frames occur at k * half. Emits occur at k * half for k >= 4.
        expected_7 = [f for f in range(28, 120, 7)]
        expected_8 = [f for f in range(32, 120, 8)]
        expected_15 = [f for f in range(60, 120, 15)]
        expected_30 = []  # 4 * 30 = 120 (frame index 120 is outside 0..119)
        expected_60 = []  # 4 * 60 = 240 (outside 0..119)

        self.assertEqual(emissions[0], expected_7)
        self.assertTrue(all(hp == 7 for hp in half_periods[0]))

        self.assertEqual(emissions[1], expected_8)
        self.assertTrue(all(hp == 8 for hp in half_periods[1]))

        self.assertEqual(emissions[2], expected_15)
        self.assertTrue(all(hp == 15 for hp in half_periods[2]))

        self.assertEqual(emissions[3], expected_30)
        self.assertEqual(emissions[4], expected_60)

    def test_jittered_period_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter and computes mean half-period rounded half up."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        # A runs: 10, 11; B runs: 10, 9. Nominal half-period ~10.
        # Sequence of run lengths: A(10), B(10), A(11), B(9), A(10), B(10)
        run_lengths = [10, 10, 11, 9, 10, 10]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])

        # Emissions must occur at transitions after 2 A runs and 2 B runs completed:
        # Total frames for first 4 runs = 10 + 10 + 11 + 9 = 40. Transition frame index = 40.
        # Next run A(10) ends at frame 40 + 10 = 50. Transition frame index = 50.
        flicker_frames = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(flicker_frames, [40, 50])

        # Check calculated half-period at frame 40:
        # A runs: 10, 11 -> mean = (10+11+1)//2 = 11
        # B runs: 10, 9 -> mean = (10+9+1)//2 = 10
        # Total hp_est = (11 + 10 + 1) // 2 = 11
        self.assertEqual(int(out[40][1][0]), 11)

    def test_asymmetric_duty_cycle(self):
        """Allows asymmetric duty cycle (e.g. A run 5 frames, B run 15 frames)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 111, 222
        # A(5), B(15), A(5), B(15), A(5)
        run_lengths = [5, 15, 5, 15, 5]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])

        flicker_frames = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        # First 4 runs complete at 5 + 15 + 5 + 15 = 40 frames -> frame 40
        self.assertEqual(flicker_frames, [40])

        # Half period estimate:
        # A mean = (5 + 5 + 1) // 2 = 5
        # B mean = (15 + 15 + 1) // 2 = 15
        # Overall hp = (5 + 15 + 1) // 2 = 10
        self.assertEqual(int(out[40][1][0]), 10)

    def test_held_phases_never_emitted(self):
        """Held frames where tile hash does not change are never emitted as FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 100, 200
        # 6 half-periods of 10 frames = 60 frames
        seq = ([a] * 10 + [b] * 10) * 3
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, _) in enumerate(out):
            if f in (40, 50):  # Transition frames after 2 full periods
                self.assertEqual(st[0], C_FLICKER, f"frame {f}")
            else:
                self.assertNotEqual(st[0], C_FLICKER, f"frame {f}")

    def test_third_state_breaks_cycle(self):
        """Any third state immediately breaks the two-state cycle and resets histogram."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 10, 20, 30
        # Sequence:
        # A(5), B(5), A(5), B(5), A(5) -> Frame 20 is FLICKER transition (A2 and B2 completed).
        # C(5) -> Frame 25 third state C arrives, resetting state_a to C.
        # B(5), C(5), B(5), C(5) -> Frame 45 is FLICKER transition for C/B cycle.
        seq = [a]*5 + [b]*5 + [a]*5 + [b]*5 + [a]*5 + [c]*5 + [b]*5 + [c]*5 + [b]*5 + [c]*5
        out = _feed_sequence(tracker, [[x] for x in seq])

        flicker_frames = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(flicker_frames, [20, 45])

    def test_scrolling_sequence_of_distinct_hashes(self):
        """Scrolling sequence of distinct hashes produces no FLICKER claims."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[100 + i] for i in range(100)]
        out = _feed_sequence(tracker, seq)

        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(st[0], C_FLICKER, f"frame {f}")
            self.assertEqual(hp[0], 0, f"frame {f}")

    def test_slow_fade_of_40_distinct_hashes(self):
        """Slow fade of 40 distinct hashes held for 3 frames each produces no FLICKER claims."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for i in range(40):
            seq.extend([[5000 + i]] * 3)
        out = _feed_sequence(tracker, seq)

        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(st[0], C_FLICKER, f"frame {f}")
            self.assertEqual(hp[0], 0, f"frame {f}")

    def test_toggle_twice_then_static(self):
        """Tile that toggles twice (A -> B -> A -> B) and then stays static produces no FLICKER claims."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        # A(10), B(10), A(10), B(50 static)
        seq = [1] * 10 + [2] * 10 + [1] * 10 + [2] * 50
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(st[0], C_FLICKER, f"frame {f}")
            self.assertEqual(hp[0], 0, f"frame {f}")

    def test_max_half_boundaries_1_and_64(self):
        """max_half boundaries 1 and 64 are correctly respected."""
        for half in (1, 64):
            with self.subTest(max_half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=half)
                seq = ([10] * half + [20] * half) * 3
                out = _feed_sequence(tracker, [[x] for x in seq])

                flicker_frames = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
                self.assertEqual(flicker_frames, [4 * half, 5 * half])

    def test_ops_increases_and_resets(self):
        """Module-level OPS increases on update and resets via reset_ops."""
        fl_runhist.reset_ops()
        self.assertEqual(fl_runhist.OPS, 0)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        tracker.update([1, 2, 3, 4])
        self.assertGreater(fl_runhist.OPS, 0)

        prev_ops = fl_runhist.OPS
        tracker.update([1, 2, 3, 5])
        self.assertGreater(fl_runhist.OPS, prev_ops)

        fl_runhist.reset_ops()
        self.assertEqual(fl_runhist.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker initialisation or update inputs raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=4, max_half=0)

        tracker = FlickerTracker(n_tiles=2, max_half=64)
        with self.assertRaises(ValueError):
            tracker.update([10])  # length != 2
        with self.assertRaises(ValueError):
            tracker.update(["invalid", 10])  # non-integer
        with self.assertRaises(ValueError):
            tracker.update(np.array([[1, 2], [3, 4]]))  # 2D array


if __name__ == "__main__":
    unittest.main()
