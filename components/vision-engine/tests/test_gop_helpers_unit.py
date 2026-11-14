"""Unit tests for private S5 GOP builder helpers in visioncore/gop.py."""
import random
import struct
import unittest
import numpy as np

from visioncore import gop, ids, layout
from visioncore.desc import TILE_DTYPE
from visioncore.errors import VqaError


class TestRecBytes(unittest.TestCase):
    def test_rec_bytes_valid(self):
        n = 16
        tiles = np.zeros(n, dtype=TILE_DTYPE)
        res = gop._rec_bytes(tiles, n)
        self.assertEqual(res.shape, (n, layout.TILE_SIZE))
        self.assertEqual(res.dtype, np.uint8)

    def test_rec_bytes_negative_shape_mismatch(self):
        n = 16
        tiles = np.zeros(n + 1, dtype=TILE_DTYPE)
        with self.assertRaises(VqaError) as ctx:
            gop._rec_bytes(tiles, n)
        self.assertEqual(ctx.exception.code, "E_VC_FORMAT")
        self.assertEqual(ctx.exception.rc, 2)

    def test_rec_bytes_negative_dtype_mismatch(self):
        n = 16
        tiles = np.zeros((n, 8), dtype=np.uint8)  # Itemsize 1 * 8 != 32
        with self.assertRaises(VqaError) as ctx:
            gop._rec_bytes(tiles, n)
        self.assertEqual(ctx.exception.code, "E_VC_FORMAT")


class TestMaskWords(unittest.TestCase):
    def test_mask_words_exact_bit_order(self):
        gw, gh = 16, 10
        m, _, _, starts, _ = gop._cells(gw, gh)
        n = len(m)
        dirty = np.zeros(n, dtype=bool)

        # Index 0 is in macroblock 0 (m[0] >> 6 == 0)
        dirty[0] = True

        # Find an index in macroblock 1 (m[idx] >> 6 == 1)
        mb1_indices = np.where((m >> 6) == 1)[0]
        idx_mb1 = int(mb1_indices[0])
        dirty[idx_mb1] = True

        words = gop._mask_words(dirty, gw, gh)
        self.assertEqual(words.dtype, np.dtype("<u8"))
        self.assertEqual(len(words), len(starts))

        # Check exact bit order in macroblock 0 word
        expected_word0 = np.uint64(1) << np.uint64(m[0] & 63)
        self.assertEqual(words[0], expected_word0)

        # Check macroblock 1 word
        expected_word1 = np.uint64(1) << np.uint64(m[idx_mb1] & 63)
        self.assertEqual(words[1], expected_word1)

        # Other macroblock words should be zero
        for w in words[2:]:
            self.assertEqual(w, 0)

    def test_mask_words_negative_size_mismatch(self):
        gw, gh = 16, 10
        m, _, _, _, _ = gop._cells(gw, gh)
        dirty = np.zeros(len(m) - 5, dtype=bool)
        with self.assertRaises(ValueError):
            gop._mask_words(dirty, gw, gh)


class TestEncodePframe(unittest.TestCase):
    def test_encode_pframe_zero_and_all_dirty(self):
        gw, gh = 16, 10  # 16x10 cells -> gw=16, gh=10
        m, _, _, starts, _ = gop._cells(gw, gh)
        N = len(m)
        n_mb = len(starts)

        t_idx = 101
        gm = (2, -3, 500000)

        rec = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)
        rec[:, 0] = np.arange(N, dtype=np.uint8)

        # Case 1: Zero dirty tiles
        dirty_zero = np.zeros(N, dtype=bool)
        ref_zero = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)
        ref_zero_orig = ref_zero.copy()

        encoded_zero = gop._encode_pframe(t_idx, rec, ref_zero, dirty_zero, gm, gw, gh)

        # Ref must not change when no tiles are dirty
        np.testing.assert_array_equal(ref_zero, ref_zero_orig)

        # Expected output length for zero dirty tiles:
        # PHDR_SIZE (24) + 8 * n_mb (mask words) + rle_encode(b'') (0 bytes)
        expected_zero_len = gop.PHDR_SIZE + 8 * n_mb + 0
        self.assertEqual(len(encoded_zero), expected_zero_len)

        # Parse PHdr from encoded_zero
        phdr_zero = struct.unpack_from(gop.PHDR_FMT, encoded_zero, 0)
        self.assertEqual(phdr_zero[0], t_idx)  # t
        self.assertEqual(phdr_zero[1], 0)      # n_dirty
        self.assertEqual(phdr_zero[3], 2)      # gdx
        self.assertEqual(phdr_zero[4], -3)     # gdy
        self.assertEqual(phdr_zero[5], 500000) # share
        self.assertEqual(phdr_zero[6], 0)      # dlen = 0 for empty delta

        # Case 2: All dirty tiles
        dirty_all = np.ones(N, dtype=bool)
        ref_all = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)

        encoded_all = gop._encode_pframe(t_idx, rec, ref_all, dirty_all, gm, gw, gh)

        # Ref must be committed in place to rec for all dirty tiles
        np.testing.assert_array_equal(ref_all, rec)

        phdr_all = struct.unpack_from(gop.PHDR_FMT, encoded_all, 0)
        self.assertEqual(phdr_all[1], N)       # n_dirty = N

        # Delta size check
        dlen_all = phdr_all[6]
        expected_all_len = gop.PHDR_SIZE + 8 * n_mb + dlen_all
        self.assertEqual(len(encoded_all), expected_all_len)

    def test_encode_pframe_partial_dirty_commits_in_place(self):
        gw, gh = 16, 10
        m, _, _, _, _ = gop._cells(gw, gh)
        N = len(m)
        rec = np.ones((N, layout.TILE_SIZE), dtype=np.uint8) * 42
        ref = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)

        dirty = np.zeros(N, dtype=bool)
        dirty[5] = True
        dirty[12] = True

        gop._encode_pframe(1, rec, ref, dirty, (0, 0, 0), gw, gh)

        # Verify only dirty index 5 and 12 were committed in place
        self.assertTrue((ref[5] == 42).all())
        self.assertTrue((ref[12] == 42).all())
        self.assertTrue((ref[0] == 0).all())
        self.assertTrue((ref[1] == 0).all())

    def test_encode_pframe_negative_shape_mismatch(self):
        gw, gh = 16, 10
        m, _, _, _, _ = gop._cells(gw, gh)
        N = len(m)
        rec = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)
        ref = np.zeros((N, layout.TILE_SIZE), dtype=np.uint8)
        dirty_bad = np.zeros(N + 50, dtype=bool)
        dirty_bad[N + 10] = True
        with self.assertRaises(IndexError):
            gop._encode_pframe(1, rec, ref, dirty_bad, (0, 0, 0), gw, gh)


