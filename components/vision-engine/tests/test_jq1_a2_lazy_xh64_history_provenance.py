# @module {"id":"tests.test_jq1_a2_lazy_xh64_history_provenance","layer":5,"stage":"L5","owner":"CORE","deps":["vc.desc","vc.change","vc.grid","vc.p"]}
"""Tests for DEF-A2-04: Lazy S1 hashes stored in S2 history before post-S2 hashing."""
import unittest

import numpy as np

from visioncore import change, desc, grid, params as P


class TestLazyXh64HistoryProvenance(unittest.TestCase):

    def test_eager_vs_lazy_and_history_characterization(self):
        """Ordinary characterization test for eager vs lazy hashing and history array behavior."""
        g = grid.Grid(16, 16)

        # Three 16x16 RGB frames with independently computed xh64_of values.
        # Choose frame 0 and frame 2 with different bytes and hashes.
        f0 = np.full((16, 16, 3), 10, dtype=np.uint8)
        f1 = np.full((16, 16, 3), 80, dtype=np.uint8)
        f2 = np.full((16, 16, 3), 160, dtype=np.uint8)

        h0 = desc.xh64_of(f0)
        h1 = desc.xh64_of(f1)
        h2 = desc.xh64_of(f2)

        self.assertNotEqual(h0, h2)
        self.assertNotEqual(h0, h1)
        self.assertNotEqual(h1, h2)

        # 1. Eager hashing matches xh64_of
        padded0 = g.pad(f0)
        tiles_eager, _ = desc.describe_padded(padded0, g, lazy_xh=False)
        self.assertEqual(tiles_eager["xh64"][0], h0)

        # 2. Lazy descriptors initially carry zero hashes
        tiles_lazy0, _ = desc.describe_padded(padded0, g, lazy_xh=True)
        self.assertEqual(tiles_lazy0["xh64"][0], 0)

        # 3. Reproduce pipeline sequence in RAM:
        # Frame 0: describe_padded (lazy_xh=True) -> classify_first -> out.copy() -> fill_xh64
        y0 = desc.luma_of_padded(padded0)
        state = change.ClassState.init(tiles_lazy0)
        out0, state, _ = change.classify_first(state, tiles_lazy0, y_cur=y0)
        out0_filled = out0.copy()
        desc.fill_xh64(out0_filled, padded0, g, prev_xh64=None, hash_all=True)

        self.assertEqual(out0_filled["xh64"][0], h0)
        # Filling an output copy does not populate previously rotated ClassState arrays
        self.assertEqual(state.prev1["xh64"][0], 0)
        self.assertEqual(state.prev2["xh64"][0], 0)

        # Frame 1: describe_padded (lazy_xh=True) -> classify_tiles -> out.copy() -> fill_xh64
        prev_padded = padded0
        prev_xh = out0_filled["xh64"]
        padded1 = g.pad(f1)
        y1 = desc.luma_of_padded(padded1)
        tiles_lazy1, words_eq1 = desc.describe_padded(
            padded1, g, prev_padded, prev_xh, P.PARAMS, prev_tiles=tiles_lazy0, y_plane=y1, lazy_xh=True
        )
        out1, state, _ = change.classify_tiles(state, tiles_lazy1, words_eq1, y_cur=y1, grid=(16, 1, 1), params=P.PARAMS)
        out1_filled = out1.copy()
        desc.fill_xh64(out1_filled, padded1, g, prev_xh64=prev_xh, hash_all=False)

        self.assertEqual(out1_filled["xh64"][0], h1)
        # Verify history array hashes remain 0 after rotation and filling output copy
        self.assertEqual(state.prev1["xh64"][0], 0)
        self.assertEqual(state.prev2["xh64"][0], 0)

        # Frame 2: describe_padded (lazy_xh=True) -> classify_tiles -> out.copy() -> fill_xh64
        prev_padded = padded1
        prev_xh = out1_filled["xh64"]
        padded2 = g.pad(f2)
        y2 = desc.luma_of_padded(padded2)
        tiles_lazy2, words_eq2 = desc.describe_padded(
            padded2, g, prev_padded, prev_xh, P.PARAMS, prev_tiles=tiles_lazy1, y_plane=y2, lazy_xh=True
        )
        out2, state, _ = change.classify_tiles(state, tiles_lazy2, words_eq2, y_cur=y2, grid=(16, 1, 1), params=P.PARAMS)
        out2_filled = out2.copy()
        desc.fill_xh64(out2_filled, padded2, g, prev_xh64=prev_xh, hash_all=False)

        self.assertEqual(out2_filled["xh64"][0], h2)
        # Assert that filling output copy does not populate history arrays
        self.assertEqual(state.prev1["xh64"][0], 0)
        self.assertEqual(state.prev2["xh64"][0], 0)

    def test_jitter_fill_xh64_copies_prior_hash(self):
        """Independently build a two-frame case changing one pixel by one LSB, verify JITTER, and pin fill_xh64 behavior."""
        g = grid.Grid(16, 16)

        fj0 = np.full((16, 16, 3), 100, dtype=np.uint8)
        fj1 = fj0.copy()
        fj1[0, 0, 0] = 101  # Change 1 pixel by 1 LSB

        hj0 = desc.xh64_of(fj0)
        hj1 = desc.xh64_of(fj1)
        self.assertNotEqual(hj0, hj1)

        # Frame 0
        pj0 = g.pad(fj0)
        yj0 = desc.luma_of_padded(pj0)
        tj0, words_eq_j0 = desc.describe_padded(pj0, g, None, None, P.PARAMS, lazy_xh=True)
        st_j = change.ClassState.init(tj0)
        out_j0, st_j, _ = change.classify_first(st_j, tj0, y_cur=yj0)
        out_j0_filled = out_j0.copy()
        desc.fill_xh64(out_j0_filled, pj0, g, prev_xh64=None, hash_all=True)

        prev_pj = pj0
        prev_xh_j = out_j0_filled["xh64"]
        self.assertEqual(prev_xh_j[0], hj0)

        # Frame 1
        pj1 = g.pad(fj1)
        yj1 = desc.luma_of_padded(pj1)
        tj1, words_eq_j1 = desc.describe_padded(pj1, g, prev_pj, prev_xh_j, P.PARAMS, prev_tiles=tj0, y_plane=yj1, lazy_xh=True)
        out_j1, st_j, _ = change.classify_tiles(st_j, tj1, words_eq_j1, y_cur=yj1, grid=(16, 1, 1), params=P.PARAMS)

        # Verify the resulting tile is classified as JITTER
        self.assertEqual(out_j1["cls"][0], change.C_JITTER)

        # Pin that fill_xh64 copies the prior hash for JITTER despite a different true current digest
        out_j1_filled = out_j1.copy()
        desc.fill_xh64(out_j1_filled, pj1, g, prev_xh64=prev_xh_j, hash_all=False)
        self.assertEqual(out_j1_filled["xh64"][0], hj0)
        self.assertNotEqual(out_j1_filled["xh64"][0], hj1)

    @unittest.expectedFailure
    def test_def_a2_04_lazy_xh64_history_provenance_contract(self):
        # DEF-A2-04
        """Assert the desired provenance contract: state.prev1 and state.prev2 hold true frame-specific digests."""
        g = grid.Grid(16, 16)

        f0 = np.full((16, 16, 3), 10, dtype=np.uint8)
        f1 = np.full((16, 16, 3), 80, dtype=np.uint8)
        f2 = np.full((16, 16, 3), 160, dtype=np.uint8)

        h0 = desc.xh64_of(f0)
        h1 = desc.xh64_of(f1)
        h2 = desc.xh64_of(f2)

        # Frame 0
        padded0 = g.pad(f0)
        y0 = desc.luma_of_padded(padded0)
        tiles0, words_eq0 = desc.describe_padded(padded0, g, None, None, P.PARAMS, lazy_xh=True)
        state = change.ClassState.init(tiles0)
        out0, state, _ = change.classify_first(state, tiles0, y_cur=y0)
        out0_filled = out0.copy()
        desc.fill_xh64(out0_filled, padded0, g, prev_xh64=None, hash_all=True)

        # Contract assertion after Frame 0: history represents frame 0 digest
        self.assertEqual(state.prev1["xh64"][0], h0)
        self.assertEqual(state.prev2["xh64"][0], h0)

        # Frame 1
        prev_padded = padded0
        prev_xh = out0_filled["xh64"]
        padded1 = g.pad(f1)
        y1 = desc.luma_of_padded(padded1)
        tiles1, words_eq1 = desc.describe_padded(padded1, g, prev_padded, prev_xh, P.PARAMS, prev_tiles=tiles0, y_plane=y1, lazy_xh=True)
        out1, state, _ = change.classify_tiles(state, tiles1, words_eq1, y_cur=y1, grid=(16, 1, 1), params=P.PARAMS)
        out1_filled = out1.copy()
        desc.fill_xh64(out1_filled, padded1, g, prev_xh64=prev_xh, hash_all=False)

        # Contract assertion after Frame 1: prev1 represents f1 digest, prev2 represents f0 digest
        self.assertEqual(state.prev1["xh64"][0], h1)
        self.assertEqual(state.prev2["xh64"][0], h0)

        # Frame 2
        prev_padded = padded1
        prev_xh = out1_filled["xh64"]
        padded2 = g.pad(f2)
        y2 = desc.luma_of_padded(padded2)
        tiles2, words_eq2 = desc.describe_padded(padded2, g, prev_padded, prev_xh, P.PARAMS, prev_tiles=tiles1, y_plane=y2, lazy_xh=True)
        out2, state, _ = change.classify_tiles(state, tiles2, words_eq2, y_cur=y2, grid=(16, 1, 1), params=P.PARAMS)
        out2_filled = out2.copy()
        desc.fill_xh64(out2_filled, padded2, g, prev_xh64=prev_xh, hash_all=False)

        # Contract assertion after Frame 2: prev1 represents f2 digest, prev2 represents f1 digest
        self.assertEqual(state.prev1["xh64"][0], h2)
        self.assertEqual(state.prev2["xh64"][0], h1)


if __name__ == "__main__":
    unittest.main()
