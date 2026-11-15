"""Unit tests for eval/v2x/fl_hyst2.py (FlickerTracker)."""
import unittest
import numpy as np

from eval.v2x import fl_hyst2
from eval.v2x.fl_hyst2 import (
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


class TestFlickerTrackerHyst2(unittest.TestCase):
    def setUp(self):
        reset_ops()

    def test_blink_half_periods_7_8_15_30_60_over_120_frames(self):
        """Exact emission frames for blink half-periods 7, 8, 15, 30 and 60 over 120 frames."""
        a, b = 0x1111222233334444, 0x9999AAAABBBBCCCC

        # --- Half period 7 ---
        tracker7 = FlickerTracker(n_tiles=1, max_half=64)
        seq7 = []
        for i in range(120):
            seq7.append([a if (i // 7) % 2 == 0 else b])
        out7 = _feed_sequence(tracker7, seq7)
        states7 = [int(s[0]) for s, _ in out7]
        hps7 = [int(hp[0]) for _, hp in out7]

        # Emissions happen strictly on toggle frames starting at Toggle 3 (frame 21, 28, 35, 42, ...)
        expected_emissions_7 = set(range(21, 120, 7))
        for f in range(120):
            if f in expected_emissions_7:
                self.assertEqual(states7[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps7[f], 7, f"frame {f}")
            else:
                self.assertNotEqual(states7[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps7[f], 0, f"frame {f}")

        # --- Half period 8 ---
        tracker8 = FlickerTracker(n_tiles=1, max_half=64)
        seq8 = [[a if (i // 8) % 2 == 0 else b] for i in range(120)]
        out8 = _feed_sequence(tracker8, seq8)
        states8 = [int(s[0]) for s, _ in out8]
        hps8 = [int(hp[0]) for _, hp in out8]
        expected_emissions_8 = set(range(24, 120, 8))
        for f in range(120):
            if f in expected_emissions_8:
                self.assertEqual(states8[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps8[f], 8, f"frame {f}")
            else:
                self.assertNotEqual(states8[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps8[f], 0, f"frame {f}")

        # --- Half period 15 ---
        tracker15 = FlickerTracker(n_tiles=1, max_half=64)
        seq15 = [[a if (i // 15) % 2 == 0 else b] for i in range(120)]
        out15 = _feed_sequence(tracker15, seq15)
        states15 = [int(s[0]) for s, _ in out15]
        hps15 = [int(hp[0]) for _, hp in out15]
        expected_emissions_15 = {45, 60, 75, 90, 105}
        for f in range(120):
            if f in expected_emissions_15:
                self.assertEqual(states15[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps15[f], 15, f"frame {f}")
            else:
                self.assertNotEqual(states15[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps15[f], 0, f"frame {f}")

        # --- Half period 30 ---
        tracker30 = FlickerTracker(n_tiles=1, max_half=64)
        seq30 = [[a if (i // 30) % 2 == 0 else b] for i in range(120)]
        out30 = _feed_sequence(tracker30, seq30)
        states30 = [int(s[0]) for s, _ in out30]
        hps30 = [int(hp[0]) for _, hp in out30]
        expected_emissions_30 = {90}
        for f in range(120):
            if f in expected_emissions_30:
                self.assertEqual(states30[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps30[f], 30, f"frame {f}")
            else:
                self.assertNotEqual(states30[f], C_FLICKER, f"frame {f}")
                self.assertEqual(hps30[f], 0, f"frame {f}")

        # --- Half period 60 ---
        tracker60 = FlickerTracker(n_tiles=1, max_half=64)
        seq60 = [[a if (i // 60) % 2 == 0 else b] for i in range(120)]
        out60 = _feed_sequence(tracker60, seq60)
        states60 = [int(s[0]) for s, _ in out60]
        hps60 = [int(hp[0]) for _, hp in out60]
        for f in range(120):
            self.assertNotEqual(states60[f], C_FLICKER, f"frame {f}")
            self.assertEqual(hps60[f], 0, f"frame {f}")

    def test_jittered_period_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter across runs."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 100, 200
        run_lengths = [15, 16, 14, 15, 14, 16, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([[a if idx % 2 == 0 else b]] * length)

        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Toggle 1 at frame 15
        # Toggle 2 at frame 15+16 = 31
        # Toggle 3 at frame 31+14 = 45 -> locks and emits!
        toggle_frames = []
        cum = 0
        for length in run_lengths[:-1]:
            cum += length
            toggle_frames.append(cum)

        self.assertEqual(states[45], C_FLICKER)
        self.assertEqual(hps[45], 15)

        for tf in toggle_frames[2:]:  # toggle 3 onwards
            self.assertEqual(states[tf], C_FLICKER, f"toggle frame {tf}")
            self.assertEqual(hps[tf], 15, f"toggle frame {tf}")

        # Non-toggle frames emit 0
        for f in range(len(seq)):
            if f not in toggle_frames[2:]:
                self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")

    def test_asymmetric_duty_cycle(self):
        """Asymmetric run lengths differing by >1 frame do not lock."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 10, 20
        # 10 frames of A, 20 frames of B, 10 frames of A, 20 frames of B...
        seq = []
        for i in range(6):
            length = 10 if i % 2 == 0 else 20
            seq.extend([[a if i % 2 == 0 else b]] * length)
        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Never emits FLICKER because runs 10 and 20 do not agree within +/-1 frame
        for f in range(len(seq)):
            self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")
            self.assertEqual(hps[f], 0, f"frame {f}")

    def test_held_phases_never_emitted(self):
        """Non-toggle frames never emit FLICKER (4), even during a locked cycle."""
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

    def test_third_state_breaks_cycle(self):
        """A third hash drops state back to IDLE immediately."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 1, 2, 3
        # Establish locked cycle: A x 10, B x 10, A x 10, B x 10
        seq = [[a if (i // 10) % 2 == 0 else b] for i in range(40)]
        out1 = _feed_sequence(tracker, seq)
        self.assertEqual(int(out1[30][0][0]), C_FLICKER)

        # Frame 40: introduce third hash c -> drops to IDLE, returns C_CHANGED (2)
        st_c, hp_c = tracker.update([c])
        self.assertEqual(int(st_c[0]), C_CHANGED)
        self.assertEqual(int(hp_c[0]), 0)

        # Held c returns UNCHANGED (0)
        st_c2, hp_c2 = tracker.update([c])
        self.assertEqual(int(st_c2[0]), C_UNCHANGED)
        self.assertEqual(int(hp_c2[0]), 0)

    def test_scrolling_sequence_of_distinct_hashes(self):
        """Scrolling producing unique hashes on every frame never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = [[1000 + i] for i in range(120)]
        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")
            if f == 0:
                self.assertEqual(int(st[0]), C_UNCHANGED)
            else:
                self.assertEqual(int(st[0]), C_CHANGED)

    def test_slow_fade_of_40_distinct_hashes(self):
        """Slow fade of 40 distinct hashes (3 frames per hash) never emits FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        seq = []
        for h in range(40):
            seq.extend([[100 + h]] * 3)
        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), C_FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_toggle_twice_then_static(self):
        """A tile that toggles twice (A->B->A->B) and then stays static stops emitting FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 50, 60
        # A x 10, B x 10, A x 10, then B x 50
        seq = [[a]] * 10 + [[b]] * 10 + [[a]] * 10 + [[b]] * 50
        out = _feed_sequence(tracker, seq)
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Toggle 3 at frame 30 emits FLICKER
        self.assertEqual(states[30], C_FLICKER)
        self.assertEqual(hps[30], 10)

        # Held B from frame 31 onwards never emits FLICKER
        for f in range(31, 80):
            self.assertNotEqual(states[f], C_FLICKER, f"frame {f}")
            self.assertEqual(hps[f], 0, f"frame {f}")

    def test_max_half_boundaries_1_and_64(self):
        """Half-period boundaries 1 and 64 work correctly."""
        # max_half = 1
        tracker1 = FlickerTracker(n_tiles=1, max_half=1)
        seq1 = [[10 if i % 2 == 0 else 20] for i in range(10)]
        out1 = _feed_sequence(tracker1, seq1)
        # Frame 0: 0, Frame 1: 2, Frame 2: 2 (CANDIDATE), Frame 3: 4 (LOCKED, hp 1)
        self.assertEqual(int(out1[3][0][0]), C_FLICKER)
        self.assertEqual(int(out1[3][1][0]), 1)

        # max_half = 64
        tracker64 = FlickerTracker(n_tiles=1, max_half=64)
        seq64 = [[10 if (i // 64) % 2 == 0 else 20] for i in range(256)]
        out64 = _feed_sequence(tracker64, seq64)
        # Toggle 3 at frame 192 emits FLICKER hp 64
        self.assertEqual(int(out64[192][0][0]), C_FLICKER)
        self.assertEqual(int(out64[192][1][0]), 64)

    def test_ops_counter_increases_and_resets(self):
        """OPS increases on tracker updates and resets with reset_ops."""
        reset_ops()
        self.assertEqual(fl_hyst2.OPS, 0)
        tracker = FlickerTracker(n_tiles=10, max_half=64)
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_hyst2.OPS, 0)

        prev_ops = fl_hyst2.OPS
        tracker.update(np.arange(10, dtype=np.uint64))
        self.assertGreater(fl_hyst2.OPS, prev_ops)

        reset_ops()
        self.assertEqual(fl_hyst2.OPS, 0)

    def test_invalid_inputs_raise_value_error(self):
        """Invalid tracker parameters or hashes raise ValueError."""
        # Invalid n_tiles
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=-5)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=True)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles="10")

        # Invalid max_half
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=1, max_half=0)
        with self.assertRaises(ValueError):
            FlickerTracker(n_tiles=1, max_half=False)

        # Invalid hashes
        tracker = FlickerTracker(n_tiles=4)
        with self.assertRaises(ValueError):
            tracker.update([1, 2, 3])  # wrong length
        with self.assertRaises(ValueError):
            tracker.update(np.zeros((2, 2), dtype=np.uint64))  # 2D
        with self.assertRaises(ValueError):
            tracker.update(np.zeros(4, dtype=np.float64))  # float
        with self.assertRaises(ValueError):
            tracker.update([1, 2, "abc", 4])  # invalid element

    def test_many_tiles_vectorised(self):
        """Vectorised multi-tile processing across 128 tiles."""
        n_tiles = 128
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)

        for f in range(120):
            hashes = np.empty(n_tiles, dtype=np.uint64)
            # Tiles 0..31: static
            hashes[0:32] = np.arange(32, dtype=np.uint64)
            # Tiles 32..63: blink 15
            hashes[32:64] = 1000 if (f // 15) % 2 == 0 else 2000
            # Tiles 64..95: blink 30
            hashes[64:96] = 3000 if (f // 30) % 2 == 0 else 4000
            # Tiles 96..127: scrolling distinct hashes per frame
            hashes[96:128] = np.arange(10000 + f * 32, 10032 + f * 32, dtype=np.uint64)

            st, hp = tracker.update(hashes)

            # Verification for specific frames
            if f in (45, 60, 75, 90, 105):
                self.assertTrue(np.all(st[32:64] == C_FLICKER), f"frame {f} tile 32..63 state")
                self.assertTrue(np.all(hp[32:64] == 15), f"frame {f} tile 32..63 hp")
            else:
                self.assertTrue(np.all(st[32:64] != C_FLICKER), f"frame {f} tile 32..63 state")

            if f == 90:
                self.assertTrue(np.all(st[64:96] == C_FLICKER), f"frame {f} tile 64..95 state")
                self.assertTrue(np.all(hp[64:96] == 30), f"frame {f} tile 64..95 hp")
            else:
                self.assertTrue(np.all(st[64:96] != C_FLICKER), f"frame {f} tile 64..95 state")

            # Static and scrolling tiles never FLICKER
            self.assertTrue(np.all(st[0:32] == C_UNCHANGED))
            self.assertTrue(np.all(st[96:128] != C_FLICKER))


if __name__ == "__main__":
    unittest.main()