class TestClip16(unittest.TestCase):
    def test_clip16_bounds(self):
        self.assertEqual(gop._clip16(0), 0)
        self.assertEqual(gop._clip16(1234), 1234)
        self.assertEqual(gop._clip16(-5678), -5678)
        self.assertEqual(gop._clip16(32767), 32767)
        self.assertEqual(gop._clip16(-32768), -32768)
        # Saturation checks
        self.assertEqual(gop._clip16(32768), 32767)
        self.assertEqual(gop._clip16(100000), 32767)
        self.assertEqual(gop._clip16(-32769), -32768)
        self.assertEqual(gop._clip16(-100000), -32768)

    def test_clip16_negative_invalid_type(self):
        with self.assertRaises((TypeError, ValueError)):
            gop._clip16("not_a_number")


class TestGlobalTriplet(unittest.TestCase):
    def test_global_triplet_zero_pos_neg_motion(self):
        tiles = np.zeros(10, dtype=TILE_DTYPE)
        # Set non-zero cls on 4 tiles, 2 of which have flag 64 (global motion)
        tiles["cls"][:4] = 2
        tiles["flags"][0] = 64
        tiles["flags"][1] = 64
        # gshare ppm = 2 * 1000000 // 4 = 500000

        # Zero motion
        gdx, gdy, share = gop._global_triplet(None, tiles)
        self.assertEqual((gdx, gdy, share), (0, 0, 500000))

        gdx, gdy, share = gop._global_triplet((0, 0), tiles)
        self.assertEqual((gdx, gdy, share), (0, 0, 500000))

        # Positive motion
        gdx, gdy, share = gop._global_triplet((120, 340), tiles)
        self.assertEqual((gdx, gdy, share), (120, 340, 500000))

        # Negative motion
        gdx, gdy, share = gop._global_triplet((-45, -89), tiles)
        self.assertEqual((gdx, gdy, share), (-45, -89, 500000))

        # Saturation clipping
        gdx, gdy, share = gop._global_triplet((50000, -50000), tiles)
        self.assertEqual((gdx, gdy, share), (32767, -32768, 500000))

    def test_global_triplet_negative_invalid_gmv(self):
        tiles = np.zeros(10, dtype=TILE_DTYPE)
        with self.assertRaises((IndexError, TypeError)):
            gop._global_triplet((10,), tiles)


class TestEvents(unittest.TestCase):
    def test_events_cut_and_flash(self):
        ev = [(10, 5, "EXISTING", "id_a")]
        frames = [10, 11, 12]
        a = 0

        # Neither cut nor flash
        pl_none = {"cut": False, "flash": False}
        res_none = gop._events(ev, pl_none, frames, a)
        self.assertEqual(res_none, [["EXISTING", "id_a"]])

        # Cut only
        pl_cut = {"cut": True, "flash": False}
        res_cut = gop._events(ev, pl_cut, frames, a)
        expected_cut_id = ids.global_id(10)
        self.assertEqual(res_cut, [["CUT", expected_cut_id], ["EXISTING", "id_a"]])

        # Both cut and flash
        pl_both = {"cut": True, "flash": True}
        res_both = gop._events(ev, pl_both, frames, a)
        # CUT (rank 0) and FLASH (rank 0) sort before EXISTING (rank 5)
        self.assertEqual(res_both, [["CUT", expected_cut_id], ["FLASH", expected_cut_id], ["EXISTING", "id_a"]])

    def test_events_negative_out_of_bounds(self):
        ev = []
        frames = [10]
        pl = {"cut": True, "flash": False}
        with self.assertRaises(IndexError):
            gop._events(ev, pl, frames, a=5)


