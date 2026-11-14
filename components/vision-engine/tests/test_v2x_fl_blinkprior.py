"""Unit tests for eval/v2x/fl_blinkprior.py (FlickerTracker with caret-blink temporal prior)."""
import unittest
import numpy as np

from eval.v2x import fl_blinkprior
from eval.v2x.fl_blinkprior import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FlickerTracker,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlickerTrackerBlinkPrior(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_blink_half_periods_7_8_15_30_60_exact_emission_frames(self):
        """Blink half-periods 7, 8, 15, 30, and 60 over 120 frames emit state 4 ONLY on toggle frames after 2 complete runs."""
        for half in (7, 8, 15, 30, 60):
            with self.subTest(half_period=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                a, b = 0x100, 0x200
                n_frames = 120
                seq = [a if ((f // half) % 2 == 0) else b for f in range(n_frames)]
                out = _feed_sequence(tracker, [[x] for x in seq])

                states = [int(s[0]) for s, _ in out]
                hps = [int(hp[0]) for _, hp in out]

                expected_emissions = set()
                # 3rd toggle is at frame 3 * half
                # 4th toggle is at frame 4 * half, etc.
                # All these toggle frames f where f >= 3 * half are emission frames.
                for k in range(3, n_frames // half + 1):
                    frame_idx = k * half
                    if frame_idx < n_frames:
                        expected_emissions.add(frame_idx)

                for f in range(n_frames):
                    if f in expected_emissions:
                        self.assertEqual(states[f], C_FLICKER, f"half={half}, frame={f}")
                        self.assertGreaterEqual(hps[f], half - 1)
                        self.assertLessEqual(hps[f], half + 1)
                    else:
                        self.assertNotEqual(states[f], C_FLICKER, f"half={half}, frame={f}")
                        if f > 0 and seq[f] == seq[f - 1]:
                            self.assertEqual(states[f], C_UNCHANGED, f"half={half}, frame={f}")
                        elif f > 0:
                            self.assertEqual(states[f], C_CHANGED, f"half={half}, frame={f}")

    def test_plus_minus_one_frame_jitter(self):
        """Tolerates +/-1 frame half-period jitter and emits only on toggle frames."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 1000, 2000
        # Phase lengths: 15, 16, 14, 15, 14, 16
        run_lengths = [15, 16, 14, 15, 14, 16]
        seq = []
        toggle_frames = []
        cur_frame = 0
        for idx, length in enumerate(run_lengths):
            if idx > 0:
                toggle_frames.append(cur_frame)
            seq.extend([a if idx % 2 == 0 else b] * length)
            cur_frame += length

        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # 3rd toggle occurs at end of phase 2 -> frame run_lengths[0] + run_lengths[1] + run_lengths[2]
        # toggles: index 0 (frame 15), index 1 (frame 31), index 2 (frame 45), index 3 (frame 60), index 4 (frame 74)
        for idx, f in enumerate(toggle_frames):
            if idx < 2:
                # First two toggles: not enough evidence yet
                self.assertNotEqual(states[f], C_FLICKER, f"toggle {idx} at frame {f}")
            else:
                # 3rd, 4th, 5th toggles: emit C_FLICKER
                self.assertEqual(states[f], C_FLICKER, f"toggle {idx} at frame {f}")
                self.assertEqual(hps[f], 15)

        # Held frames between toggles must NEVER emit C_FLICKER
        for f in range(len(seq)):
            if f not in toggle_frames[2:]:
                self.assertNotEqual(states[f], C_FLICKER, f"held frame {f}")

    def test_asymmetric_duty_cycle_rejection(self):
        """Asymmetric duty cycle (difference in run lengths > 1) is rejected."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        # Duty cycle: 10 frames A, 15 frames B (diff = 5 > 1)
        seq = ([a] * 10 + [b] * 15) * 4
        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        self.assertTrue(all(s != C_FLICKER for s in states))

    def test_held_phases_never_emitted(self):
        """Held frames where tile hash did not change are NEVER emitted as C_FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 111, 222
        half = 10
        seq = ([a] * half + [b] * half) * 5
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp) in enumerate(out):
            if f > 0 and seq[f] == seq[f - 1]:
                self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f} is held but emitted FLICKER")

    def test_third_state_breaks_cycle(self):
        """A third state breaking the cycle immediately resets tracking."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 1, 2, 3
        half = 10
        # 2 complete cycles (a*10 + b*10)*2, then toggle to a (3rd toggle -> FLICKER), then c
        seq = ([a] * half + [b] * half) * 2 + [a] * half
        out = _feed_sequence(tracker, [[x] for x in seq])

        # Frame 40 is 3rd toggle to A -> C_FLICKER
        self.assertEqual(int(out[40][0][0]), C_FLICKER)

        # Feed 3rd state c on frame 50
        st, hp = tracker.update([c])
        self.assertEqual(int(st[0]), C_CHANGED)
        self.assertEqual(int(hp[0]), 0)

    def test_scrolling_sequence_of_distinct_hashes(self):
        """Scrolling sequence of distinct hashes is never classified as FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = list(range(100, 220))
        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        self.assertTrue(all(s != C_FLICKER for s in states))

    def test_slow_fade_of_40_distinct_hashes(self):
        """Slow fade producing a new hash every few frames is never classified as FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for h in range(1000, 1040):
            seq.extend([h] * 3)  # distinct hash held for 3 frames
        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        self.assertTrue(all(s != C_FLICKER for s in states))

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 50, 60
        # Toggle A -> B (1st toggle), B -> A (2nd toggle), then hold A for 100 frames
        seq = [a] * 10 + [b] * 10 + [a] * 100
        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        self.assertTrue(all(s != C_FLICKER for s in states))

    def test_max_half_boundaries_1_and_64(self):
        """Half-period boundaries max_half=1 and max_half=64 are correctly handled."""
        # max_half = 1
        tracker1 = FlickerTracker(n_tiles=1, max_half=1)
        seq1 = [1, 2] * 10
        out1 = _feed_sequence(tracker1, [[x] for x in seq1])
        # Frame 0 is init, Frame 1 is 1st toggle, Frame 2 is 2nd toggle, Frame 3 is 3rd toggle
        self.assertEqual(int(out1[3][0][0]), C_FLICKER)
        self.assertEqual(int(out1[3][1][0]), 1)

        # max_half = 64
        tracker64 = FlickerTracker(n_tiles=1, max_half=64)
        half = 64
        seq64 = ([100] * half + [200] * half) * 3
        out64 = _feed_sequence(tracker64, [[x] for x in seq64])
        # 3rd toggle is at frame 3 * 64 = 192
        self.assertEqual(int(out64[192][0][0]), C_FLICKER)
        self.assertEqual(int(out64[192][1][0]), 64)

    def test_ops_increases_and_resets(self):
        """OPS increases on tracker updates and resets to zero with reset_ops()."""
        reset_ops()
        self.assertEqual(fl_blinkprior.OPS, 0)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        tracker.update([1, 2, 3, 4])
        ops_init = fl_blinkprior.OPS
        self.assertGreater(ops_init, 0)

        tracker.update([1, 2, 3, 5])
        ops_step2 = fl_blinkprior.OPS
        self.assertGreater(ops_step2, ops_init)

        reset_ops()
        self.assertEqual(fl_blinkprior.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker parameters or hashes raise ValueError matching prototype."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=10, max_half=0)

        tracker = FlickerTracker(n_tiles=2)
        with self.assertRaises(ValueError):
            tracker.update([100])  # length != n_tiles
        with self.assertRaises(ValueError):
            tracker.update([100, "invalid"])  # non-integer element
        with self.assertRaises(ValueError):
            tracker.update(np.array([[1, 2]]))  # wrong dimension


if __name__ == "__main__":
    unittest.main()
