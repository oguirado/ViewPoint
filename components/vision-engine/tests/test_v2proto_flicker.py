"""Unit tests for eval/v2proto_flicker.py (FlickerTracker)."""
import hashlib
import unittest

import numpy as np

from eval.v2proto_flicker import (
    CHANGED,
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


def _tile_xh64_row(rgb_frame, tile_s=16):
    """Compute deterministic 64-bit integer descriptor hashes per tile in memory (no disk I/O)."""
    h, w, _ = rgb_frame.shape
    gh, gw = h // tile_s, w // tile_s
    hashes = np.empty(gh * gw, dtype=np.uint64)
    idx = 0
    for ty in range(gh):
        y0 = ty * tile_s
        for tx in range(gw):
            x0 = tx * tile_s
            tile = np.ascontiguousarray(rgb_frame[y0:y0 + tile_s, x0:x0 + tile_s])
            digest = hashlib.blake2b(tile.tobytes(), digest_size=8, person=b"vqa-tile").digest()
            hashes[idx] = int.from_bytes(digest, "little", signed=False)
            idx += 1
    return hashes


class TestFlickerTracker(unittest.TestCase):
    def test_caret_blink_15_frames_per_phase_30fps(self):
        """500 ms caret blink at 30 fps (15 frames per phase) declares FLICKER after 2 full periods."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 0x1111222233334444, 0x9999AAAABBBBCCCC
        half = 15
        # 3 full periods = 6 half-periods of 15 frames = 90 frames
        phases = ([a] * half + [b] * half) * 3
        out = _feed_sequence(tracker, [[x] for x in phases])

        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Frame 0 is UNCHANGED; before 2 full periods (frames 0..58) never FLICKER
        self.assertEqual(states[0], C_UNCHANGED)
        self.assertTrue(all(s != C_FLICKER for s in states[: 4 * half - 1]))
        self.assertTrue(all(hp == 0 for hp in hps[: 4 * half - 1]))

        # At 2 full periods (frame 59, end of 4th half-period) and throughout 3rd period: FLICKER with half_period=15
        for f in range(4 * half - 1, 6 * half):
            self.assertEqual(states[f], C_FLICKER, f"frame {f}")
            self.assertEqual(hps[f], half, f"frame {f}")

    def test_caret_blink_30_frames_per_phase_60fps(self):
        """500 ms caret blink at 60 fps (30 frames per phase) declares FLICKER after 2 full periods."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 1001, 2002
        half = 30
        phases = ([a] * half + [b] * half) * 3
        out = _feed_sequence(tracker, [[x] for x in phases])

        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        self.assertTrue(all(s != C_FLICKER for s in states[: 4 * half - 1]))
        for f in range(4 * half - 1, 6 * half):
            self.assertEqual(states[f], C_FLICKER, f"frame {f}")
            self.assertEqual(hps[f], half, f"frame {f}")

    def test_in_memory_synthetic_caret_blink_frames_15_and_30(self):
        """In-memory synthetic GUI caret blink frames (30 fps and 60 fps) without touching disk."""
        w, h, tile_s = 64, 32, 16
        n_tiles = (w // tile_s) * (h // tile_s)
        bg = np.full((h, w, 3), 240, dtype=np.uint8)
        caret_on = bg.copy()
        # Caret inside tile (tx=1, ty=0) -> tile index 1
        caret_on[2:14, 20:22, :] = 20

        for half in (15, 30):
            with self.subTest(half_period=half):
                tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
                last_states = None
                last_hp = None
                for phase_idx in range(6):
                    frame = caret_on if (phase_idx % 2 == 0) else bg
                    hashes = _tile_xh64_row(frame, tile_s=tile_s)
                    for _ in range(half):
                        last_states, last_hp = tracker.update(hashes)
                self.assertEqual(int(last_states[1]), FLICKER)
                self.assertEqual(int(last_hp[1]), half)
                for t in range(n_tiles):
                    if t != 1:
                        self.assertEqual(int(last_states[t]), UNCHANGED)
                        self.assertEqual(int(last_hp[t]), 0)

    def test_boundary_half_periods_1_and_64(self):
        """Half-period boundaries 1 and 64 are recognised."""
        for half in (1, 64):
            with self.subTest(half=half):
                tracker = FlickerTracker(n_tiles=1, max_half=64)
                seq = ([10] * half + [20] * half) * 3
                out = _feed_sequence(tracker, [[x] for x in seq])
                states = [int(s[0]) for s, _ in out]
                hps = [int(hp[0]) for _, hp in out]
                self.assertTrue(all(s != FLICKER for s in states[: 4 * half - 1]))
                for f in range(4 * half - 1, len(seq)):
                    self.assertEqual(states[f], FLICKER)
                    self.assertEqual(hps[f], half)

    def test_jittered_period_plus_minus_one(self):
        """Tolerates +/-1 frame half-period jitter and returns the measured nominal half-period."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 500, 600
        # Nominal half-period 15 with +/-1 jitter across phases: 15, 16, 14, 15, 14, 16, 15
        run_lengths = [15, 16, 14, 15, 14, 16, 15]
        seq = []
        for idx, length in enumerate(run_lengths):
            seq.extend([a if idx % 2 == 0 else b] * length)
        out = _feed_sequence(tracker, [[x] for x in seq])
        states = [int(s[0]) for s, _ in out]
        hps = [int(hp[0]) for _, hp in out]

        # Never emit during warm-up (frames 0..first_four-2), then FLICKER from first_four-1 onwards
        first_four = sum(run_lengths[:4])
        self.assertTrue(all(s != FLICKER for s in states[: first_four - 1]))
        self.assertTrue(all(hp == 0 for hp in hps[: first_four - 1]))
        self.assertEqual(states[first_four - 1], FLICKER)
        self.assertEqual(hps[first_four - 1], 15)
        for f in range(first_four - 1, len(seq)):
            self.assertEqual(states[f], FLICKER, f"frame {f}")
            self.assertEqual(hps[f], 15, f"frame {f}")

        # Also test when the 4th phase is 1 frame shorter (15, 15, 15, 14) followed by transition to A
        tracker2 = FlickerTracker(n_tiles=1, max_half=64)
        seq2 = [a] * 15 + [b] * 15 + [a] * 15 + [b] * 14 + [a] * 15
        out2 = _feed_sequence(tracker2, [[x] for x in seq2])
        # Never emit during warm-up (frames 0..58); on transition frame 59, 4 half-periods completed
        self.assertTrue(all(int(st[0]) != FLICKER for st, _ in out2[:59]))
        self.assertEqual(int(out2[59][0][0]), FLICKER)
        self.assertEqual(int(out2[59][1][0]), 15)
        self.assertEqual(int(out2[-1][0][0]), FLICKER)
        self.assertEqual(int(out2[-1][1][0]), 15)

        # Excessive jitter (> +/-1 frame around any integer half-period, e.g. 15 vs 18) is rejected
        tracker3 = FlickerTracker(n_tiles=1, max_half=64)
        seq3 = ([a] * 15 + [b] * 18) * 4
        out3 = _feed_sequence(tracker3, [[x] for x in seq3])
        self.assertTrue(all(int(st[0]) != FLICKER for st, _ in out3))

    def test_third_state_breaks_flicker(self):
        """Any third state immediately drops FLICKER back to CHANGED and clears half-period to 0."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 11, 22, 33
        half = 10
        seq = ([a] * half + [b] * half) * 2 + [a] * 5
        out = _feed_sequence(tracker, [[x] for x in seq])
        self.assertEqual(int(out[-1][0][0]), FLICKER)
        self.assertEqual(int(out[-1][1][0]), half)

        # Introduce third state c: must immediately drop back to CHANGED with half_period 0
        st, hp = tracker.update([c])
        self.assertEqual(int(st[0]), CHANGED)
        self.assertEqual(int(hp[0]), 0)

        # Holding c gives UNCHANGED with half_period 0
        st2, hp2 = tracker.update([c])
        self.assertEqual(int(st2[0]), UNCHANGED)
        self.assertEqual(int(hp2[0]), 0)

        # Returning to a or b does not resume FLICKER without 2 full new periods
        st3, hp3 = tracker.update([a])
        self.assertEqual(int(st3[0]), CHANGED)
        self.assertEqual(int(hp3[0]), 0)

    def test_stall_drops_back_from_flicker(self):
        """If a flickering tile stops toggling and holds a state past half_period + 1, it drops back."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b = 101, 202
        half = 8
        seq = ([a] * half + [b] * half) * 2
        out = _feed_sequence(tracker, [[x] for x in seq])
        self.assertEqual(int(out[-1][0][0]), FLICKER)
        self.assertEqual(int(out[-1][1][0]), half)

        # Hold state a for half + 1 frames (still within +1 jitter) -> stays FLICKER
        for _ in range(half + 1):
            st, hp = tracker.update([a])
            self.assertEqual(int(st[0]), FLICKER)
            self.assertEqual(int(hp[0]), half)

        # One more frame of state a (cur_len == half + 2) -> stalls out to UNCHANGED, hp=0
        st_stall, hp_stall = tracker.update([a])
        self.assertEqual(int(st_stall[0]), UNCHANGED)
        self.assertEqual(int(hp_stall[0]), 0)

    def test_abbc_is_not_flicker(self):
        """A-B-B-C pattern (single or repeated) is never classified as FLICKER."""
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        a, b, c = 1, 2, 3
        seq = ([a, b, b, c]) * 20
        out = _feed_sequence(tracker, [[x] for x in seq])
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_slow_two_state_toggle_above_max_half_is_not_flicker(self):
        """Two-state toggling with half-period > max_half is never classified as FLICKER."""
        for max_half, slow_half in ((64, 65), (64, 80), (16, 20)):
            with self.subTest(max_half=max_half, slow_half=slow_half):
                tracker = FlickerTracker(n_tiles=1, max_half=max_half)
                seq = ([100] * slow_half + [200] * slow_half) * 4
                out = _feed_sequence(tracker, [[x] for x in seq])
                for f, (st, hp) in enumerate(out):
                    self.assertNotEqual(int(st[0]), FLICKER, f"frame {f}")
                    self.assertEqual(int(hp[0]), 0, f"frame {f}")
                    if f == 0 or seq[f] == seq[f - 1]:
                        self.assertEqual(int(st[0]), C_UNCHANGED)
                    else:
                        self.assertEqual(int(st[0]), C_CHANGED)

    def test_many_tiles_at_once(self):
        """Vectorised tracking across many tiles with distinct simultaneous behaviours."""
        n_tiles = 256
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
        n_frames = 140

        # Tile roles:
        # 0..63: static
        # 64..127: 15-frame caret blink (30 fps)
        # 128..191: 30-frame caret blink (60 fps)
        # 192..223: jittered half-period ~10 (10, 11, 9, 10, 10, 11, 9, ...)
        # 224..239: A-B-B-C non-flicker
        # 240..255: slow toggle (half-period 70 > max_half=64)
        jitter_runs = [10, 11, 9, 10, 10, 11, 9, 10, 11, 9, 10, 10, 11, 9, 10]
        jitter_seq = []
        for idx, length in enumerate(jitter_runs):
            jitter_seq.extend([7000 + (idx % 2)] * length)

        abbc = [1, 2, 2, 3]
        states_hist = []
        hp_hist = []

        for f in range(n_frames):
            row = np.zeros(n_tiles, dtype=np.uint64)
            # 0..63 static
            row[0:64] = np.arange(100, 164, dtype=np.uint64)
            # 64..127 blink 15
            row[64:128] = 1000 if ((f // 15) % 2 == 0) else 2000
            # 128..191 blink 30
            row[128:192] = 3000 if ((f // 30) % 2 == 0) else 4000
            # 192..223 jittered 10
            row[192:224] = jitter_seq[f]
            # 224..239 A-B-B-C
            row[224:240] = abbc[f % 4]
            # 240..255 slow toggle (half=70)
            row[240:256] = 8000 if ((f // 70) % 2 == 0) else 9000

            st, hp = tracker.update(row)
            states_hist.append(st)
            hp_hist.append(hp)

        final_st = states_hist[-1]
        final_hp = hp_hist[-1]

        # Static tiles: always UNCHANGED, hp=0
        self.assertTrue(np.all(final_st[0:64] == UNCHANGED))
        self.assertTrue(np.all(final_hp[0:64] == 0))

        # 15-frame blink tiles: FLICKER from frame 59 onwards, hp=15
        self.assertTrue(np.all(states_hist[58][64:128] != FLICKER))
        self.assertTrue(np.all(states_hist[59][64:128] == FLICKER))
        self.assertTrue(np.all(final_st[64:128] == FLICKER))
        self.assertTrue(np.all(final_hp[64:128] == 15))

        # 30-frame blink tiles: FLICKER from frame 119 onwards, hp=30
        self.assertTrue(np.all(states_hist[118][128:192] != FLICKER))
        self.assertTrue(np.all(states_hist[119][128:192] == FLICKER))
        self.assertTrue(np.all(final_st[128:192] == FLICKER))
        self.assertTrue(np.all(final_hp[128:192] == 30))

        # Jittered ~10 tiles: never FLICKER during warm-up (0..38), FLICKER from frame 39 onwards, hp=10
        self.assertTrue(np.all(states_hist[38][192:224] != FLICKER))
        self.assertTrue(np.all(states_hist[39][192:224] == FLICKER))
        self.assertTrue(np.all(final_st[192:224] == FLICKER))
        self.assertTrue(np.all(final_hp[192:224] == 10))

        # A-B-B-C and slow-toggle tiles: never FLICKER on any frame
        for f in range(n_frames):
            self.assertTrue(np.all(states_hist[f][224:240] != FLICKER))
            self.assertTrue(np.all(hp_hist[f][224:240] == 0))
            self.assertTrue(np.all(states_hist[f][240:256] != FLICKER))
            self.assertTrue(np.all(hp_hist[f][240:256] == 0))

    def test_fp_tile_changes_once_and_holds(self):
        """Regression: a tile that changes once and holds is never classified as FLICKER."""
        w, h, tile_s = 32, 32, 16
        n_tiles = (w // tile_s) * (h // tile_s)
        frame_a = np.full((h, w, 3), 240, dtype=np.uint8)
        frame_b = frame_a.copy()
        frame_b[4:12, 4:12, :] = 30
        ha = _tile_xh64_row(frame_a, tile_s=tile_s)
        hb = _tile_xh64_row(frame_b, tile_s=tile_s)

        for hold_a in (1, 10, 30):
            with self.subTest(hold_a=hold_a):
                tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)
                seq = [ha] * hold_a + [hb] * 90
                out = _feed_sequence(tracker, seq)
                for f, (st, hp) in enumerate(out):
                    self.assertTrue(np.all(st != FLICKER), f"frame {f}")
                    self.assertTrue(np.all(hp == 0), f"frame {f}")
                    if f == hold_a:
                        self.assertEqual(int(st[0]), C_CHANGED)
                    else:
                        self.assertEqual(int(st[0]), C_UNCHANGED)

    def test_fp_slow_fade_between_two_states(self):
        """Regression: a slow fade stepping between two endpoint states is never FLICKER."""
        w, h, tile_s = 16, 16, 16
        tracker = FlickerTracker(n_tiles=1, max_half=64)
        # Multi-step luma fade 20 -> 220 -> 20 with 4 frames per grey step
        levels = list(range(20, 221, 25)) + list(range(195, 19, -25))
        seq = []
        for lv in levels * 3:
            frame = np.full((h, w, 3), lv, dtype=np.uint8)
            h_val = _tile_xh64_row(frame, tile_s=tile_s)
            seq.extend([h_val] * 4)

        out = _feed_sequence(tracker, seq)
        for f, (st, hp) in enumerate(out):
            self.assertNotEqual(int(st[0]), FLICKER, f"frame {f}")
            self.assertEqual(int(hp[0]), 0, f"frame {f}")

    def test_fp_scroll_passing_over_tile(self):
        """Regression: a scroll passing over a tile (producing transient hashes then background) is never FLICKER."""
        w, h, tile_s = 32, 32, 16
        n_tiles = (w // tile_s) * (h // tile_s)
        tracker = FlickerTracker(n_tiles=n_tiles, max_half=64)

        # Build an in-memory canvas with a horizontal bar that scrolls vertically across tile (0, 0)
        canvas_h = 160
        canvas = np.full((canvas_h, w, 3), 235, dtype=np.uint8)
        for row in range(24, 56):
            canvas[row, :, :] = 30 + ((row * 17) % 150)

        for f in range(80):
            y0 = min(f * 2, canvas_h - h)
            frame = canvas[y0:y0 + h, :, :]
            hashes = _tile_xh64_row(frame, tile_s=tile_s)
            st, hp = tracker.update(hashes)
            self.assertTrue(np.all(st != FLICKER), f"frame {f}")
            self.assertTrue(np.all(hp == 0), f"frame {f}")

    def test_fp_caret_stops_blinking(self):
        """Regression: a caret that stops blinking drops FLICKER immediately after half_period + 1 and stays UNCHANGED."""
        a, b = 0xAAAA, 0xBBBB
        half = 10

        # Case 1: stops during warm-up after 2 half-periods (1 full period) and holds state a
        tracker_warm = FlickerTracker(n_tiles=1, max_half=64)
        out_warm2 = _feed_sequence(tracker_warm, [[x] for x in ([a] * half + [b] * half + [a] * 60)])
        for f, (st, hp) in enumerate(out_warm2):
            self.assertNotEqual(int(st[0]), FLICKER, f"warm-up stop frame {f}")
            self.assertEqual(int(hp[0]), 0, f"warm-up stop frame {f}")

        # Case 2: blinks for 2 full periods (4 half-periods), then stops blinking and holds state a for 60 frames
        tracker_active = FlickerTracker(n_tiles=1, max_half=64)
        seq_active = ([a] * half + [b] * half) * 2 + [a] * 60
        out_active = _feed_sequence(tracker_active, [[x] for x in seq_active])

        # Frame 4*half - 1 is FLICKER; during the 5th phase [a]*60, frames 0..half (cur_len 1..half+1) are within +1 jitter
        # From cur_len == half + 2 (frame 4*half + half + 1) through the end of the 60-frame hold, must NEVER be FLICKER
        drop_frame = 4 * half + (half + 1)
        self.assertEqual(int(out_active[drop_frame - 1][0][0]), FLICKER)
        for f in range(drop_frame, len(seq_active)):
            self.assertEqual(int(out_active[f][0][0]), C_UNCHANGED, f"frame {f}")
            self.assertEqual(int(out_active[f][1][0]), 0, f"frame {f}")

        # Case 3: after stalling on state a, a single toggle back to b and holding b must not re-declare FLICKER
        for f in range(30):
            st, hp = tracker_active.update([b])
            self.assertNotEqual(int(st[0]), FLICKER, f"post-stall frame {f}")
            self.assertEqual(int(hp[0]), 0, f"post-stall frame {f}")

    def test_fp_two_state_toggle_with_irregular_gaps(self):
        """Regression: two-state toggles with irregular gaps or ping-pong turnaround pauses never emit FLICKER."""
        a, b = 100, 200

        # Pattern A: irregular phase lengths between two states
        irregular_runs = [5, 18, 6, 25, 4, 15, 7, 22, 5, 19]
        seq_a = []
        for idx, length in enumerate(irregular_runs):
            seq_a.extend([a if idx % 2 == 0 else b] * length)
        tracker_a = FlickerTracker(n_tiles=1, max_half=64)
        for f, (st, hp) in enumerate(_feed_sequence(tracker_a, [[x] for x in seq_a])):
            self.assertNotEqual(int(st[0]), FLICKER, f"irregular A frame {f}")
            self.assertEqual(int(hp[0]), 0, f"irregular A frame {f}")

        # Pattern B: ping-pong turnaround pattern (3 short runs of 3 frames separated by 15-frame holds, as in caret_move/typing/window_drag)
        ping_pong_runs = [1, 3, 15, 3, 3, 3, 15, 3, 3, 3, 15, 3, 3, 3, 15]
        seq_b = []
        for idx, length in enumerate(ping_pong_runs):
            seq_b.extend([a if idx % 2 == 0 else b] * length)
        tracker_b = FlickerTracker(n_tiles=1, max_half=64)
        for f, (st, hp) in enumerate(_feed_sequence(tracker_b, [[x] for x in seq_b])):
            self.assertNotEqual(int(st[0]), FLICKER, f"ping-pong B frame {f}")
            self.assertEqual(int(hp[0]), 0, f"ping-pong B frame {f}")

        # Pattern C: asymmetric two-state duty cycle (2, 4, 2, 4, 2, 4) where |r_k - r_{k+1}| == 2 on every phase
        asym_runs = [2, 4, 2, 4, 2, 4, 2, 4]
        seq_c = []
        for idx, length in enumerate(asym_runs):
            seq_c.extend([a if idx % 2 == 0 else b] * length)
        tracker_c = FlickerTracker(n_tiles=1, max_half=64)
        for f, (st, hp) in enumerate(_feed_sequence(tracker_c, [[x] for x in seq_c])):
            self.assertNotEqual(int(st[0]), FLICKER, f"asymmetric C frame {f}")
            self.assertEqual(int(hp[0]), 0, f"asymmetric C frame {f}")


if __name__ == "__main__":
    unittest.main()
