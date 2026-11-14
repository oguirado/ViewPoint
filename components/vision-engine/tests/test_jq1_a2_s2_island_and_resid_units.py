# tests/test_jq1_a2_s2_island_and_resid_units.py
"""Unit tests pinning local-island motion rescue and q4 vs display-residual contract (jq1-a2-s2)."""

import unittest
import numpy as np

from visioncore.change import (
    TILE_DT,
    ClassState,
    classify_tiles,
    residual,
    thresholds,
    C_UNCHANGED,
    C_JITTER,
    C_CHANGED,
    C_MOVED,
    C_FLICKER,
    FLAG_GLOBAL_MV,
)
from visioncore.grid import Grid


class TestIslandRescueAndResidUnits(unittest.TestCase):
    """Passing contract unit tests for local-island motion rescue and q4 display residual."""

    def test_q4_vs_display_residual_contract(self):
        """Verify integer q4 residual distances and displayed resid values (8, 7, saturated 255, and 0 for UNCHANGED)."""
        t_enter, t_exit, _ = thresholds()
        self.assertEqual(t_enter, 35)
        self.assertEqual(t_exit, 28)

        # 1. Direct residual calculation tests
        a = np.zeros(1, dtype=TILE_DT)
        b = np.zeros(1, dtype=TILE_DT)

        # Isolated y_q4 difference = 35
        a["y_q4"] = 100
        b["y_q4"] = 135
        r_35 = residual(a, b)
        self.assertEqual(int(r_35[0]), 35)

        # Isolated y_q4 difference = 28
        b["y_q4"] = 128
        r_28 = residual(a, b)
        self.assertEqual(int(r_28[0]), 28)

        # Isolated y_q4 difference = 1040
        b["y_q4"] = 1140
        r_1040 = residual(a, b)
        self.assertEqual(int(r_1040[0]), 1040)

        # 2. Classification and displayed residual tests
        # Create 4 tiles:
        # Tile 0: r1 = 35 (cold tile -> JITTER, display resid 8)
        # Tile 1: r1 = 28 (cold tile -> JITTER, display resid 7)
        # Tile 2: r1 = 1040 (cold tile -> CHANGED, display resid 255)
        # Tile 3: words_eq = True (UNCHANGED, display resid 0)
        t_cur = np.zeros(4, dtype=TILE_DT)
        t_cur["morton"] = np.array([0, 1, 2, 3], dtype=np.uint16)
        t_cur["y_q4"] = np.array([135, 128, 1140, 100], dtype=np.uint16)

        t_prev = np.zeros(4, dtype=TILE_DT)
        t_prev["morton"] = np.array([0, 1, 2, 3], dtype=np.uint16)
        t_prev["y_q4"] = np.array([100, 100, 100, 100], dtype=np.uint16)

        st = ClassState.init(t_prev)
        # st.hot is zero (cold tile)
        # Avoid lag-two return by making prev2 == prev1 so r2 == r1
        st.prev1 = t_prev.copy()
        st.prev2 = t_prev.copy()

        words_eq = np.array([False, False, False, True], dtype=bool)

        out, st, summary = classify_tiles(st, t_cur, words_eq)

        # Tile 0: r1 = 35. At defaults t_enter=35. Cold tile hot=False -> 35 > 35 is False (not active).
        # Classified as JITTER, display resid 35 >> 2 = 8.
        self.assertEqual(out["cls"][0], C_JITTER)
        self.assertEqual(out["resid"][0], 8)

        # Tile 1: r1 = 28. Cold tile -> 28 > 35 is False (not active).
        # Classified as JITTER, display resid 28 >> 2 = 7.
        self.assertEqual(out["cls"][1], C_JITTER)
        self.assertEqual(out["resid"][1], 7)

        # Tile 2: r1 = 1040. Cold tile -> 1040 > 35 is True (active).
        # Classified as CHANGED, display resid min(255, 1040 >> 2) = 255.
        self.assertEqual(out["cls"][2], C_CHANGED)
        self.assertEqual(out["resid"][2], 255)

        # Tile 3: words_eq = True -> UNCHANGED, display resid = 0.
        self.assertEqual(out["cls"][3], C_UNCHANGED)
        self.assertEqual(out["resid"][3], 0)

    def test_local_island_motion_rescue_and_candidate_pool(self):
        """Verify local-island motion rescue, candidate pool filtering, and outside tile explanation."""
        # 64x64 grid with tile size s=16 -> 4x4 tiles (16 tiles total).
        grid = Grid(64, 64)
        s, gw, gh, N = grid.s, grid.gw, grid.gh, grid.N
        self.assertEqual((s, gw, gh, N), (16, 4, 4, 16))

        # Central 2x2 pane: (tx, ty) in {(1,1), (2,1), (1,2), (2,2)}
        central_coords = {(1, 1), (2, 1), (1, 2), (2, 2)}
        central_indices = []
        outside_shifted_index = None
        provisional_flicker_index = None
        other_active_indices = []
        inactive_indices = []

        # Identify Morton index for each tile position
        for i in range(N):
            tx, ty = int(grid.tx[i]), int(grid.ty[i])
            if (tx, ty) in central_coords:
                central_indices.append(i)
            elif (tx, ty) == (1, 0):  # Outside tile shifted by 4
                outside_shifted_index = i
            elif (tx, ty) == (2, 0):  # Provisional flicker tile
                provisional_flicker_index = i
            elif (tx, ty) in {(3, 0), (3, 1), (3, 2), (3, 3), (0, 3)}:
                other_active_indices.append(i)
            else:
                inactive_indices.append(i)

        self.assertEqual(len(central_indices), 4)
        self.assertEqual(len(other_active_indices), 5)

        # Construct luma planes y_prev and y_cur (64x64)
        y_prev = np.full((64, 64), 100, dtype=np.int16)
        y_cur = np.full((64, 64), 100, dtype=np.int16)

        def pattern(y, x):
            return 30 + ((x * 7 + y * 3) % 180)

        # Populate central 2x2 pane pattern (y in 16..48, x in 12..48 for y_prev, 16..48 for y_cur)
        for y in range(16, 48):
            for x in range(12, 48):
                y_prev[y, x] = pattern(y, x)
            for x in range(16, 48):
                y_cur[y, x] = pattern(y, x - 4)

        # Populate outside shifted tile (1, 0) pattern (y in 0..16, x in 12..32 for y_prev, 16..32 for y_cur)
        for y in range(0, 16):
            for x in range(12, 32):
                y_prev[y, x] = pattern(y, x)
            for x in range(16, 32):
                y_cur[y, x] = pattern(y, x - 4)

        # Random noise for other active tiles in y_prev and y_cur so vector (4,0) doesn't explain them
        rng = np.random.RandomState(42)
        for idx in other_active_indices:
            tx, ty = int(grid.tx[idx]), int(grid.ty[idx])
            y0, y1 = ty * 16, (ty + 1) * 16
            x0, x1 = tx * 16, (tx + 1) * 16
            y_prev[y0:y1, x0:x1] = 100 + rng.randint(-30, 30, size=(16, 16))
            y_cur[y0:y1, x0:x1] = 100 + rng.randint(-30, 30, size=(16, 16))

        # Construct tile descriptor records
        t_cur = np.zeros(N, dtype=TILE_DT)
        t_cur["morton"] = grid.mortons

        t_prev1 = np.zeros(N, dtype=TILE_DT)
        t_prev1["morton"] = grid.mortons

        t_prev2 = np.zeros(N, dtype=TILE_DT)
        t_prev2["morton"] = grid.mortons

        # Set descriptor y_q4 values to control residual r1 and r2
        active_indices = central_indices + [outside_shifted_index] + other_active_indices + [provisional_flicker_index]

        for idx in active_indices:
            t_cur["y_q4"][idx] = 200
            t_prev1["y_q4"][idx] = 100  # r1 = 100 > t_enter (35)
            t_prev2["y_q4"][idx] = 100  # r2 = 100 > t_exit (28)

        # Make tile (2,0) a provisional flicker tile: r1 > 35, r2 = 0 <= 28
        t_prev2["y_q4"][provisional_flicker_index] = 200  # r2 = 0 <= t_exit (28)

        # Inactive tiles
        for idx in inactive_indices:
            t_cur["y_q4"][idx] = 100
            t_prev1["y_q4"][idx] = 100  # r1 = 0 <= 35
            t_prev2["y_q4"][idx] = 100

        words_eq = np.zeros(N, dtype=bool)
        for idx in inactive_indices:
            words_eq[idx] = True

        st = ClassState.init(t_prev1)
        st.hot = np.ones(N, dtype=np.uint8)  # Hot tiles so active = r1 > t_exit (28) & ~words_eq
        st.prev1 = t_prev1.copy()
        st.prev2 = t_prev2.copy()
        st.y_prev = y_prev  # Set previous luma frame

        # Run classification
        out, st, summary = classify_tiles(st, t_cur, words_eq, y_cur=y_cur, grid=(16, 4, 4))

        # Verification 1: Global explanation share is below 500,000 ppm
        self.assertIsNone(summary["global_mv"])

        # Verification 2: Active tiles mask and provisional flicker mask
        # 11 active tiles total (4 central + 1 outside + 5 other active + 1 provisional flicker)
        # 1 provisional flicker tile (tile (2,0))
        # cand_base = active & ~flicker (10 tiles)
        for idx in central_indices:
            self.assertEqual(out["cls"][idx], C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 4)
            self.assertEqual(out["mv_dy"][idx], 0)
            self.assertEqual(out["flags"][idx] & FLAG_GLOBAL_MV, 0)

        # Verification 3: Outside eligible cand_base tile (1, 0) is explained by island offer
        self.assertEqual(out["cls"][outside_shifted_index], C_MOVED)
        self.assertEqual(out["mv_dx"][outside_shifted_index], 4)
        self.assertEqual(out["mv_dy"][outside_shifted_index], 0)
        self.assertEqual(out["flags"][outside_shifted_index] & FLAG_GLOBAL_MV, 0)

        # Verification 4: Provisional flicker tile (2, 0) is NOT in cand_base pool for island offer
        self.assertEqual(out["cls"][provisional_flicker_index], C_FLICKER)
        self.assertEqual(out["mv_dx"][provisional_flicker_index], 0)
        self.assertEqual(out["mv_dy"][provisional_flicker_index], 0)


if __name__ == "__main__":
    unittest.main()
