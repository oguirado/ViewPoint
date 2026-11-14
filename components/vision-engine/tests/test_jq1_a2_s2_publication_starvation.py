"""Adversarial tests for DEF-A2-02: S2 global-motion publication starvation and repeated-offer trap.

VM timing label: VM (never MEASURED).
"""
import unittest
from unittest.mock import patch

import numpy as np

from visioncore import change as C, layout, params as P


def create_starvation_fixture(num_flicker=9, seed=42):
    """Creates synthetic 64x64 grid with 16 tiles (4x4), s=16, translated by (4,0).

    Returns:
        grid: (16, 4, 4)
        y_prev: 64x64 int16 luma
        y_cur: 64x64 int16 luma
        t_prev2: TILE_DT[16]
        t_prev1: TILE_DT[16]
        t_cur: TILE_DT[16]
        state: ClassState
        flicker_indices: list of tile indices that satisfy provisional flicker
    """
    s, gw, gh = 16, 4, 4
    n_tiles = gw * gh  # 16
    grid = (s, gw, gh)

    rng = np.random.default_rng(seed)

    # 1. Non-periodic textured luma plane y_prev
    y_prev = rng.integers(20, 220, (64, 64), dtype=np.int16)

    # y_cur is y_prev translated by dx=4, dy=0
    y_cur = np.zeros_like(y_prev)
    y_cur[:, 4:] = y_prev[:, :-4]
    # Left border x in [0, 3] filled with distinct non-periodic values
    y_cur[:, :4] = rng.integers(20, 220, (64, 4), dtype=np.int16)

    # 2. Tile descriptor array setup
    tx_arr = np.array([i % 4 for i in range(n_tiles)])
    ty_arr = np.array([i // 4 for i in range(n_tiles)])
    mortons = np.array([layout.morton(int(x), int(y)) for x, y in zip(tx_arr, ty_arr)], dtype=np.uint16)

    t_cur = np.zeros(n_tiles, dtype=C.TILE_DT)
    t_cur["morton"] = mortons
    t_cur["y_q4"] = 1000

    t_prev1 = t_cur.copy()
    # r1 = |1000 - 960| = 40 > t_enter(35) for all 16 tiles
    t_prev1["y_q4"] = 960

    t_prev2 = t_cur.copy()
    # By default r2 = |1000 - 960| = 40 > t_exit(28) (not flicker)
    t_prev2["y_q4"] = 960

    # Select num_flicker tiles to have r2 <= 28 (provisional flicker)
    # Prefer interior/right tiles (tx >= 1) for flicker
    interior_right_indices = [i for i in range(n_tiles) if tx_arr[i] >= 1]  # 12 tiles
    flicker_indices = interior_right_indices[:num_flicker]

    for idx in flicker_indices:
        # r2 = |1000 - 1000| = 0 <= t_exit(28)
        t_prev2["y_q4"][idx] = 1000

    # 3. ClassState setup
    state = C.ClassState.init(t_prev2)
    state.prev2 = t_prev2.copy()
    state.prev1 = t_prev1.copy()
    state.y_prev = y_prev.copy()
    state.frame = 1

    return grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices


class TestS2PublicationStarvation(unittest.TestCase):
    """Adversarial tests for DEF-A2-02 global motion publication starvation."""

    def test_preconditions_and_residuals(self):
        """Verify synthetic preconditions: r1 > 35 for all 16 tiles, r2 <= 28 on 9 flicker tiles."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices = create_starvation_fixture(num_flicker=9)
        words_eq = np.array([t_cur[k].tobytes() == t_prev1[k].tobytes() for k in range(len(t_cur))])
        self.assertFalse(words_eq.any())

        r1 = C.residual(t_cur, t_prev1, P.PARAMS["H_DEAD"])
        r2 = C.residual(t_cur, t_prev2, P.PARAMS["H_DEAD"])

        t_enter, t_exit, _ = C.thresholds()
        self.assertEqual(t_enter, 35)
        self.assertEqual(t_exit, 28)

        active = r1 > t_enter
        self.assertEqual(int(active.sum()), 16)

        flicker = active & (r2 <= t_exit) & (r1 > t_enter)
        self.assertEqual(int(flicker.sum()), 9)
        self.assertEqual(set(np.flatnonzero(flicker)), set(flicker_indices))

    def test_boundary_eligible_counts(self):
        """Verify full-window eligible counts for global vector (4, 0).

        Left border tiles (tx=0, 4 tiles) are partially exposed (count < 256),
        while interior/right tiles (tx >= 1, 12 tiles) are full-window eligible (count = 256).
        """
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices = create_starvation_fixture(num_flicker=9)
        s, gw, gh = grid
        tx, ty = C._morton_xy(t_cur["morton"])

        sad, count = C._tile_sad_clip(y_cur, y_prev, (4, 0), s, gh, gw, tx, ty)

        # Left border tx=0 (4 tiles)
        left_border = tx == 0
        self.assertTrue((count[left_border] < s * s).all())

        # Interior/right tx >= 1 (12 tiles)
        interior_right = tx >= 1
        self.assertTrue((count[interior_right] == s * s).all())
        self.assertTrue((sad[interior_right] == 0).all())

    def test_current_publication_starvation(self):
        """Verify current behavior: with F=9 flicker tiles out of A=16 active tiles, cand_base has only 7 tiles.

        7/16 = 43.75% < GLOBAL_SHARE_PPM (50%), so global_mv is withdrawn (None).
        """
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices = create_starvation_fixture(num_flicker=9)
        words_eq = np.zeros(16, dtype=bool)

        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        # Due to publication starvation, global_mv is withdrawn (None)
        self.assertIsNone(summary["global_mv"])

    @unittest.expectedFailure
    def test_desired_publication_and_flicker_resolution(self):
        """DEF-A2-02: Require desired behavior where global motion [4, 0] is published and fully explained

        interior provisional-flicker tiles are classified as MOVED with vector (4, 0), even when F=9 > A/2=8.

        This test also catches a naive cand_base-only fix (e.g. setting cand_base = active), where a second
        offer on flicker tiles with identical SAD (0) fails strict `sad_c < best` inequality and fails to clear
        flicker tiles to MOVED.
        """
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices = create_starvation_fixture(num_flicker=9)
        words_eq = np.zeros(16, dtype=bool)

        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        # 1. Global motion vector [4, 0] must be published
        self.assertEqual(summary["global_mv"], [4, 0])

        # 2. Fully explained interior provisional-flicker tiles must be C_MOVED with vector (4, 0)
        for idx in flicker_indices:
            self.assertEqual(
                out["cls"][idx],
                C.C_MOVED,
                f"Provisional flicker tile at index {idx} was not cleared to C_MOVED"
            )
            self.assertEqual(out["mv_dx"][idx], 4)
            self.assertEqual(out["mv_dy"][idx], 0)


if __name__ == "__main__":
    unittest.main()
