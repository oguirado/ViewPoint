"""Unit scene test suite for eval.v2x.fl_v3 tracker (S1, S2, S3, and Static control).

Tests simultaneous multi-stream vector tracking, independent slot equivalence,
slot permutation invariance, and reset isolation on slot replacement.
"""
import importlib.util
import unittest
import numpy as np

from visioncore.desc import describe_padded
from visioncore.grid import Grid


def _generate_s1_frames(n_frames, height=16, width=16):
    """S1: Translated alternating vertical period-2 stripes (1px left shift per frame)."""
    full_width = width + n_frames
    base = np.zeros((height, full_width, 3), dtype=np.uint8)
    for x in range(full_width):
        base[:, x, :] = (x % 2) * 255
    frames = []
    for f in range(n_frames):
        frames.append(base[:, f : f + width, :].copy())
    return frames


def _generate_s2_frames(n_frames, height=16, width=16):
    """S2: Independent seeded canvas redraws with integer noise and fixed RGB channel offsets (+12, +24 on G)."""
    frames = []
    for f in range(n_frames):
        rng = np.random.default_rng(1000 + f)
        g = rng.integers(0, 221, size=(height, width), dtype=np.uint8)
        frame = np.stack([g, g + 12, g + 24], axis=-1)
        frames.append(frame)
    return frames


