"""Unit tests for eval/v2x/fl_deadband.py (deadband_step and FlickerTracker)."""
import unittest
import numpy as np

from eval.v2x import fl_deadband
from eval.v2x.fl_deadband import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FLICKER,
    CHANGED,
    UNCHANGED,
    FlickerTracker,
    deadband_step,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestDeadbandStep(unittest.TestCase):
    def test_deadband_theorem_pinning(self):
        """Pin Dead Band Theorem:

        1000 alternating jitter samples give 1000 events at M=0 and exactly 1 at M=5.
        Alternating jitter samples with amplitude 5: d alternating between 0 and 5.
        With R=0, jitter amplitude = 5.
        """
        # Generator for 1000 alternating jitter differences: 0, 5, 0, 5, 0, 5...
        d_samples = [0 if i % 2 == 0 else 5 for i in range(1000)]

        # --- Test with M = 0 (R = 0, M = 0) ---
        state = False
        events_m0 = 0
        for d in d_samples:
            next_state = deadband_step(state, d, R=0, M=0)
            if next_state != state:
                events_m0 += 1
            state = next_state
        self.assertEqual(events_m0, 1000)

        # --- Test with M = 5 (R = 0, M = 5) ---
        state = False
        events_m5 = 0
        for d in d_samples:
            next_state = deadband_step(state, d, R=0, M=5)
            if next_state != state:
                events_m5 += 1
            state = next_state
        self.assertEqual(events_m5, 1)


class TestFlickerTrackerDeadband(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_30_frame_half_period(self):
        """Tracker test for a 30-frame half period blinking tile."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 0x1000, 0x2000
        seq = [[a if (i // 30) % 2 == 0 else b] for i in range(150)]
        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        expected_flicker_frames = {90, 120}
        for f in range(150):
            if f in expected_flicker_frames:
                self.assertEqual(states[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps[f], 30, f"frame {f}")
            else:
                self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_jitter_plus_minus_one_not_dropping_claim(self):
        """Jitter +/-1 does not drop the periodic claim once inside."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 100, 200
        run_lengths = [15, 15, 16, 14, 14, 15, 15, 16, 16, 14]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([[a if idx % 2 == 0 else b]] * length)

        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        toggle_frames = []
        cum = 0
        for length in run_lengths[:-1]:
            cum += length
            toggle_frames.append(cum)

        for tf in toggle_frames[2:]:
            self.assertEqual(states[tf], C_FLICKER, f"toggle frame {tf}")
            self.assertGreater(hps[tf], 0)

        for f in range(len(seq)):
            if f not in toggle_frames[2:]:
                self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_one_off_change_behavior(self):
        """A run failing the widened bound R+M drops the claim."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        seq = (
            [[a]] * 10
            + [[b]] * 10
            + [[a]] * 10
            + [[b]] * 10
            + [[a]] * 20
            + [[b]] * 10
        )
        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]

        self.assertEqual(states[30], C_FLICKER)
        self.assertEqual(states[40], C_FLICKER)
        self.assertNotEqual(states[60], C_FLICKER)

    def test_three_hash_cycling_resets_tile(self):
        """A third hash resets the tile immediately."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 1, 2, 3
        seq = [[a if (i // 10) % 2 == 0 else b] for i in range(40)]
        out1 = _feed_sequence(tracker, seq)
        self.assertEqual(int(out1[30][0][0]), C_FLICKER)

        st_c, hp_c = tracker.update([c])
        self.assertEqual(int(st_c[0]), C_CHANGED)
        self.assertEqual(int(hp_c[0]), 0)

        st_c2, hp_c2 = tracker.update([c])
        self.assertEqual(int(st_c2[0]), C_UNCHANGED)
        self.assertEqual(int(hp_c2[0]), 0)

    def test_held_frames_never_emit_flicker(self):
        """Held frames where the hash does not change never emit FLICKER (4)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 111, 222
        seq = [[a if (i // 10) % 2 == 0 else b] for i in range(100)]
        out = _feed_sequence(tracker, seq)

        for f, (st, hp) in enumerate(out):
            s_val = int(st[0])
            hp_val = int(hp[0])
            if f in (30, 40, 50, 60, 70, 80, 90):
                self.assertEqual(s_val, C_FLICKER, f"frame {f}")
                self.assertEqual(hp_val, 10, f"frame {f}")
            else:
                self.assertNotEqual(s_val, C_FLICKER, f"frame {f}")
                self.assertEqual(hp_val, 0, f"frame {f}")

    def test_ops_counter_increases_and_resets(self):
        """OPS increases on updates and resets via reset_ops."""
        reset_ops()
        self.assertEqual(fl_deadband.OPS, 0)
        tracker = FlickerTracker(n_tiles=10, max_half=64)
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_deadband.OPS, 0)

        ops_before = fl_deadband.OPS
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_deadband.OPS, ops_before)

        reset_ops()
        self.assertEqual(fl_deadband.OPS, 0)


if __name__ == "__main__":
    unittest.main()
