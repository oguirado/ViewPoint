"""Unit tests for eval/v2x/fl_fadeguard.py (FlickerTracker)."""
import unittest
import numpy as np

import eval.v2x.fl_fadeguard as fl_mod
from eval.v2x.fl_fadeguard import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FLICKER,
    FlickerTracker,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlFadeGuard(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_ops_increases_and_resets(self):
        """OPS increases with update calls and resets to 0 with reset_ops()."""
        reset_ops()
        self.assertEqual(fl_mod.OPS, 0)
        tracker = FlickerTracker(n_tiles=4, max_half=64)
        tracker.update([10, 20, 30, 40])
        ops1 = fl_mod.OPS
        self.assertGreater(ops1, 0)
        tracker.update([10, 20, 30, 45])
        ops2 = fl_mod.OPS
        self.assertGreater(ops2, ops1)
        reset_ops()
        self.assertEqual(fl_mod.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker creation and update inputs raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=4, max_half=0)

        tracker = FlickerTracker(n_tiles=2, max_half=64)
        with self.assertRaises(ValueError):
            tracker.update([10])  # length mismatch
        with self.assertRaises(ValueError):
            tracker.update(np.array([[1, 2], [3, 4]]))  # 2D array
        with self.assertRaises(ValueError):
            tracker.update(["invalid", 2])  # non-integer element

    def test_blink_half_periods_7_8_15_30_60_over_120_frames(self):
        """Blink half-periods 7, 8, 15, 30, 60 over 120 frames with exact expected emission frames."""
        half_periods = [7, 8, 15, 30, 60]
        n_tiles = len(half_periods)
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
        n_frames = 120

        a_hashes = [1000 + i for i in range(n_tiles)]
        b_hashes = [2000 + i for i in range(n_tiles)]

        states_history = []
        hp_history = []

        for f in range(n_frames):
            row = []
            for i, hp in enumerate(half_periods):
                # Phase index
                phase = f // hp
                val = a_hashes[i] if (phase % 2 == 0) else b_hashes[i]
                row.append(val)
            st, hp_out = tracker.update(row)
            states_history.append(st)
            hp_history.append(hp_out)

        # Check tile by tile
        for i, hp in enumerate(half_periods):
            # Check toggle frames and emission
            for f in range(n_frames):
                is_toggle = (f > 0) and ((f // hp) != ((f - 1) // hp))
                is_held = not is_toggle
                st = int(states_history[f][i])
                measured_hp = int(hp_history[f][i])

                # Held frames MUST NEVER emit state 4
                if is_held:
                    self.assertNotEqual(st, C_FLICKER, f"Tile {i} (hp={hp}) emitted state 4 on held frame {f}")

                # Check declaration boundary: requires 3 complete runs
                # Run 0: frames 0..hp-1 (ends at toggle frame hp)
                # Run 1: frames hp..2*hp-1 (ends at toggle frame 2*hp)
                # Run 2: frames 2*hp..3*hp-1 (ends at toggle frame 3*hp)
                # Declaration frame is 3 * hp (toggle frame starting 4th run)
                if is_toggle and f >= 3 * hp:
                    self.assertEqual(st, C_FLICKER, f"Tile {i} (hp={hp}) expected FLICKER on toggle frame {f}")
                    self.assertEqual(measured_hp, hp, f"Tile {i} (hp={hp}) measured wrong hp on frame {f}")
                elif is_toggle and f < 3 * hp:
                    self.assertNotEqual(st, C_FLICKER, f"Tile {i} (hp={hp}) emitted FLICKER early on frame {f}")

    def test_held_phases_never_emitted(self):
        """Held phases never emit state 4; only toggles emit state 4."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        hp = 10
        a, b = 111, 222
        seq = []
        for p in range(10):
            seq.extend([a if p % 2 == 0 else b] * hp)
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp_val) in enumerate(out):
            is_toggle = (f > 0) and (seq[f] != seq[f - 1])
            if not is_toggle:
                self.assertNotEqual(int(st[0]), C_FLICKER, f"Emitted FLICKER on non-toggle frame {f}")

    def test_jittered_period_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        # Phase run lengths: 15, 16, 14, 15, 14, 16
        run_lengths = [15, 16, 14, 15, 14, 16]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])

        # Toggle frames occur at: 15, 15+16=31, 31+14=45, 45+15=60, 60+14=74
        # Complete 3 runs at frame 45 (15+16+14). Toggle frame 45 is the 3rd run completion (declaration frame).
        toggle_frames = [15, 31, 45, 60, 74]
        for f in range(len(seq)):
            st = int(out[f][0][0])
            hp_est = int(out[f][1][0])
            if f in toggle_frames and f >= 45:
                self.assertEqual(st, C_FLICKER, f"Expected FLICKER at toggle frame {f}")
                self.assertIn(hp_est, (14, 15, 16), f"Expected estimated hp in (14,15,16) at frame {f}")
            else:
                self.assertNotEqual(st, C_FLICKER, f"Frame {f} should not emit FLICKER")

    def test_asymmetric_duty_cycle(self):
        """Asymmetric phase run lengths within +/-1 frame (e.g., 10 and 11) are accepted."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        # Runs: 10, 11, 10, 11, 10, 11
        run_lengths = [10, 11, 10, 11, 10, 11]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])
        # 3 runs complete at 10 + 11 + 10 = 31 (frame index 31)
        # Toggle frames at 10, 21, 31, 42, 52
        self.assertEqual(int(out[31][0][0]), C_FLICKER)
        self.assertEqual(int(out[31][1][0]), 10)

        # Held frame 32 is not FLICKER
        self.assertNotEqual(int(out[32][0][0]), C_FLICKER)

    def test_third_state_breaks_cycle(self):
        """A third distinct hash breaks the cycle and blocks FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 100, 200, 300
        half = 10
        # Establish 2-state cycle for 4 phases
        seq = ([a] * half + [b] * half) * 2 + [c] * half
        out = _feed_sequence(tracker, [[x] for x in seq])

        # Introduce third state c: at frame 40 (transition to c), 3 distinct hashes in window -> blocked!
        st_c = int(out[40][0][0])
        hp_c = int(out[40][1][0])
        self.assertEqual(st_c, C_CHANGED)
        self.assertEqual(hp_c, 0)

        # Subsequent frames of c are UNCHANGED with hp 0
        self.assertEqual(int(out[41][0][0]), C_UNCHANGED)
        self.assertEqual(int(out[41][1][0]), 0)

    def test_scrolling_sequence_of_distinct_hashes(self):
        """A scroll producing a sequence of distinct hashes is blocked by distinctness memory."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        # 20 distinct hashes, 3 frames each
        seq = []
        for h_val in range(1000, 1020):
            seq.extend([h_val] * 3)

        out = _feed_sequence(tracker, [[x] for x in seq])
        for f, (st, hp_val) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"Scroll emitted FLICKER on frame {f}")
            self.assertEqual(int(hp_val[0]), 0)

    def test_slow_fade_of_40_distinct_hashes(self):
        """A slow fade producing a new hash every 3 frames is blocked from FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for h_val in range(5000, 5040):
            seq.extend([h_val] * 3)

        out = _feed_sequence(tracker, [[x] for x in seq])
        for f, (st, hp_val) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"Slow fade emitted FLICKER on frame {f}")
            self.assertEqual(int(hp_val[0]), 0)

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice (2 runs) and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 77, 88
        # Phase 1 (a for 10), Phase 2 (b for 10), Phase 3 (a for 50)
        seq = [a] * 10 + [b] * 10 + [a] * 50
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp_val) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"Emitted FLICKER on frame {f}")
            self.assertEqual(int(hp_val[0]), 0)

    def test_max_half_boundaries_1_and_64(self):
        """Half-period boundaries 1 and 64 are recognized on toggle frames."""
        for half in (1, 64):
            with self.subTest(half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                a, b = 10, 20
                seq = ([a] * half + [b] * half) * 4
                out = _feed_sequence(tracker, [[x] for x in seq])

                # Check toggle frames after 3 runs (frame 3 * half)
                for f in range(len(seq)):
                    is_toggle = (f > 0) and (seq[f] != seq[f - 1])
                    st = int(out[f][0][0])
                    hp_val = int(out[f][1][0])
                    if is_toggle and f >= 3 * half:
                        self.assertEqual(st, C_FLICKER, f"Boundary half={half} failed on frame {f}")
                        self.assertEqual(hp_val, half, f"Boundary half={half} wrong hp on frame {f}")
                    else:
                        self.assertNotEqual(st, C_FLICKER, f"Boundary half={half} emitted non-toggle frame {f}")


if __name__ == "__main__":
    unittest.main()