class TestRegionRanks(unittest.TestCase):
    def test_region_ranks_spans_and_bbox(self):
        gw, gh = 16, 10
        m, tx, ty, _, _ = gop._cells(gw, gh)
        grid_mock = type("GridMock", (), {"gw": gw, "gh": gh})()

        # Spans mode
        reg_spans = {"spans": [["0000", "0002"], ["0005", "0005"]]}
        ranks_spans = gop._region_ranks(reg_spans, grid_mock)
        np.testing.assert_array_equal(ranks_spans, [0, 1, 2, 5])

        # Bbox mode
        reg_bbox = {"bbox_t": [0, 0, 1, 1]}  # tx in [0, 1], ty in [0, 1]
        ranks_bbox = gop._region_ranks(reg_bbox, grid_mock)
        self.assertTrue(((tx[ranks_bbox] <= 1) & (ty[ranks_bbox] <= 1)).all())

    def test_region_ranks_permutation_invariance_seeds_0_to_4(self):
        gw, gh = 16, 10
        grid_mock = type("GridMock", (), {"gw": gw, "gh": gh})()
        base_spans = [["0000", "0004"], ["000a", "000f"], ["0020", "0025"]]

        canonical_ranks = np.unique(gop._region_ranks({"spans": base_spans}, grid_mock))

        for seed in range(5):
            rng = random.Random(seed)
            shuffled_spans = list(base_spans)
            rng.shuffle(shuffled_spans)
            ranks = np.unique(gop._region_ranks({"spans": shuffled_spans}, grid_mock))
            np.testing.assert_array_equal(ranks, canonical_ranks)

    def test_region_ranks_negative_missing_keys(self):
        grid_mock = type("GridMock", (), {"gw": 16, "gh": 10})()
        with self.assertRaises(KeyError):
            gop._region_ranks({}, grid_mock)


class TestIdStrings(unittest.TestCase):
    def test_id_strings_variants(self):
        self.assertEqual(gop._id_strings("single_id"), ["single_id"])
        self.assertEqual(gop._id_strings(["id1", 123, "id2", None]), ["id1", "id2"])
        self.assertEqual(gop._id_strings(("id_x", "id_y")), ["id_x", "id_y"])
        self.assertEqual(gop._id_strings(12345), [])
        self.assertEqual(gop._id_strings(None), [])

    def test_id_strings_negative_custom_non_sequence(self):
        class NonSeq:
            pass
        self.assertEqual(gop._id_strings(NonSeq()), [])


class TestTrackSort(unittest.TestCase):
    def test_track_sort_key_parsing(self):
        key = "k10:000a"
        self.assertEqual(gop._track_sort(key), (10, 10))

    def test_track_sort_permutation_invariance_seeds_0_to_4(self):
        keys = ["k10:000a", "k2:0001", "k10:0002", "k1:000f", "k5:0100", "k2:0000"]
        canonical_sorted = sorted(keys, key=gop._track_sort)

        for seed in range(5):
            rng = random.Random(seed)
            shuffled_keys = list(keys)
            rng.shuffle(shuffled_keys)
            res = sorted(shuffled_keys, key=gop._track_sort)
            self.assertEqual(res, canonical_sorted)

    def test_track_sort_negative_malformed_key(self):
        with self.assertRaises((ValueError, IndexError)):
            gop._track_sort("invalid_key")


class TestAttachCycles(unittest.TestCase):
    def test_attach_cycles_matching_and_non_matching(self):
        grid_mock = type("GridMock", (), {"gw": 16, "gh": 10})()
        track1 = {"all_ranks": np.array([2, 5, 8]), "flags": set()}
        track2 = {"all_ranks": np.array([10, 12]), "flags": set()}
        tracks = [track1, track2]

        cycles = [
            {"rank": 5, "p": 4},
            {"rank": 2, "p": 2},
            {"rank": 99, "p": 3},  # Does not match any track
        ]

        gop._attach_cycles(tracks, cycles, grid_mock)

        # track1 intersects cycles with ranks 5 (p=4) and 2 (p=2) -> min period = 2
        self.assertEqual(track1["period"], 2)
        self.assertIn("CYCLE", track1["flags"])
        self.assertIn("FLICKER", track1["flags"])

        # track2 intersects no cycles -> period remains 0, flags untouched
        self.assertEqual(track2["period"], 0)
        self.assertNotIn("CYCLE", track2["flags"])
        self.assertNotIn("FLICKER", track2["flags"])

    def test_attach_cycles_negative_missing_rank_in_track(self):
        grid_mock = type("GridMock", (), {"gw": 16, "gh": 10})()
        track_bad = {"flags": set()}  # Missing "all_ranks"
        cycles = [{"rank": 1, "p": 2}]
        with self.assertRaises(KeyError):
            gop._attach_cycles([track_bad], cycles, grid_mock)


if __name__ == "__main__":
    unittest.main()
