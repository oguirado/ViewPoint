"""Adversarial tests for fx2-f6-j2-s2 motion publication under provisional flicker.

VM timing label: VM (never MEASURED).
"""
import unittest

import numpy as np

from visioncore import change as C, desc, layout, params as P


def create_motion_flicker_fixture(dx=4, dy=0, num_flicker=9, seed=42, add_competing_island=False):
    """Creates a synthetic grid (4x4 tiles of 16x16, total 64x64) with displacement (dx, dy).

    Args:
        dx: Horizontal motion displacement (-16 < dx < 16).
        dy: Vertical motion displacement (-16 < dy < 16).
        num_flicker: Number of provisional flicker tiles (A-B-A) among full-window eligible tiles.
        seed: PRNG seed for deterministic non-periodic texture.
        add_competing_island: If True, adds a distinct sprite with island motion in part of the grid.

    Returns:
        grid: (16, 4, 4)
        y_prev: 64x64 int16 luma
        y_cur: 64x64 int16 luma
        t_prev2: TILE_DT[16]
        t_prev1: TILE_DT[16]
        t_cur: TILE_DT[16]
        state: ClassState
        flicker_indices: List of tile indices that satisfy provisional flicker
        full_win_indices: List of tile indices whose translated window is 100% in-bounds (count=256)
    """
    s, gw, gh = 16, 4, 4
    n_tiles = gw * gh
    grid = (s, gw, gh)

    rng = np.random.default_rng(seed)

    # 1. Non-periodic textured luma plane y_prev
    y_prev = rng.integers(20, 220, (gh * s, gw * s), dtype=np.int16)

    # 2. Translate y_prev by (dx, dy) into y_cur
    y_cur = np.zeros_like(y_prev)

    # Handle all 4 directional translations
    if dx > 0:
        y_cur[:, dx:] = y_prev[:, :-dx]
        y_cur[:, :dx] = rng.integers(20, 220, (gh * s, dx), dtype=np.int16)
    elif dx < 0:
        y_cur[:, :gw * s + dx] = y_prev[:, -dx:]
        y_cur[:, gw * s + dx:] = rng.integers(20, 220, (gh * s, -dx), dtype=np.int16)
    elif dy > 0:
        y_cur[dy:, :] = y_prev[:-dy, :]
        y_cur[:dy, :] = rng.integers(20, 220, (dy, gw * s), dtype=np.int16)
    elif dy < 0:
        y_cur[:gh * s + dy, :] = y_prev[-dy:, :]
        y_cur[gh * s + dy:, :] = rng.integers(20, 220, (-dy, gw * s), dtype=np.int16)
    else:
        y_cur[:, :] = y_prev[:, :]

    # Option: Add a distinct competing island sprite if requested
    if add_competing_island:
        # Place sprite at bottom-right corner tile (tx=3, ty=3) with an opposite/competing motion
        # e.g., local sprite moving by (-4, 0)
        spr = rng.integers(30, 200, (s, s), dtype=np.int16)
        y_prev[3 * s:4 * s, 3 * s:4 * s] = spr
        y_cur[3 * s:4 * s, 3 * s - 4:4 * s - 4] = spr

    # 3. Descriptor setup and Morton layout
    tx_arr = np.array([i % gw for i in range(n_tiles)])
    ty_arr = np.array([i // gw for i in range(n_tiles)])
    mortons = np.array([layout.morton(int(x), int(y)) for x, y in zip(tx_arr, ty_arr)], dtype=np.uint16)

    rgb_cur = np.repeat(y_cur[..., None], 3, axis=-1).astype(np.uint8)
    rgb_prev = np.repeat(y_prev[..., None], 3, axis=-1).astype(np.uint8)

    t_cur = np.zeros(n_tiles, dtype=C.TILE_DT)
    t_cur["morton"] = mortons
    t_cur["y_q4"] = 1000

    # Fill true content hashes (xh64_of) for current frame
    for i in range(n_tiles):
        x, y = tx_arr[i], ty_arr[i]
        block = rgb_cur[y * s:(y + 1) * s, x * s:(x + 1) * s, :]
        t_cur["xh64"][i] = desc.xh64_of(block)

    t_prev1 = t_cur.copy()
    # r1 = |1000 - 960| = 40 > t_enter(35) for active motion
    t_prev1["y_q4"] = 960

    # Fill true content hashes (xh64_of) for prev1 frame
    for i in range(n_tiles):
        x, y = tx_arr[i], ty_arr[i]
        block = rgb_prev[y * s:(y + 1) * s, x * s:(x + 1) * s, :]
        t_prev1["xh64"][i] = desc.xh64_of(block)

    t_prev2 = t_prev1.copy()

    # Determine full-window eligible tiles for the given displacement
    if dx > 0:
        full_win_indices = [i for i in range(n_tiles) if tx_arr[i] >= 1]
    elif dx < 0:
        full_win_indices = [i for i in range(n_tiles) if tx_arr[i] < gw - 1]
    elif dy > 0:
        full_win_indices = [i for i in range(n_tiles) if ty_arr[i] >= 1]
    elif dy < 0:
        full_win_indices = [i for i in range(n_tiles) if ty_arr[i] < gh - 1]
    else:
        full_win_indices = list(range(n_tiles))

    # Assign provisional flicker (r2 <= 28) to the specified number of full-window tiles
    flicker_indices = full_win_indices[:num_flicker]
    for idx in flicker_indices:
        t_prev2["y_q4"][idx] = 1000

    # 4. ClassState initialization
    state = C.ClassState.init(t_prev2)
    state.prev2 = t_prev2.copy()
    state.prev1 = t_prev1.copy()
    state.y_prev = y_prev.copy()
    state.frame = 1

    return grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices


class TestMotionPublicationUnderFlickerOrdinary(unittest.TestCase):
    """Ordinary unit tests proving preconditions, content provenance, negative controls, and publication starvation below threshold."""

    def test_preconditions_all_orientations_and_integer_support(self):
        """Prove preconditions for provisional flicker, nonzero translation, overlap, SAD, and exact integer support arithmetic."""
        orientations = [(4, 0), (-4, 0), (0, 4), (0, -4)]
        t_enter, t_exit, _ = C.thresholds()
        self.assertEqual(t_enter, 35)
        self.assertEqual(t_exit, 28)

        for dx, dy in orientations:
            with self.subTest(displacement=(dx, dy)):
                grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
                    dx=dx, dy=dy, num_flicker=9
                )
                s, gw, gh = grid
                tx, ty = C._morton_xy(t_cur["morton"])

                # Check residuals r1 and r2
                r1 = C.residual(t_cur, t_prev1, P.PARAMS["H_DEAD"])
                r2 = C.residual(t_cur, t_prev2, P.PARAMS["H_DEAD"])

                active = r1 > t_enter
                self.assertEqual(int(active.sum()), 16, "All 16 tiles must be active")

                flicker = active & (r2 <= t_exit)
                self.assertEqual(int(flicker.sum()), 9, "Exactly 9 tiles must satisfy provisional flicker")
                self.assertEqual(set(np.flatnonzero(flicker)), set(flicker_indices))

                # Window SAD and count calculation
                sad, count = C._tile_sad_clip(y_cur, y_prev, (dx, dy), s, gh, gw, tx, ty)

                # Full-window eligible tiles have count = 256 and SAD = 0
                for idx in full_win_indices:
                    self.assertEqual(count[idx], 256)
                    self.assertEqual(sad[idx], 0)

                # Edge / partially exposed tiles have count < 256
                partially_exposed = [i for i in range(16) if i not in full_win_indices]
                self.assertEqual(len(partially_exposed), 4)
                for idx in partially_exposed:
                    self.assertLess(count[idx], 256)

                # Integer arithmetic for support threshold
                # 9 full-window flicker tiles out of 16 active tiles
                # Without flicker resolution (current behavior), cand_base active non-flicker = 16 - 9 = 7
                # Support PPM = (7 * 1,000,000) // 16 = 437,500 ppm < GLOBAL_SHARE_PPM (500,000 ppm)
                cand_base = active & ~flicker
                expl_count = int(cand_base.sum())
                self.assertEqual(expl_count, 7)
                support_ppm = (expl_count * 1000000) // int(active.sum())
                self.assertEqual(support_ppm, 437500)
                self.assertLess(support_ppm, P.PARAMS["GLOBAL_SHARE_PPM"])

    def test_content_provenance_and_hash_validity(self):
        """Assert true content hashes (xh64) on descriptors so future hash-return guards cannot make fixtures vacuous."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9
        )
        s, gw, gh = grid
        tx, ty = C._morton_xy(t_cur["morton"])
        rgb_cur = np.repeat(y_cur[..., None], 3, axis=-1).astype(np.uint8)

        # Confirm that every descriptor xh64 equals desc.xh64_of applied to the actual frame tile bytes
        for i in range(16):
            x, y = tx[i], ty[i]
            block = rgb_cur[y * s:(y + 1) * s, x * s:(x + 1) * s, :]
            expected_hash = desc.xh64_of(block)
            self.assertEqual(t_cur["xh64"][i], expected_hash)
            self.assertNotEqual(t_cur["xh64"][i], 0, "Hash must be non-zero valid content hash")

    def test_negative_control_words_equal(self):
        """Negative control: When words_eq is True, unchanged tiles are bypassed and do not publish global motion."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9
        )
        words_eq = np.ones(16, dtype=bool)  # All words equal
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        # With all words equal, r1 is forced to 0, no global motion is published, and tiles cannot become C_MOVED
        self.assertIsNone(summary["global_mv"])
        self.assertFalse((out["cls"] == C.C_MOVED).any())

    def test_negative_control_unavailable_history(self):
        """Negative control: When history frame or y_prev is unavailable/None, global motion is safely bypassed."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)

        # Clear state.y_prev to simulate unavailable luma history
        state.y_prev = None
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertIsNone(summary["global_mv"])
        self.assertFalse((out["cls"] == C.C_MOVED).any())

    def test_publication_starvation_below_threshold_withdrawn(self):
        """Assert withdrawal (global_mv is None) across all 4 reflected/transposed orientations when support is 7/16 (43.75% < 50%)."""
        orientations = [(4, 0), (-4, 0), (0, 4), (0, -4)]
        words_eq = np.zeros(16, dtype=bool)

        for dx, dy in orientations:
            with self.subTest(displacement=(dx, dy)):
                grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
                    dx=dx, dy=dy, num_flicker=9
                )
                out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)
                self.assertIsNone(
                    summary["global_mv"],
                    f"Global motion must be withdrawn (None) for displacement ({dx}, {dy}) under 43.75% support"
                )


class TestMotionPublicationUnderFlickerContracts(unittest.TestCase):
    """Future-behavior contract test cases targeting fx2-f6-s2-flicker-veto-and-fl-v3."""

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_desired_publication_pos_x(self):
        """Contract: Published vector [4, 0], C_MOVED class, and signed motion fields (4, 0) for positive X displacement."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertEqual(summary["global_mv"], [4, 0])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 4)
            self.assertEqual(out["mv_dy"][idx], 0)

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_desired_publication_neg_x(self):
        """Contract: Published vector [-4, 0], C_MOVED class, and signed motion fields (-4, 0) for negative X displacement."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=-4, dy=0, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertEqual(summary["global_mv"], [-4, 0])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], -4)
            self.assertEqual(out["mv_dy"][idx], 0)

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_desired_publication_pos_y(self):
        """Contract: Published vector [0, 4], C_MOVED class, and signed motion fields (0, 4) for positive Y displacement."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=0, dy=4, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertEqual(summary["global_mv"], [0, 4])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 0)
            self.assertEqual(out["mv_dy"][idx], 4)

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_desired_publication_neg_y(self):
        """Contract: Published vector [0, -4], C_MOVED class, and signed motion fields (0, -4) for negative Y displacement."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=0, dy=-4, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertEqual(summary["global_mv"], [0, -4])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 0)
            self.assertEqual(out["mv_dy"][idx], -4)

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_desired_publication_exactly_at_threshold(self):
        """Contract: Published vector [4, 0] when support is exactly at 8/16 = 500,000 ppm == GLOBAL_SHARE_PPM."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=8
        )
        words_eq = np.zeros(16, dtype=bool)

        # Support check: 8 flicker tiles out of 16 full-window tiles
        # (8 * 1,000,000) // 16 = 500,000 ppm == GLOBAL_SHARE_PPM
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        self.assertEqual(summary["global_mv"], [4, 0])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 4)
            self.assertEqual(out["mv_dy"][idx], 0)

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_partially_exposed_windows_never_moved(self):
        """Contract: Partially exposed qualifying windows (count < 256) never become C_MOVED even when global motion is published."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9
        )
        words_eq = np.zeros(16, dtype=bool)

        # Mock summary and classify to test the contract asserting global motion vector is published
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        # Contract requirement 1: global_mv MUST be published
        self.assertEqual(summary["global_mv"], [4, 0])

        # Contract requirement 2: partially exposed edge tiles MUST NEVER become C_MOVED
        partially_exposed = [i for i in range(16) if i not in full_win_indices]
        for idx in partially_exposed:
            self.assertNotEqual(
                out["cls"][idx],
                C.C_MOVED,
                f"Partially exposed tile at index {idx} must never be classified as C_MOVED"
            )

    @unittest.expectedFailure  # task_id: fx2-f6-s2-flicker-veto-and-fl-v3
    def test_competing_island_and_zero_sad_reoffer_stability(self):
        """Contract: Equal-cost reoffers and competing island motions do not change the established global motion winner."""
        grid, y_prev, y_cur, t_prev2, t_prev1, t_cur, state, flicker_indices, full_win_indices = create_motion_flicker_fixture(
            dx=4, dy=0, num_flicker=9, add_competing_island=True
        )
        words_eq = np.zeros(16, dtype=bool)
        out, st, summary = C.classify_tiles(state, t_cur, words_eq, y_cur=y_cur, grid=grid)

        # Global motion winner [4, 0] must be preserved without corruption from competing island offer
        self.assertEqual(summary["global_mv"], [4, 0])
        for idx in flicker_indices:
            self.assertEqual(out["cls"][idx], C.C_MOVED)
            self.assertEqual(out["mv_dx"][idx], 4)
            self.assertEqual(out["mv_dy"][idx], 0)


if __name__ == "__main__":
    unittest.main()
