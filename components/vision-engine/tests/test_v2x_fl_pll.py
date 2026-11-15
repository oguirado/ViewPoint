"""Unit tests for eval/v2x/fl_pll.py (FlickerTracker phase-locked toggle tracker)."""
import unittest
import numpy as np

from eval.v2x.fl_pll import (
    C_UNCHANGED,
    C_CHANGED,
    C_FLICKER,
    UNCHANGED,
    CHANGED,
    FLICKER,
    FlickerTracker,
    OPS,
    reset_ops,
)


def _feed_sequence(tracker, seq_per_frame):
    """Feed a list of per-frame hash sequences into tracker; return list of (states, half_periods)."""
    return [tracker.update(frame_hashes) for frame_hashes in seq_per_frame]


class TestFlickerTrackerPLL(unittest.TestCase):

    def setUp(self):
        reset_ops()

    def test_blink_half_periods_7_8_15_30_60_exact_emissions(self):
        """Test blink half-periods 7, 8, 15, 30 and 60 frames over 120 frames."""
        for half in (7, 8, 15, 30, 60):
            with self.subTest(half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                a, b = 0x1111222233334444, 0x9999AAAABBBBCCCC

                # Generate 120 frames of alternating phases
                seq = []
                for f in range(120):
                    phase_idx = f // half
                    seq.append(a if phase_idx % 2 == 0 else b)

                out = _feed_sequence(tracker, [[x] for x in seq])
                states = [int(s[0]) for s, _ in out]
                hps = [int(hp[0]) for _, hp in out]

                # Held frames MUST NEVER emit FLICKER (state 4)
                for f in range(120):
                    if f > 0 and seq[f] == seq[f - 1]:
                        self.assertEqual(states[f], C_UNCHANGED, f"frame {f} held phase must be UNCHANGED")
                        self.assertEqual(hps[f], 0, f"frame {f} held phase hp must be 0")

                # Check expected toggle emissions after 2 complete runs + 2 hits
                for f in range(120):
                    is_toggle = (f > 0 and seq[f] != seq[f - 1])
                    if is_toggle:
                        toggle_count = f // half
                        if toggle_count >= 4:
                            self.assertEqual(states[f], C_FLICKER, f"frame {f} toggle should emit FLICKER")
                            self.assertEqual(hps[f], half, f"frame {f} half_period estimate")
                        else:
                            self.assertEqual(states[f], C_CHANGED, f"frame {f} toggle before lock should be CHANGED")
                            self.assertEqual(hps[f], 0, f"frame {f} hp before lock should be 0")

    def test_jitter_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter and locks correctly."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        # Nominal half-period 15 with +/-1 jitter across phases: 15, 16, 14, 15, 14, 16
        run_lengths = [15, 16, 14, 15, 14, 16]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Calculate toggle frames
        toggle_frames = []
        curr_frame = 0
        for length in run_lengths[:-1]:
            curr_frame += length
            toggle_frames.append(curr_frame)

        # 4th toggle frame is toggle_frames[3]
        f_lock = toggle_frames[3]
        self.assertEqual(states[f_lock], C_FLICKER)
        self.assertIn(hps[f_lock], (14, 15, 16))

        # Excessive random jitter (> +/-1 around any predictable pattern, e.g. 15, 20, 10, 25) must not lock
        tracker_bad = FlickerTracker(n_tiles=1, max_half=64)
        bad_runs = [15, 20, 10, 25, 12, 22]
        seq_bad = []
        for idx, length in enumerate(bad_runs):
            seq_bad.extend([a if idx % 2 == 0 else b] * length)

        out_bad = _feed_sequence(tracker_bad, [[x] for x in seq_bad])
        for st, hp in out_bad:
            self.assertNotEqual(int(st[0]), C_FLICKER)

    def test_asymmetric_duty_cycle(self):
        """Asymmetric duty cycle (e.g., 10 frames phase A, 20 frames phase B)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 101, 202
        # Phases: 10, 20, 10, 20, 10, 20
        phases = [10, 20] * 3
        seq = []
        for idx, length in enumerate(phases):
            seq.extend([a if idx % 2 == 0 else b] * length)

        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]

        # Calculate toggle frames
        toggle_frames = []
        curr = 0
        for p in phases[:-1]:
            curr += p
            toggle_frames.append(curr)

        # 4th toggle (end of 4th phase) achieves lock
        f_lock = toggle_frames[3]  # 10 + 20 + 10 + 20 = 60
        self.assertEqual(states[f_lock], C_FLICKER)

        # Held frames must be UNCHANGED
        for f in range(len(seq)):
            if f not in toggle_frames:
                self.assertEqual(states[f], C_UNCHANGED)

    def test_held_phases_never_emitted(self):
        """Held phases (where hash did not change) MUST NEVER be emitted as C_FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 12345, 67890
        seq = ([a] * 10 + [b] * 10) * 10
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp) in enumerate(out):
            if f > 0 and seq[f] == seq[f - 1]:
                self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f} held phase emitted FLICKER")
                self.assertEqual(int(st[0]), C_UNCHANGED, f"frame {f} held phase must be UNCHANGED")
                self.assertEqual(int(hp[0]), 0)

    def test_third_state_breaks_cycle(self):
        """A third hash immediately breaks cycle and drops lock."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 111, 222, 333
        half = 10
        seq = ([a] * half + [b] * half) * 3
        out = _feed_sequence(tracker, [[x] for x in seq])

        # Verify 4th toggle emitted FLICKER
        self.assertEqual(int(out[40][0][0]), C_FLICKER)

        # Frame 60: Introduce third state c
        st, hp = tracker.update([c])
        self.assertEqual(int(st[0]), C_CHANGED)
        self.assertEqual(int(hp[0]), 0)

        # Holding c gives UNCHANGED
        st2, hp2 = tracker.update([c])
        self.assertEqual(int(st2[0]), C_UNCHANGED)
        self.assertEqual(int(hp2[0]), 0)

    def test_scrolling_sequence_distinct_hashes(self):
        """Scrolling sequence of distinct hashes never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[1000 + i] for i in range(100)]
        out = _feed_sequence(tracker, seq)

        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_slow_fade_40_distinct_hashes(self):
        """Slow fade of 40 distinct hashes (changing every 3 frames) never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for i in range(40):
            seq.extend([[5000 + i]] * 3)

        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_toggle_twice_then_static(self):
        """Tile that toggles twice and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 77, 88
        seq = [a] * 10 + [b] * 10 + [a] * 50
        out = _feed_sequence(tracker, [[x] for x in seq])

        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_max_half_boundaries_1_and_64(self):
        """Max half boundaries 1 and 64 are handled correctly."""
        for max_half in (1, 64):
            with self.subTest(max_half=max_half):
                tracker = FlickerTracker(n_tiles=1, max_half=max_half)
                half = max_half
                seq = ([100] * half + [200] * half) * 3
                out = _feed_sequence(tracker, [[x] for x in seq])

                states = [int(s[0]) for s, _ in out]
                f_lock = 4 * half
                self.assertEqual(states[f_lock], C_FLICKER)

    def test_ops_increases_and_resets(self):
        """OPS increases during update and resets via reset_ops."""
        reset_ops()
        from eval.v2x.fl_pll import OPS as initial_ops
        self.assertEqual(initial_ops, 0)

        tracker = FlickerTracker(n_tiles=10, max_half=64)
        tracker.update(np.arange(10, dtype=np.uint64))

        from eval.v2x.fl_pll import OPS as ops1
        self.assertGreater(ops1, 0)

        tracker.update(np.arange(10, dtype=np.uint64))
        from eval.v2x.fl_pll import OPS as ops2
        self.assertGreater(ops2, ops1)

        reset_ops()
        from eval.v2x.fl_pll import OPS as ops_reset
        self.assertEqual(ops_reset, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid initialization and update inputs raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=True)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles="10")

        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=4, max_half=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=4, max_half=-1)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=4, max_half=False)

        tracker = FlickerTracker(n_tiles=4, max_half=64)
        with self.assertRaises(ValueError):
            tracker.update([1, 2, 3])
        with self.assertRaises(ValueError):
            tracker.update(np.ones((4, 4), dtype=np.uint64))
        with self.assertRaises(ValueError):
            tracker.update(np.ones(4, dtype=bool))
        with self.assertRaises(ValueError):
            tracker.update([1, 2, "3", 4])

    def test_many_tiles_at_once(self):
        """Vectorised tracking across multiple tiles with distinct behaviours simultaneously."""
        n_tiles = 64
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)

        for f in range(90):
            row = np.zeros(n_tiles, dtype=np.uint64)
            row[0] = 1000 if ((f // 15) % 2 == 0) else 2000
            row[1] = 5555
            row[2] = 10000 + f

            st, hp = tracker.update(row)

            self.assertEqual(int(st[1]), C_UNCHANGED)
            self.assertEqual(int(hp[1]), 0)

            self.assertNotEqual(int(st[2]), C_FLICKER)
            self.assertEqual(int(hp[2]), 0)

            if f in (60, 75):
                self.assertEqual(int(st[0]), C_FLICKER)
                self.assertEqual(int(hp[0]), 15)


if __name__ == "__main__":
    unittest.main()
