"""Unit tests for eval/v2x/fl_autocorr.py (FlickerTracker autocorrelation variant)."""
import unittest

import numpy as np

import eval.v2x.fl_autocorr as fl_mod
from eval.v2x.fl_autocorr import (
    C_CHANGED,
    C_FLICKER,
    C_UNCHANGED,
    FLICKER,
    FlickerTracker,
    UNCHANGED,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlAutocorr(unittest.TestCase):
    def test_blink_half_periods_exact_emission_frames(self):
        """Blink half-periods 7, 8, 15, 30, and 60 over 120 frames emit FLICKER (4) ONLY on toggle frames after 3 runs."""
        for half in (7, 8, 15, 30, 60):
            with self.subTest(half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                a, b = 100, 200
                seq = []
                for frame in range(120):
                    phase_idx = frame // half
                    seq.append([a if (phase_idx % 2 == 0) else b])

                out = _feed_sequence(tracker, seq)
                states = [int(s[0]) for s, _ in out]
                hps = [int(hp[0]) for _, hp in out]

                expected_emissions = []
                # Toggles happen at frame = k * half for k >= 1.
                # Runs completed at toggle k:
                # toggle k=1 (frame half): 1st run (length half) completes.
                # toggle k=2 (frame 2*half): 2nd run completes (nr0=half, nr1=half).
                # toggle k=3 (frame 3*half): 3rd run completes -> 3 consecutive matching runs -> EMIT!
                # toggle k=4 (frame 4*half): 4th run completes -> 3 consecutive matching runs -> EMIT!
                # and so on for all k * half < 120 with k >= 3.
                for k in range(3, 120 // half + 1):
                    toggle_frame = k * half
                    if toggle_frame < 120:
                        expected_emissions.append(toggle_frame)

                for frame in range(120):
                    if frame in expected_emissions:
                        self.assertEqual(states[frame], C_FLICKER, f"half={half}, frame={frame}")
                        self.assertEqual(hps[frame], half, f"half={half}, frame={frame}")
                    else:
                        self.assertNotEqual(states[frame], C_FLICKER, f"half={half}, frame={frame}")

    def test_plus_minus_one_frame_jitter(self):
        """Tolerates +/-1 frame half-period jitter and emits on toggle frames after 3 matching runs."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        run_lengths = [15, 16, 14, 15, 14, 16, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([[a if idx % 2 == 0 else b]] * length)

        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Toggle frames occur at cumulative sum of run_lengths
        cum_runs = np.cumsum(run_lengths)
        # 3rd run completes at cum_runs[2] (frame 15+16+14 = 45)
        # 4th run completes at cum_runs[3] (frame 45+15 = 60)
        # 5th run completes at cum_runs[4] (frame 60+14 = 74)
        # 6th run completes at cum_runs[5] (frame 74+16 = 90)
        # 7th run completes at cum_runs[6] (frame 90+15 = 105)
        expected_toggle_emissions = [cum_runs[2], cum_runs[3], cum_runs[4], cum_runs[5]]

        for f in range(len(seq)):
            if f in expected_toggle_emissions:
                self.assertEqual(states[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps[f], 15, f"frame {f}")
            else:
                self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_asymmetric_duty_cycle(self):
        """Asymmetric duty cycle (e.g. 10 frames A, 12 frames B) within +/-1 of estimated half-period 11."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        run_lengths = [10, 12, 10, 12, 10, 12]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([[a if idx % 2 == 0 else b]] * length)

        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        cum = np.cumsum(run_lengths)
        # Runs 10, 12, 10 -> rmin3=10, rmax3=12, diff=2 <= 2 -> h3=(10+12)//2 = 11.
        # Toggle 3 at cum[2] = 32 -> emits FLICKER with hp=11.
        # Toggle 4 at cum[3] = 44 -> emits FLICKER with hp=11.
        expected_emissions = [cum[2], cum[3], cum[4]]
        for f in range(len(seq)):
            if f in expected_emissions:
                self.assertEqual(states[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps[f], 11, f"frame {f}")
            else:
                self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_held_phases_never_emitted(self):
        """Held frames where tile hash does not change are NEVER emitted as FLICKER (4)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 1, 2
        half = 10
        # 10 full cycles
        seq = ([[a]] * half + [[b]] * half) * 10
        out = _feed_sequence(tracker, seq)

        for f, (s, hp) in enumerate(out):
            # Check held frames (where hash == previous frame's hash)
            if f > 0 and seq[f] == seq[f - 1]:
                self.assertEqual(int(s[0]), C_UNCHANGED, f"held frame {f}")
                self.assertNotEqual(int(s[0]), C_FLICKER, f"held frame {f}")

    def test_third_state_breaks_cycle(self):
        """A third state immediately breaks the cycle, resetting tracker state."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 11, 22, 33
        half = 8
        seq = ([[a]] * half + [[b]] * half) * 3
        _feed_sequence(tracker, seq)

        # Introduce third state c
        st, hp = tracker.update([c])
        self.assertEqual(int(st[0]), C_CHANGED)
        self.assertEqual(int(hp[0]), 0)

        # Holding c gives UNCHANGED
        st2, hp2 = tracker.update([c])
        self.assertEqual(int(st2[0]), C_UNCHANGED)
        self.assertEqual(int(hp2[0]), 0)

    def test_scrolling_sequence_of_distinct_hashes(self):
        """Scrolling sequence with a new distinct hash every frame never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[i + 1000] for i in range(100)]
        out = _feed_sequence(tracker, seq)

        for f, (s, hp) in enumerate(out):
            self.assertNotEqual(int(s[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_slow_fade_of_40_distinct_hashes(self):
        """Slow fade where hash changes every 3 frames across 40 distinct hashes never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for i in range(40):
            seq.extend([[i * 100]] * 3)

        out = _feed_sequence(tracker, seq)
        for f, (s, hp) in enumerate(out):
            self.assertNotEqual(int(s[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_toggle_twice_then_static(self):
        """Tile that toggles twice (A -> B -> A -> A...) and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 100, 200
        # A for 10, B for 10, A for 50
        seq = [[a]] * 10 + [[b]] * 10 + [[a]] * 50
        out = _feed_sequence(tracker, seq)

        for f, (s, hp) in enumerate(out):
            self.assertNotEqual(int(s[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_max_half_boundaries_1_and_64(self):
        """Boundaries max_half=1 and max_half=64 work as expected."""
        for half, max_half in ((1, 1), (64, 64)):
            with self.subTest(half=half, max_half=max_half):
                tracker = FlickerTracker(n_tiles=1, max_half=max_half)
                a, b = 1, 2
                seq = ([[a]] * half + [[b]] * half) * 5
                out = _feed_sequence(tracker, seq)

                states = [int(s[0]) for s, _ in out]
                hps = [int(hp[0]) for _, hp in out]

                cum = [k * half for k in range(1, 11)]
                expected_emissions = cum[2:]  # 3rd run and beyond
                for f in range(len(seq)):
                    if f in expected_emissions:
                        self.assertEqual(states[f], C_FLICKER, f"frame {f}")
                        self.assertEqual(hps[f], half, f"frame {f}")
                    else:
                        self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_ops_increases_and_resets(self):
        """OPS increases on tracker operations and resets to 0 via reset_ops()."""
        fl_mod.reset_ops()
        self.assertEqual(fl_mod.OPS, 0)

        tracker = FlickerTracker(n_tiles=10, max_half=64)
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_mod.OPS, 0)

        ops_after_first = fl_mod.OPS
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_mod.OPS, ops_after_first)

        fl_mod.reset_ops()
        self.assertEqual(fl_mod.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker init or update inputs raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=10, max_half=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=10, max_half=-1)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        # Wrong length
        with self.assertRaises(ValueError):
            tracker.update([1, 2, 3])
        # Non-integer element
        with self.assertRaises(ValueError):
            tracker.update([1, 2, "3", 4])
        # Wrong shape numpy array
        with self.assertRaises(ValueError):
            tracker.update(np.ones((2, 2), dtype=np.uint64))


if __name__ == "__main__":
    unittest.main()