def _generate_s3_frames(n_frames, height=16, width=16, run_len=8):
    """S3: Two-state caret with held runs of 8 frames."""
    frame_A = np.zeros((height, width, 3), dtype=np.uint8)
    frame_B = np.zeros((height, width, 3), dtype=np.uint8)
    frame_B[4:6, 4:12, :] = 250
    frames = []
    for f in range(n_frames):
        if (f // run_len) % 2 == 0:
            frames.append(frame_A.copy())
        else:
            frames.append(frame_B.copy())
    return frames


def _generate_static_frames(n_frames, height=16, width=16):
    """Static control: Constant frame across all time steps."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    return [frame.copy() for _ in range(n_frames)]


def _compute_eager_hashes(frames, grid):
    """Compute eager per-tile xh64 content hashes for single-tile frames."""
    hashes = []
    for frame in frames:
        padded = grid.pad(frame)
        out, _ = describe_padded(padded, grid, lazy_xh=False)
        hashes.append(int(out["xh64"][0]))
    return hashes


class TestFLV3UnitScenesS1S2S3(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(16, 16)
        self.n_frames = 48
        self.frames_s1 = _generate_s1_frames(self.n_frames)
        self.frames_s2 = _generate_s2_frames(self.n_frames)
        self.frames_s3 = _generate_s3_frames(self.n_frames)
        self.frames_static = _generate_static_frames(self.n_frames)

        self.hashes_s1 = _compute_eager_hashes(self.frames_s1, self.grid)
        self.hashes_s2 = _compute_eager_hashes(self.frames_s2, self.grid)
        self.hashes_s3 = _compute_eager_hashes(self.frames_s3, self.grid)
        self.hashes_static = _compute_eager_hashes(self.frames_static, self.grid)

    def test_fixture_preconditions_and_eager_hashes(self):
        """Assert frame bytes and eager hashes meet their intended preconditions."""
        # 1. Frame shape and dtype checks
        for name, frames in [("S1", self.frames_s1), ("S2", self.frames_s2), ("S3", self.frames_s3), ("Static", self.frames_static)]:
            self.assertEqual(len(frames), self.n_frames, f"{name} count mismatch")
            for f, frame in enumerate(frames):
                self.assertEqual(frame.shape, (16, 16, 3), f"{name} frame {f} shape invalid")
                self.assertEqual(frame.dtype, np.uint8, f"{name} frame {f} dtype invalid")

        # 2. Byte level preconditions
        # S1: Frames vary across time
        self.assertTrue(any(not np.array_equal(self.frames_s1[0], f) for f in self.frames_s1[1:]), "S1 frames should vary")

        # S2: Redraws are all distinct
        s2_bytes = [f.tobytes() for f in self.frames_s2]
        self.assertEqual(len(set(s2_bytes)), self.n_frames, "S2 frames should all be distinct")

        # S3: Alternates every 8 frames between frame_A and frame_B
        self.assertTrue(np.array_equal(self.frames_s3[0], self.frames_s3[7]), "S3 frame 0 and 7 should be identical")
        self.assertTrue(np.array_equal(self.frames_s3[8], self.frames_s3[15]), "S3 frame 8 and 15 should be identical")
        self.assertFalse(np.array_equal(self.frames_s3[0], self.frames_s3[8]), "S3 frame 0 and 8 should differ")

        # Static: All frames identical
        for f in range(1, self.n_frames):
            self.assertTrue(np.array_equal(self.frames_static[0], self.frames_static[f]), f"Static frame {f} differs")

        # 3. Eager hash preconditions
        # S1 hashes vary
        self.assertGreater(len(set(self.hashes_s1)), 1, "S1 hashes should vary")

        # S2 hashes are 100% unique across redraws
        self.assertEqual(len(set(self.hashes_s2)), self.n_frames, "S2 hashes should all be distinct")

        # S3 hashes have exactly 2 distinct state values
        self.assertEqual(len(set(self.hashes_s3)), 2, "S3 hashes should have exactly 2 distinct states")
        self.assertEqual(self.hashes_s3[0], self.hashes_s3[7])
        self.assertEqual(self.hashes_s3[8], self.hashes_s3[15])
        self.assertNotEqual(self.hashes_s3[0], self.hashes_s3[8])

        # Static hash has exactly 1 value across all frames
        self.assertEqual(len(set(self.hashes_static)), 1, "Static hash should be constant")

    def test_fl_v3_simultaneous_multi_stream_vector_tracking(self):
        """Require no FLICKER from S1, S2 or Static control; S3 declares at frame 24 (hp 8) emitting only on departures."""
        spec = importlib.util.find_spec("eval.v2x.fl_v3")
        self.assertIsNotNone(spec, "eval.v2x.fl_v3 module must exist")
        import eval.v2x.fl_v3 as fl_v3

        c_flicker = getattr(fl_v3, "C_FLICKER", getattr(fl_v3.FlickerTracker, "FLICKER", 4))
        ft = fl_v3.FlickerTracker(n_tiles=4)

        emissions = []
        half_periods = []
        for f in range(40):
            vec = [self.hashes_s1[f], self.hashes_s2[f], self.hashes_s3[f], self.hashes_static[f]]
            st, hp = ft.update(vec)
            emissions.append([int(s) for s in st])
            half_periods.append([int(h) for h in hp])

        # Require zero FLICKER from slot 0 (S1), slot 1 (S2), or slot 3 (Static)
        for f in range(40):
            self.assertNotEqual(emissions[f][0], c_flicker, f"S1 emitted FLICKER at frame {f}")
            self.assertNotEqual(emissions[f][1], c_flicker, f"S2 emitted FLICKER at frame {f}")
            self.assertNotEqual(emissions[f][3], c_flicker, f"Static control emitted FLICKER at frame {f}")

        # Slot 2 (S3) first declares FLICKER at frame 24 with half-period 8
        self.assertEqual(emissions[24][2], c_flicker, "S3 did not emit FLICKER at frame 24")
        self.assertEqual(half_periods[24][2], 8, "S3 half-period at frame 24 is not 8")

        # Slot 2 (S3) emits FLICKER only on departures (toggle frames: 24, 32)
        for f in range(40):
            if emissions[f][2] == c_flicker:
                self.assertIn(f, (24, 32), f"S3 emitted FLICKER on non-departure frame {f}")

    def test_fl_v3_independent_tracker_slot_equivalence(self):
        """Compare each slot in a multi-tile vector with a fresh independent tracker for that slot."""
        spec = importlib.util.find_spec("eval.v2x.fl_v3")
        self.assertIsNotNone(spec, "eval.v2x.fl_v3 module must exist")
        import eval.v2x.fl_v3 as fl_v3

        multi_ft = fl_v3.FlickerTracker(n_tiles=4)
        indep_fts = [fl_v3.FlickerTracker(n_tiles=1) for _ in range(4)]

        for f in range(40):
            vec = [self.hashes_s1[f], self.hashes_s2[f], self.hashes_s3[f], self.hashes_static[f]]
            st_m, hp_m = multi_ft.update(vec)

            for i in range(4):
                st_i, hp_i = indep_fts[i].update([vec[i]])
                self.assertEqual(int(st_m[i]), int(st_i[0]), f"Frame {f} slot {i} state mismatch")
                self.assertEqual(int(hp_m[i]), int(hp_i[0]), f"Frame {f} slot {i} half-period mismatch")

    def test_fl_v3_slot_permutation_invariance(self):
        """Verify that permuting input vector slots merely permutes output states and half-periods."""
        spec = importlib.util.find_spec("eval.v2x.fl_v3")
        self.assertIsNotNone(spec, "eval.v2x.fl_v3 module must exist")
        import eval.v2x.fl_v3 as fl_v3

        perm = [2, 0, 3, 1]
        tracker_orig = fl_v3.FlickerTracker(n_tiles=4)
        tracker_perm = fl_v3.FlickerTracker(n_tiles=4)

        for f in range(40):
            vec_orig = [self.hashes_s1[f], self.hashes_s2[f], self.hashes_s3[f], self.hashes_static[f]]
            vec_perm = [vec_orig[p] for p in perm]

            st_orig, hp_orig = tracker_orig.update(vec_orig)
            st_perm, hp_perm = tracker_perm.update(vec_perm)

            for idx, p in enumerate(perm):
                self.assertEqual(int(st_perm[idx]), int(st_orig[p]), f"Frame {f} permuted slot {idx} state mismatch")
                self.assertEqual(int(hp_perm[idx]), int(hp_orig[p]), f"Frame {f} permuted slot {idx} hp mismatch")

    def test_fl_v3_reset_isolation_on_slot_replacement(self):
        """Extend beyond declaration, replace one slot with a third hash, and prove S3 caret continues without resetting."""
        spec = importlib.util.find_spec("eval.v2x.fl_v3")
        self.assertIsNotNone(spec, "eval.v2x.fl_v3 module must exist")
        import eval.v2x.fl_v3 as fl_v3

        c_flicker = getattr(fl_v3, "C_FLICKER", getattr(fl_v3.FlickerTracker, "FLICKER", 4))
        ft = fl_v3.FlickerTracker(n_tiles=4)

        # Feed frames 0..31 normally
        for f in range(32):
            vec = [self.hashes_s1[f], self.hashes_s2[f], self.hashes_s3[f], self.hashes_static[f]]
            st, hp = ft.update(vec)
            if f == 24:
                self.assertEqual(int(st[2]), c_flicker)

        # Extend beyond declaration (frames 32..47)
        # At frame 32, replace slot 0 (S1) with a third distinct hash stream to trigger a reset on slot 0
        third_hash_base = 0x9999888877776666
        for f in range(32, 48):
            # Slot 0 gets third_hash_base + f (distinct third state breaking 2-state cycle)
            h_slot0_replaced = third_hash_base + f
            vec = [h_slot0_replaced, self.hashes_s2[f], self.hashes_s3[f], self.hashes_static[f]]
            st, hp = ft.update(vec)

            # Replaced slot 0 must NOT be in FLICKER
            self.assertNotEqual(int(st[0]), c_flicker, f"Replaced slot 0 emitted FLICKER at frame {f}")

            # S3 caret (slot 2) must continue without resetting:
            # On departures (toggle frames 32, 40), slot 2 must continue to emit FLICKER (hp 8)
            if f in (32, 40):
                self.assertEqual(int(st[2]), c_flicker, f"S3 caret in slot 2 failed to emit FLICKER at frame {f}")
                self.assertEqual(int(hp[2]), 8, f"S3 caret in slot 2 half-period modified at frame {f}")


if __name__ == "__main__":
    unittest.main()
