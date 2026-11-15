"""Unit tests for eval/v2x/fl_majority.py (FlickerTracker)."""
import unittest
import numpy as np

import eval.v2x.fl_majority as fl_majority
from eval.v2x.fl_majority import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    CHANGED,
    FLICKER,
    FlickerTracker,
    UNCHANGED,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlickerTrackerMajority(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_blink_half_periods_7_8_15_30_60_over_120_frames(self):
        """Test exact expected emission frames for half-periods 7, 8, 15, 30, and 60 over 120 frames."""
        a, b = 0x1111222233334444, 0x9999AAAABBBBCCCC

        # Test half-period 7
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq7 = []
        for i in range(120):
            seq7.append([a if (i // 7) % 2 == 0 else b])
        out7 = _feed_sequence(tracker, seq7)
        emitted7 = [f for f, (st, hp) in enumerate(out7) if st[0] == C_FLICKER]
        expected7 = [35, 42, 49, 56, 63, 70, 77, 84, 91, 98, 105, 112, 119]
        self.assertEqual(emitted7, expected7)
        for f in emitted7:
            self.assertEqual(int(out7[f][1][0]), 7)

        # Test half-period 8
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq8 = []
        for i in range(120):
            seq8.append([a if (i // 8) % 2 == 0 else b])
        out8 = _feed_sequence(tracker, seq8)
        emitted8 = [f for f, (st, hp) in enumerate(out8) if st[0] == C_FLICKER]
        expected8 = [40, 48, 56, 64, 72, 80, 88, 96, 104, 112]
        self.assertEqual(emitted8, expected8)
        for f in emitted8:
            self.assertEqual(int(out8[f][1][0]), 8)

        # Test half-period 15
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq15 = []
        for i in range(120):
            seq15.append([a if (i // 15) % 2 == 0 else b])
        out15 = _feed_sequence(tracker, seq15)
        emitted15 = [f for f, (st, hp) in enumerate(out15) if st[0] == C_FLICKER]
        expected15 = [75, 90, 105]
        self.assertEqual(emitted15, expected15)
        for f in emitted15:
            self.assertEqual(int(out15[f][1][0]), 15)

        # Test half-period 30: in 120 frames (0..119), toggles at 30, 60, 90 (3 toggles < 5 required) -> no emission
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq30 = []
        for i in range(120):
            seq30.append([a if (i // 30) % 2 == 0 else b])
        out30 = _feed_sequence(tracker, seq30)
        emitted30 = [f for f, (st, hp) in enumerate(out30) if st[0] == C_FLICKER]
        self.assertEqual(emitted30, [])

        # Test half-period 60: in 120 frames (0..119), toggle at 60 (1 toggle < 5 required) -> no emission
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq60 = []
        for i in range(120):
            seq60.append([a if (i // 60) % 2 == 0 else b])
        out60 = _feed_sequence(tracker, seq60)
        emitted60 = [f for f, (st, hp) in enumerate(out60) if st[0] == C_FLICKER]
        self.assertEqual(emitted60, [])

    def test_half_period_30_over_180_frames(self):
        """Half-period 30 emits on frame 150 (5th toggle) and 180 when extended past 120 frames."""
        a, b = 100, 200
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq30 = [[a if (i // 30) % 2 == 0 else b] for i in range(185)]
        out30 = _feed_sequence(tracker, seq30)
        emitted = [f for f, (st, hp) in enumerate(out30) if st[0] == C_FLICKER]
        self.assertEqual(emitted, [150, 180])
        self.assertEqual(int(out30[150][1][0]), 30)
        self.assertEqual(int(out30[180][1][0]), 30)

    def test_jittered_period_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter and returns the measured nominal half-period."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        # Phase run lengths: 15, 16, 14, 15, 16 -> intervals 16, 14, 15, 16 around median 15
        run_lengths = [15, 16, 14, 15, 16, 14, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([[a if idx % 2 == 0 else b]] * length)
        out = _feed_sequence(tracker, seq)

        # 5th toggle frame: 15 + 16 + 14 + 15 + 16 = 76
        fifth_toggle_frame = sum(run_lengths[:5])
        self.assertEqual(out[fifth_toggle_frame][0][0], C_FLICKER)
        self.assertEqual(out[fifth_toggle_frame][1][0], 15)

        # Excessive jitter (> +/-1 frame difference, e.g. 15, 18, 15, 18) is rejected
        tracker2 = FlickerTracker(n_tiles=1, max_half=64)
        seq2 = []
        for idx, length in enumerate([15, 18, 15, 18, 15, 18]):
            seq2.extend([[a if idx % 2 == 0 else b]] * length)
        out2 = _feed_sequence(tracker2, seq2)
        emitted2 = [f for f, (st, _) in enumerate(out2) if st[0] == C_FLICKER]
        self.assertEqual(emitted2, [])

    def test_asymmetric_duty_cycle(self):
        """Asymmetric duty cycles (e.g., 10 frames ON, 20 frames OFF) are rejected as non-flicker."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 111, 222
        seq = []
        for _ in range(6):
            seq.extend([[a]] * 10)
            seq.extend([[b]] * 20)
        out = _feed_sequence(tracker, seq)
        emitted = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(emitted, [])

    def test_held_phases_never_emitted(self):
        """State 4 (FLICKER) is emitted ONLY on toggle frames; held frames return state 0 (UNCHANGED)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        half = 8
        seq = [[a if (i // half) % 2 == 0 else b] for i in range(100)]
        out = _feed_sequence(tracker, seq)

        for f, (st, hp) in enumerate(out):
            if f in (40, 48, 56, 64, 72, 80, 88, 96):
                self.assertEqual(st[0], C_FLICKER, f"frame {f}")
                self.assertEqual(hp[0], half, f"frame {f}")
            else:
                self.assertNotEqual(st[0], C_FLICKER, f"frame {f}")
                self.assertEqual(hp[0], 0, f"frame {f}")

    def test_third_state_breaks_cycle(self):
        """Interjecting a third state breaks the two-state cycle and prevents FLICKER emission."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 11, 22, 33
        half = 10
        # A B A B C A B A B
        seq = []
        for val in (a, b, a, b, c, a, b, a, b):
            seq.extend([[val]] * half)
        out = _feed_sequence(tracker, seq)

        # On frame 40 (start of c phase): toggle occurs, but third state breaks alternation
        st_c, hp_c = out[40]
        self.assertEqual(st_c[0], C_CHANGED)
        self.assertEqual(hp_c[0], 0)

        # Frame 40..49 has no state 4
        for f in range(40, 50):
            self.assertNotEqual(out[f][0][0], C_FLICKER)

    def test_scrolling_sequence_of_distinct_hashes(self):
        """A scrolling object producing distinct hashes every frame never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[100 + i] for i in range(120)]
        out = _feed_sequence(tracker, seq)
        emitted = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(emitted, [])

    def test_slow_fade_40_distinct_hashes(self):
        """A slow fade of 40 distinct hashes held 3 frames each never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for h_val in range(1000, 1040):
            seq.extend([[h_val]] * 3)
        out = _feed_sequence(tracker, seq)
        emitted = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(emitted, [])

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice (A -> B -> A) and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 1, 2
        seq = [[a]] * 10 + [[b]] * 10 + [[a]] * 100
        out = _feed_sequence(tracker, seq)
        emitted = [f for f, (st, _) in enumerate(out) if st[0] == C_FLICKER]
        self.assertEqual(emitted, [])

    def test_max_half_boundaries_1_and_64(self):
        """Half-period boundaries 1 and 64 are recognized, while 65 (> max_half) is rejected."""
        # Boundary 1
        tracker1 = FlickerTracker(n_tiles=1, max_half=64)
        seq1 = [[10 if i % 2 == 0 else 20] for i in range(20)]
        out1 = _feed_sequence(tracker1, seq1)
        # 5th toggle at frame 5
        self.assertEqual(out1[5][0][0], C_FLICKER)
        self.assertEqual(out1[5][1][0], 1)

        # Boundary 64
        tracker64 = FlickerTracker(n_tiles=1, max_half=64)
        seq64 = [[10 if (i // 64) % 2 == 0 else 20] for i in range(64 * 6)]
        out64 = _feed_sequence(tracker64, seq64)
        # 5th toggle at frame 320 (64 * 5)
        self.assertEqual(out64[320][0][0], C_FLICKER)
        self.assertEqual(out64[320][1][0], 64)

        # Boundary 65 (exceeds max_half=64)
        tracker65 = FlickerTracker(n_tiles=1, max_half=64)
        seq65 = [[10 if (i // 65) % 2 == 0 else 20] for i in range(65 * 6)]
        out65 = _feed_sequence(tracker65, seq65)
        emitted65 = [f for f, (st, _) in enumerate(out65) if st[0] == C_FLICKER]
        self.assertEqual(emitted65, [])

    def test_ops_increases_and_resets(self):
        """Module-level OPS increases with update calls and resets with reset_ops()."""
        reset_ops()
        self.assertEqual(fl_majority.OPS, 0)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        tracker.update([10, 20, 30, 40])
        ops_after_first = fl_majority.OPS
        self.assertGreater(ops_after_first, 0)

        tracker.update([10, 20, 30, 40])
        ops_after_second = fl_majority.OPS
        self.assertGreater(ops_after_second, ops_after_first)

        reset_ops()
        self.assertEqual(fl_majority.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker creation or update parameters raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=True)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=10, max_half=0)

        tracker = FlickerTracker(n_tiles=2, max_half=64)
        with self.assertRaises(ValueError):
            tracker.update([10])  # length mismatch
        with self.assertRaises(ValueError):
            tracker.update([[10, 20]])  # wrong ndim
        with self.assertRaises(ValueError):
            tracker.update([10, "invalid"])  # non-integer
        with self.assertRaises(ValueError):
            tracker.update([10, True])  # bool element

    def test_many_tiles_simultaneous_vectorized(self):
        """Vectorized tracking across multiple tiles with distinct behaviors."""
        n_tiles = 4
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
        # Tile 0: static
        # Tile 1: 7-frame blink
        # Tile 2: 15-frame blink
        # Tile 3: scrolling
        n_frames = 100
        a, b = 1000, 2000

        states_hist = []
        hp_hist = []
        for f in range(n_frames):
            row = [
                100,  # static
                a if (f // 7) % 2 == 0 else b,  # 7-frame
                a if (f // 15) % 2 == 0 else b,  # 15-frame
                5000 + f,  # scrolling
            ]
            st, hp = tracker.update(row)
            states_hist.append(st)
            hp_hist.append(hp)

        # Tile 0: always UNCHANGED, hp=0
        for f in range(n_frames):
            self.assertEqual(states_hist[f][0], UNCHANGED)
            self.assertEqual(hp_hist[f][0], 0)

        # Tile 1 (7-frame): emits FLICKER at frame 35, 42, 49, 56...
        self.assertEqual(states_hist[35][1], FLICKER)
        self.assertEqual(hp_hist[35][1], 7)

        # Tile 2 (15-frame): emits FLICKER at frame 75, 90...
        self.assertEqual(states_hist[75][2], FLICKER)
        self.assertEqual(hp_hist[75][2], 15)

        # Tile 3 (scrolling): never FLICKER
        for f in range(n_frames):
            self.assertNotEqual(states_hist[f][3], FLICKER)

    @unittest.expectedFailure
    def test_limitation_60fps_30_frame_blink_in_120_frame_scene(self):
        """Limitation: majority vote requires 5 toggles (5 half-periods).

        A 500 ms caret blink at 60 fps (30 frames per phase) in a 120-frame scene
        has toggles only at frames 30, 60, 90 (3 toggles < 5 required), so majority
        vote cannot gather enough evidence to declare FLICKER within 120 frames.
        """
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 1001, 2002
        half = 30
        phases = ([a] * half + [b] * half) * 2  # 120 frames total
        out = _feed_sequence(tracker, [[x] for x in phases])

        states = [int(s[0]) for s, _ in out]
        # Expecting FLICKER within 120 frames fails because 3 toggles < 5 required
        self.assertIn(FLICKER, states)


if __name__ == "__main__":
    unittest.main()
