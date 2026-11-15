"""Unit tests for eval/v2x/fl_caretblink.py (FlickerTracker)."""
import unittest
import numpy as np

import eval.v2x.fl_caretblink as fl_mod
from eval.v2x.fl_caretblink import FlickerTracker, C_FLICKER, C_UNCHANGED, C_CHANGED


class TestFLCaretBlink(unittest.TestCase):
    def test_synthetic_hash_sequences_half_periods(self):
        """Blink half-periods 7, 8, 15, 30, 60 over 120 frames with exact expected emission frames."""
        for half in (7, 8, 15, 30, 60):
            with self.subTest(half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                a, b = 101, 202
                # Generate sequence for 120 frames
                seq = []
                while len(seq) < 120:
                    phase_idx = len(seq) // half
                    seq.append(a if phase_idx % 2 == 0 else b)
                seq = seq[:120]

                out_states = []
                out_hps = []
                for f, h in enumerate(seq):
                    st, hp = tracker.update([h])
                    out_states.append(int(st[0]))
                    out_hps.append(int(hp[0]))

                # Calculate toggle frames
                toggles = [f for f in range(1, 120) if seq[f] != seq[f - 1]]
                # Cohort size is 1 (<= 4), so requires >= 2 complete runs (3rd toggle)
                expected_emission_toggles = toggles[2:]

                for f in range(120):
                    if f in expected_emission_toggles:
                        self.assertEqual(out_states[f], C_FLICKER, f"frame {f} for half={half}")
                        self.assertGreater(out_hps[f], 0, f"frame {f} for half={half}")
                    else:
                        self.assertNotEqual(out_states[f], C_FLICKER, f"frame {f} for half={half}")
                        self.assertEqual(out_hps[f], 0, f"frame {f} for half={half}")

    def test_jitter_plus_minus_one(self):
        """Plus or minus 1 frame jitter in run lengths is accepted."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        run_lengths = [15, 16, 14, 15, 14]
        seq = []
        for idx, r in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * r)

        toggles = []
        cur_f = 0
        for idx, r in enumerate(run_lengths):
            if idx > 0:
                toggles.append(cur_f)
            cur_f += r

        out_states = []
        for f, h in enumerate(seq):
            st, _ = tracker.update([h])
            out_states.append(int(st[0]))

        expected_emissions = toggles[2:]
        for f in expected_emissions:
            self.assertEqual(out_states[f], C_FLICKER, f"frame {f}")

    def test_asymmetric_duty_cycle(self):
        """Asymmetric duty cycle (e.g., 10 frames vs 20 frames) exceeds +/-1 jitter and is rejected."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        seq = ([a] * 10 + [b] * 20) * 5
        for f, h in enumerate(seq):
            st, _ = tracker.update([h])
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")

    def test_held_phases_never_emitted(self):
        """Held phases (non-toggle frames) NEVER emit FLICKER (state 4)."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        half = 15
        seq = ([a] * half + [b] * half) * 4
        for f, h in enumerate(seq):
            st, _ = tracker.update([h])
            if f > 0 and seq[f] == seq[f - 1]:
                self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")

    def test_third_state_breaks_cycle(self):
        """A third state breaks the cycle and clears state."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 10, 20, 30
        half = 10
        seq = ([a] * half + [b] * half) * 2 + [a] * half
        out = [tracker.update([h]) for h in seq]
        # At 3rd toggle (frame 40), FLICKER was emitted
        self.assertEqual(int(out[40][0][0]), C_FLICKER)

        # Introduce state c
        st, hp = tracker.update([c])
        self.assertEqual(int(st[0]), C_CHANGED)
        self.assertEqual(int(hp[0]), 0)

    def test_scrolling_sequence_distinct_hashes(self):
        """A scrolling sequence of distinct hashes never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = list(range(100, 200))
        for f, h in enumerate(seq):
            st, hp = tracker.update([h])
            self.assertNotEqual(int(st[0]), C_FLICKER)
            self.assertEqual(int(hp[0]), 0)

    def test_slow_fade_distinct_hashes(self):
        """A slow fade of 40 distinct hashes never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for h in range(40):
            seq.extend([1000 + h] * 3)
        for f, h in enumerate(seq):
            st, hp = tracker.update([h])
            self.assertNotEqual(int(st[0]), C_FLICKER)

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice and then stays static never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        seq = [a] * 10 + [b] * 10 + [a] * 10 + [a] * 50
        for f, h in enumerate(seq):
            st, hp = tracker.update([h])
            self.assertNotEqual(int(st[0]), C_FLICKER)

    def test_max_half_boundaries(self):
        """Boundary max_half=1 and max_half=64."""
        for max_h in (1, 64):
            with self.subTest(max_h=max_h):
                tracker = FlickerTracker(n_tiles=1, max_half=max_h)
                a, b = 1, 2
                seq = ([a] * max_h + [b] * max_h) * 4
                out = [tracker.update([h]) for h in seq]
                toggles = [f for f in range(1, len(seq)) if seq[f] != seq[f - 1]]
                emissions = [f for f, (st, _) in enumerate(out) if int(st[0]) == C_FLICKER]
                self.assertEqual(emissions, toggles[2:])

    def test_ops_increases_and_resets(self):
        """OPS increases during update calls and resets with reset_ops()."""
        fl_mod.reset_ops()
        self.assertEqual(fl_mod.OPS, 0)
        tracker = FlickerTracker(n_tiles=10, max_half=64)
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_mod.OPS, 0)
        tracker.update(np.arange(10, dtype=np.uint64))
        prev_ops = fl_mod.OPS
        self.assertGreater(prev_ops, 0)
        fl_mod.reset_ops()
        self.assertEqual(fl_mod.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid constructor arguments and update hashes raise ValueError."""
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=5, max_half=-1)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles="5")

        tracker = FlickerTracker(n_tiles=2)
        with self.assertRaises(ValueError):
            tracker.update([10])
        with self.assertRaises(ValueError):
            tracker.update(["a", "b"])

    def test_two_tiles_in_phase_cohort_small(self):
        """2 tiles blinking in phase with half-period 30 emit on toggles 3 and 4."""
        tracker = FlickerTracker(n_tiles=2, max_half=64)
        a, b = 100, 200
        half = 30
        # 4 complete runs = 5 phases = 150 frames
        seq = ([a] * half + [b] * half) * 3
        out_st = []
        for f, h in enumerate(seq):
            st, _ = tracker.update([h, h])
            out_st.append(st)

        toggles = [f for f in range(1, len(seq)) if seq[f] != seq[f - 1]]
        # Toggles at index 60, 90, 120, 150...
        # Toggles 3 and 4 are at indices 2 and 3 in toggles array (frame 90 and 120)
        self.assertEqual(int(out_st[toggles[2]][0]), C_FLICKER)
        self.assertEqual(int(out_st[toggles[2]][1]), C_FLICKER)
        self.assertEqual(int(out_st[toggles[3]][0]), C_FLICKER)
        self.assertEqual(int(out_st[toggles[3]][1]), C_FLICKER)

    def test_nine_tiles_toggling_together_cohort_large(self):
        """9 tiles toggling together require 4 complete runs (5th toggle) to emit."""
        tracker = FlickerTracker(n_tiles=9, max_half=64)
        a, b = 100, 200
        half = 15
        seq = ([a] * half + [b] * half) * 4
        out_st = []
        for f, h in enumerate(seq):
            st, _ = tracker.update([h] * 9)
            out_st.append(st)

        toggles = [f for f in range(1, len(seq)) if seq[f] != seq[f - 1]]
        # Toggles at toggles[0], toggles[1], toggles[2], toggles[3] (1st, 2nd, 3rd, 4th toggles)
        # 3rd and 4th toggles should NOT emit state 4 for cohort of size 9 (>4)
        self.assertNotEqual(int(out_st[toggles[2]][0]), C_FLICKER)
        self.assertNotEqual(int(out_st[toggles[3]][0]), C_FLICKER)
        # 5th toggle (toggles[4]) should emit state 4
        if len(toggles) > 4:
            self.assertEqual(int(out_st[toggles[4]][0]), C_FLICKER)


if __name__ == "__main__":
    unittest.main()
