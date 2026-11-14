"""tests/test_jq1_a2_s2_mexp_and_withdrawal.py - S2 exposed-window mexp and failed publication rollback contract tests.

Pins the S2 published exposed-window mexp branch and failed-publication rollback contract:
- 64x16 grid with tile size 16 (4 tiles).
- Exposed tile CHANGED with zero motion, no FLAG_GLOBAL_MV, low motion-history bit set.
- Full-window explained tiles MOVED with FLAG_GLOBAL_MV.
- Controls with less than half overlap and strict improvement equality boundary.
- Failed publication rollback and withdrawal with island rescue explicitly prevented.
"""
import unittest
from unittest.mock import patch
import numpy as np

from visioncore import change as C, layout, params as P
from visioncore.grid import Grid


def create_tile_array(grid, y_q4=1000):
    t = np.zeros(grid.N, dtype=C.TILE_DT)
    t["morton"] = grid.mortons.copy()
    t["y_q4"] = y_q4
    t["sig_q4"] = 100
    return t


class TestS2MexpAndWithdrawal(unittest.TestCase):
    def setUp(self):
        self.grid = Grid(64, 16)
        self.s = self.grid.s  # 16
        self.gw = self.grid.gw  # 4
        self.gh = self.grid.gh  # 1
        self.N = self.grid.N  # 4
        self.grid_tuple = (self.s, self.gw, self.gh)

    def test_published_exposed_window_mexp(self):
        """Published global vector (8,0): 3 interior tiles MOVED, 1 left-edge tile exposed mexp CHANGED."""
        # Frame 0 (t-2), Frame 1 (t-1), Frame 2 (t)
        # Create luma planes:
        # Prev frame y_prev (t-1): 16x64 array
        # Cur frame y_cur (t): shifted by +8 px in x relative to y_prev
        y_prev = np.zeros((16, 64), dtype=np.int16)
        # Add high contrast pattern so unshifted SAD is positive
        for x in range(64):
            y_prev[:, x] = (x * 10) % 200

        y_cur = np.zeros((16, 64), dtype=np.int16)
        # Shift y_prev by +8 px horizontally: y_cur[:, x] = y_prev[:, x-8] for x >= 8
        y_cur[:, 8:] = y_prev[:, :56]
        # For tx=0, x=0..7 in y_cur, fill with pattern so unshifted SAD s0c > 0
        y_cur[:, :8] = 150

        # Create tile descriptor arrays for frames
        t0 = create_tile_array(self.grid, y_q4=1000)
        t1 = create_tile_array(self.grid, y_q4=1100)
        t2 = create_tile_array(self.grid, y_q4=1200)

        # Make tx=0 provisional flicker at frame t=2:
        # r1 > t_enter, r2 <= t_exit
        # For tx=0: t0["y_q4"]=1000, t1["y_q4"]=1100, t2["y_q4"]=1000
        t2["y_q4"][0] = 1000

        # Initialize ClassState
        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=y_prev)

        # Step t=1
        weq1 = np.zeros(self.N, bool)
        C.classify_tiles(st, t1, weq1, y_cur=y_prev, grid=self.grid_tuple)

        # Step t=2
        weq2 = np.zeros(self.N, bool)
        out, st, summary = C.classify_tiles(st, t2, weq2, y_cur=y_cur, grid=self.grid_tuple)

        # Verify global MV published
        self.assertEqual(summary["global_mv"], [8, 0])

        # Verify classifications and flags
        # tx=0 (exposed left edge tile): CHANGED (2), mv=(0,0), no FLAG_GLOBAL_MV, bit 0 in mvh16 set
        self.assertEqual(out["cls"][0], C.C_CHANGED)
        self.assertEqual(out["mv_dx"][0], 0)
        self.assertEqual(out["mv_dy"][0], 0)
        self.assertEqual(out["flags"][0] & C.FLAG_GLOBAL_MV, 0)
        self.assertEqual(st.mvh16[0] & 1, 1)

        # tx=1, 2, 3 (full interior tiles): MOVED (3), mv=(8,0), FLAG_GLOBAL_MV set
        for tx in (1, 2, 3):
            self.assertEqual(out["cls"][tx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][tx], 8)
            self.assertEqual(out["mv_dy"][tx], 0)
            self.assertNotEqual(out["flags"][tx] & C.FLAG_GLOBAL_MV, 0)

    def test_mexp_control_under_half_overlap(self):
        """Control case: overlap count less than half (2 * nvc < nn) fails mexp."""
        y_prev = np.zeros((16, 64), dtype=np.int16)
        for x in range(64):
            y_prev[:, x] = (x * 10) % 200

        y_cur = np.zeros((16, 64), dtype=np.int16)
        y_cur[:, 8:] = y_prev[:, :56]
        y_cur[:, :8] = 150

        t0 = create_tile_array(self.grid, y_q4=1000)
        t1 = create_tile_array(self.grid, y_q4=1100)
        t2 = create_tile_array(self.grid, y_q4=1200)
        t2["y_q4"][0] = 1000

        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=y_prev)
        C.classify_tiles(st, t1, np.zeros(self.N, bool), y_cur=y_prev, grid=self.grid_tuple)

        # Mock sad_for to return nvc = 112 (< 128, so 2 * 112 = 224 < 256) for tile 0
        real_sad_clip = C._tile_sad_clip

        def mock_sad_clip(y_c, y_p, c, s, gh, gw, tx, ty):
            sad, count = real_sad_clip(y_c, y_p, c, s, gh, gw, tx, ty)
            if c == (8, 0):
                sad = sad.copy()
                count = count.copy()
                count[0] = 112  # Less than half (128)
            return sad, count

        with patch("visioncore.change._tile_sad_clip", side_effect=mock_sad_clip):
            out, st, summary = C.classify_tiles(st, t2, np.zeros(self.N, bool), y_cur=y_cur, grid=self.grid_tuple)

        # mexp fails -> tx=0 remains FLICKER
        self.assertEqual(summary["global_mv"], [8, 0])
        self.assertEqual(out["cls"][0], C.C_FLICKER)
        self.assertEqual(out["mv_dx"][0], 0)
        self.assertEqual(out["mv_dy"][0], 0)
        self.assertEqual(out["flags"][0] & C.FLAG_GLOBAL_MV, 0)
        self.assertEqual(st.mvh16[0] & 1, 0)

    def test_mexp_control_strict_improvement_boundary(self):
        """Control case: equality at strict improvement boundary (den * sgc * nn == s0c * nvc) fails mexp."""
        y_prev = np.zeros((16, 64), dtype=np.int16)
        for x in range(64):
            y_prev[:, x] = (x * 10) % 200

        y_cur = np.zeros((16, 64), dtype=np.int16)
        y_cur[:, 8:] = y_prev[:, :56]
        y_cur[:, :8] = 150

        t0 = create_tile_array(self.grid, y_q4=1000)
        t1 = create_tile_array(self.grid, y_q4=1100)
        t2 = create_tile_array(self.grid, y_q4=1200)
        t2["y_q4"][0] = 1000

        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=y_prev)
        C.classify_tiles(st, t1, np.zeros(self.N, bool), y_cur=y_prev, grid=self.grid_tuple)

        # Mock sad_for so for tile 0: sgc = 1, s0c = 6. With den=3, nn=256, nvc=128:
        # den * sgc * nn = 3 * 1 * 256 = 768
        # s0c * nvc = 6 * 128 = 768
        # den * sgc * nn == s0c * nvc (768 == 768), strict inequality < fails!
        real_sad_clip = C._tile_sad_clip

        def mock_sad_clip(y_c, y_p, c, s, gh, gw, tx, ty):
            sad, count = real_sad_clip(y_c, y_p, c, s, gh, gw, tx, ty)
            sad = sad.copy()
            count = count.copy()
            if c == (8, 0):
                sad[0] = 1  # sgc = 1
            elif c == (0, 0):
                sad[0] = 6  # s0c = 6
            return sad, count

        with patch("visioncore.change._tile_sad_clip", side_effect=mock_sad_clip):
            out, st, summary = C.classify_tiles(st, t2, np.zeros(self.N, bool), y_cur=y_cur, grid=self.grid_tuple)

        # mexp fails -> tx=0 remains FLICKER
        self.assertEqual(summary["global_mv"], [8, 0])
        self.assertEqual(out["cls"][0], C.C_FLICKER)
        self.assertEqual(out["mv_dx"][0], 0)
        self.assertEqual(out["mv_dy"][0], 0)
        self.assertEqual(out["flags"][0] & C.FLAG_GLOBAL_MV, 0)
        self.assertEqual(st.mvh16[0] & 1, 0)

    def test_failed_publication_rollback_and_withdrawal(self):
        """Failed publication rollback: only 1 full tile supports candidate MV (< 50% GLOBAL_SHARE_PPM).
        Tentative offers are withdrawn and island rescue explicitly prevented."""
        y_prev = np.zeros((16, 64), dtype=np.int16)

        # Create y_cur such that only tile 1 (tx=1) supports vector (8, 0)
        y_cur = np.zeros((16, 64), dtype=np.int16)
        # Tile 1: x=16..31 matches y_prev shifted by 8
        y_cur[:, 16:32] = y_prev[:, 8:24]
        # Tiles 0, 2, 3: mismatch for vector (8,0)
        y_cur[:, 0:16] = 500
        y_cur[:, 32:64] = 800

        t0 = create_tile_array(self.grid, y_q4=1000)
        t1 = create_tile_array(self.grid, y_q4=1100)
        t2 = create_tile_array(self.grid, y_q4=1200)

        st = C.ClassState.init(t0)
        C.classify_first(st, t0, y_cur=y_prev)
        C.classify_tiles(st, t1, np.zeros(self.N, bool), y_cur=y_prev, grid=self.grid_tuple)

        # Mock global_vector to propose (8, 0)
        # Explicitly prevent island rescue by mocking island_vector to return (0, 0)
        with patch("visioncore.change.global_vector", return_value=(8, 0)), \
             patch("visioncore.change.island_vector", return_value=(0, 0)):
            out, st, summary = C.classify_tiles(st, t2, np.zeros(self.N, bool), y_cur=y_cur, grid=self.grid_tuple)

        # Verify global_mv is None
        self.assertIsNone(summary["global_mv"])

        # Verify no MOVED classifications
        self.assertFalse((out["cls"] == C.C_MOVED).any())

        # Verify all tiles are CHANGED
        self.assertTrue((out["cls"] == C.C_CHANGED).all())

        # Verify zero vectors for all tiles
        self.assertTrue((out["mv_dx"] == 0).all())
        self.assertTrue((out["mv_dy"] == 0).all())

        # Verify no global flags set
        self.assertTrue((out["flags"] & C.FLAG_GLOBAL_MV == 0).all())


if __name__ == "__main__":
    unittest.main()
