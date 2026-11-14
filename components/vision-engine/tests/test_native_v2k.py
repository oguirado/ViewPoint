"""Conformance and V2EVAL2 plug-in tests for vqa_native_v2k and its *_nat adapters."""

import importlib
import os
import unittest
from unittest import mock
import numpy as np

from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid
from visioncore.layout import grid_params
from eval import native_v2k, v2proto_global_motion, v2proto_subtile
from eval.v2eval import plugins
from eval.v2x import gm_proto_nat, st_block4, st_block4_nat, st_proto8_nat


def _shift_frame_edge_padded(frame, dx, dy):
    """Return a C-contiguous frame where cur[y, x] = prev[clamp(y - dy), clamp(x - dx)]."""
    h, w = frame.shape
    ys = np.clip(np.arange(h, dtype=np.int32) - int(dy), 0, h - 1)
    xs = np.clip(np.arange(w, dtype=np.int32) - int(dx), 0, w - 1)
    return np.ascontiguousarray(frame[ys[:, None], xs[None, :]], dtype=np.uint8)


def _make_subtile_shifted_pair(w, h, s, target_tx, target_ty, dx, dy, rng, bg=200):
    """Create a (prev, cur) pair with a textured patch inside target tile shifted by (dx, dy)."""
    prev = np.full((h, w), bg, dtype=np.uint8)
    cur = np.full((h, w), bg, dtype=np.uint8)

    tx0 = target_tx * s
    ty0 = target_ty * s
    tx1 = min(w, (target_tx + 1) * s)
    ty1 = min(h, (target_ty + 1) * s)
    tw = tx1 - tx0
    th = ty1 - ty0

    # Place a sub-patch inside the source region so both source and destination lie in bounds
    margin_x = max(abs(dx) + 1, 2)
    margin_y = max(abs(dy) + 1, 2)
    pw = max(4, min(12, tw - 2 * margin_x))
    ph = max(4, min(12, th - 2 * margin_y))

    px0 = max(0, min(w - pw, tx0 + (tw - pw) // 2 - dx))
    py0 = max(0, min(h - ph, ty0 + (th - ph) // 2 - dy))
    cx0 = px0 + dx
    cy0 = py0 + dy

    if 0 <= cx0 and cx0 + pw <= w and 0 <= cy0 and cy0 + ph <= h:
        patch = rng.integers(10, 180, size=(ph, pw), dtype=np.uint8)
        prev[py0 : py0 + ph, px0 : px0 + pw] = patch
        cur[cy0 : cy0 + ph, cx0 : cx0 + pw] = patch

    return prev, cur


def _candidate_tiles_for_grid(gw, gh):
    """Return corner, edge, interior, and out-of-bounds tile coordinates for (gw, gh)."""
    tiles = [
        # Corners
        (0, 0),
        (gw - 1, 0),
        (0, gh - 1),
        (gw - 1, gh - 1),
        # Edges
        (gw // 2, 0),
        (gw // 2, gh - 1),
        (0, gh // 2),
        (gw - 1, gh // 2),
        # Interior
        (gw // 2, gh // 2),
        (max(0, gw // 3), max(0, gh // 3)),
        # Out-of-bounds
        (-1, 0),
        (0, -1),
        (gw, 0),
        (0, gh),
    ]
    seen = set()
    unique = []
    for xy in tiles:
        if xy not in seen:
            seen.add(xy)
            unique.append(xy)
    return unique


class TestNativeV2K(unittest.TestCase):
    """Test suite for rust/vqa_native_v2k, eval/native_v2k.py, and V2EVAL2 *_nat plug-ins."""

    def test_loader_and_missing_so_error(self):
        mod = native_v2k.load_native_v2k()
        self.assertEqual(mod.__name__, "vqa_native_v2k")
        for fn_name in (
            "st_proto8_subtile_motion",
            "st_block4_subtile_motion",
            "st_block4_batch",
            "gm_estimate",
            "gm_consensus",
        ):
            self.assertTrue(
                hasattr(mod, fn_name) and callable(getattr(mod, fn_name)),
                f"Missing callable {fn_name} on vqa_native_v2k",
            )

        missing_path = "/tmp/nonexistent_vqa_native_v2k_dir/libvqa_native_v2k.so"
        with mock.patch.dict(os.environ, {"VQA_NATIVE_V2K_SO": missing_path}):
            with self.assertRaises(ImportError) as ctx:
                native_v2k.load_native_v2k()
            self.assertIn(missing_path, str(ctx.exception))

        # Restore cached module after mock
        native_v2k.load_native_v2k()

    def test_c_contiguous_and_readonly_buffer_checks(self):
        mod = native_v2k.load_native_v2k()
        rng = np.random.default_rng(101)
        prev = rng.integers(0, 256, size=(48, 64), dtype=np.uint8)
        cur = _shift_frame_edge_padded(prev, 2, -1)

        # Non-C-contiguous views (strided slice and Fortran-ordered transpose) must be rejected
        non_contig_slice = prev[:, ::2]
        non_contig_fortran = np.asfortranarray(prev)
        with self.assertRaises((ValueError, BufferError)):
            mod.st_proto8_subtile_motion(non_contig_slice, cur[:, ::2], 16, (1, 1), 4)
        with self.assertRaises((ValueError, BufferError)):
            mod.st_block4_subtile_motion(non_contig_fortran, cur, 16, (1, 1), 4)
        with self.assertRaises((ValueError, BufferError)):
            mod.st_block4_batch(non_contig_fortran, cur, 16, [(1, 1)], 4)
        with self.assertRaises((ValueError, BufferError)):
            mod.gm_estimate(non_contig_fortran, cur, 8)
        with self.assertRaises((ValueError, BufferError)):
            mod.gm_consensus(non_contig_fortran, cur, 16, 2, -1)

        # Read-only C-contiguous buffers (as set by eval.v2eval.plugins._worker) must work zero-copy
        prev_ro = prev.copy()
        cur_ro = cur.copy()
        prev_ro.flags.writeable = False
        cur_ro.flags.writeable = False
        self.assertEqual(
            gm_proto_nat.estimate_global_motion(prev_ro, cur_ro, 8),
            v2proto_global_motion.estimate_global_motion(prev_ro, cur_ro, 8),
        )

    def test_exact_parity_small_and_medium_fixtures(self):
        rng = np.random.default_rng(20261019)
        sizes = [(64, 48), (63, 45), (320, 180)]
        shifts = [(3, -2), (-4, 1), (0, 3), (-2, -3)]

        for w, h in sizes:
            s, gw, gh = grid_params(w, h)
            max_d = min(6, s - 1)
            tiles = _candidate_tiles_for_grid(gw, gh)

            # 1. Identical frames
            ident = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
            self._assert_all_kernels_equal(ident, ident.copy(), s, tiles, max_d, (0, 0))

            # 2. Low-texture frames (flat constant and sparse <8 pixel noise)
            flat_prev = np.full((h, w), 128, dtype=np.uint8)
            flat_cur = np.full((h, w), 128, dtype=np.uint8)
            self._assert_all_kernels_equal(flat_prev, flat_cur, s, tiles, max_d, (0, 0))

            low_tex_prev = np.full((h, w), 210, dtype=np.uint8)
            low_tex_cur = low_tex_prev.copy()
            low_tex_cur[h // 2, w // 2] = 30
            low_tex_cur[0, 0] = 40
            self._assert_all_kernels_equal(
                low_tex_prev, low_tex_cur, s, tiles, max_d, (1, 0)
            )

            # 3. Random independent frames
            rand_prev = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
            rand_cur = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
            self._assert_all_kernels_equal(
                rand_prev, rand_cur, s, tiles, max_d, (2, -1)
            )

            # 4. Whole-frame shifted pairs
            for dx, dy in shifts:
                base = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
                shifted = _shift_frame_edge_padded(base, dx, dy)
                self._assert_all_kernels_equal(
                    base, shifted, s, tiles, max_d, (dx, dy)
                )

            # 5. Subtile-shifted pairs targeting corner, edge, and interior tiles
            focus_tiles = [
                (0, 0),
                (gw - 1, 0),
                (0, gh - 1),
                (gw - 1, gh - 1),
                (gw - 1, gh // 2),
                (gw // 2, gh - 1),
                (gw // 2, gh // 2),
            ]
            for tx, ty in focus_tiles:
                for dx, dy in [(2, -1), (-3, 2)]:
                    p_sub, c_sub = _make_subtile_shifted_pair(
                        w, h, s, tx, ty, dx, dy, rng
                    )
                    self._assert_subtile_kernels_equal(
                        p_sub, c_sub, s, tiles, max_d
                    )

    def test_exact_parity_1920x1080_pair(self):
        w, h = 1920, 1080
        s, gw, gh = grid_params(w, h)
        max_d = 8

        # Build a 1920x1080 pair with both global texture and localized shifted subtile patches
        prev, cur, s_bench, cls2_tiles, gm_prev, gm_cur = (
            native_v2k._make_shifted_1080p_pair(seed=9001, dx=3, dy=-2)
        )
        self.assertEqual(s, s_bench)

        # Include corners, right/bottom partial edges (1080 % 32 == 24), and active cls-2 tiles
        test_tiles = (
            _candidate_tiles_for_grid(gw, gh)
            + cls2_tiles[:8]
            + [(gw - 1, ty) for ty in (0, 1, gh // 2, gh - 1)]
            + [(tx, gh - 1) for tx in (0, 1, gw // 2, gw - 1)]
        )
        self._assert_subtile_kernels_equal(prev, cur, s, test_tiles, max_d)

        # Also check identical and low-texture 1080p corner/edge tiles
        self._assert_subtile_kernels_equal(prev, prev, s, test_tiles[:8], max_d)
        low_prev = np.full((h, w), 180, dtype=np.uint8)
        low_cur = low_prev.copy()
        low_cur[1075:1079, 1915:1919] = 40
        self._assert_subtile_kernels_equal(low_prev, low_cur, s, test_tiles[:8], max_d)

        # Global motion estimate & consensus on 1920x1080
        gm_max_d = 6
        ref_gm = v2proto_global_motion.estimate_global_motion(
            gm_prev, gm_cur, gm_max_d, k_top=2
        )
        nat_gm = gm_proto_nat.estimate_global_motion(
            gm_prev, gm_cur, gm_max_d, k_top=2
        )
        self.assertEqual(nat_gm, ref_gm)

        ref_mask = v2proto_global_motion.consensus_tiles(gm_prev, gm_cur, s, 3, -2)
        nat_mask = gm_proto_nat.consensus_tiles(gm_prev, gm_cur, s, 3, -2)
        self.assertEqual(nat_mask.shape, (gh, gw))
        self.assertEqual(nat_mask.dtype, np.bool_)
        self.assertTrue(np.array_equal(nat_mask, ref_mask))

    def _assert_subtile_kernels_equal(self, prev, cur, s, tiles, max_d):
        for xy in tiles:
            ref8 = v2proto_subtile.subtile_motion(prev, cur, s, xy, max_d)
            nat8 = st_proto8_nat.subtile_motion(prev, cur, s, xy, max_d)
            self.assertEqual(nat8, ref8, f"st_proto8 mismatch at tile={xy}")

            ref4 = st_block4.subtile_motion(prev, cur, s, xy, max_d)
            nat4 = st_block4_nat.subtile_motion(prev, cur, s, xy, max_d)
            self.assertEqual(nat4, ref4, f"st_block4 mismatch at tile={xy}")

        # Verify st_block4_batch matches element-wise reference results
        ref_batch = [
            st_block4.subtile_motion(prev, cur, s, xy, max_d) for xy in tiles
        ]
        nat_batch = native_v2k.st_block4_batch(prev, cur, s, tiles, max_d)
        self.assertEqual(nat_batch, ref_batch)
        # Also test alternate (prev, cur, s, max_d, tiles) parameter order
        nat_batch_alt = native_v2k.st_block4_batch(prev, cur, s, max_d, tiles)
        self.assertEqual(nat_batch_alt, ref_batch)

        # Verify None tile size fallback matches Python reference
        sample_xy = tiles[0]
        self.assertEqual(
            st_proto8_nat.subtile_motion(prev, cur, None, sample_xy, max_d),
            v2proto_subtile.subtile_motion(prev, cur, None, sample_xy, max_d),
        )
        self.assertEqual(
            st_block4_nat.subtile_motion(prev, cur, None, sample_xy, max_d),
            st_block4.subtile_motion(prev, cur, None, sample_xy, max_d),
        )

    def _assert_all_kernels_equal(self, prev, cur, s, tiles, max_d, gm_shift):
        self._assert_subtile_kernels_equal(prev, cur, s, tiles, max_d)

        gm_max_d = max(4, max_d)
        ref_est = v2proto_global_motion.estimate_global_motion(
            prev, cur, gm_max_d, k_top=16
        )
        nat_est = gm_proto_nat.estimate_global_motion(prev, cur, gm_max_d, k_top=16)
        self.assertEqual(nat_est, ref_est)

        dx, dy = gm_shift
        ref_cons = v2proto_global_motion.consensus_tiles(prev, cur, s, dx, dy)
        nat_cons = gm_proto_nat.consensus_tiles(prev, cur, s, dx, dy)
        self.assertIsInstance(nat_cons, np.ndarray)
        self.assertEqual(nat_cons.shape, ref_cons.shape)
        self.assertEqual(nat_cons.dtype, np.bool_)
        self.assertTrue(np.array_equal(nat_cons, ref_cons))

    def test_v2eval_plugins_contract_and_worker_execution(self):
        specs = [
            ("st=eval.v2x.st_proto8_nat", "st=eval.v2proto_subtile"),
            ("st=eval.v2x.st_block4_nat", "st=eval.v2x.st_block4"),
            ("gm=eval.v2x.gm_proto_nat", "gm=eval.v2proto_global_motion"),
        ]
        grid = Grid(64, 48)
        rng = np.random.default_rng(777)

        for nat_spec, ref_spec in specs:
            kind, mod_name = plugins.parse_variant(nat_spec)
            mod = importlib.import_module(mod_name)
            if kind == "gm":
                self.assertTrue(callable(getattr(mod, "estimate_global_motion", None)))
                self.assertTrue(callable(getattr(mod, "consensus_tiles", None)))
            elif kind == "st":
                self.assertTrue(callable(getattr(mod, "subtile_motion", None)))

            # Build synthetic frame pair and tiles array for end-to-end Variant worker verification
            if kind == "gm":
                prev = rng.integers(0, 256, size=(grid.H, grid.W), dtype=np.uint8)
                cur = _shift_frame_edge_padded(prev, 2, -1)
            else:
                prev, cur = _make_subtile_shifted_pair(
                    grid.W, grid.H, grid.s, 1, 1, 2, -1, rng
                )

            tiles = np.zeros(grid.N, dtype=TILE_DTYPE)
            tiles["cls"] = 2

            nat_var = plugins.Variant(nat_spec, grid, frame_budget_ms=5000)
            ref_var = plugins.Variant(ref_spec, grid, frame_budget_ms=5000)
            try:
                self.assertEqual(nat_var.status, "OK", nat_var.error)
                self.assertEqual(ref_var.status, "OK", ref_var.error)
                nat_out = nat_var.update(prev, cur, tiles)
                ref_out = ref_var.update(prev, cur, tiles)
                self.assertEqual(nat_var.status, "OK", nat_var.error)
                self.assertIsNotNone(nat_out)
                self.assertIsNotNone(ref_out)
                for key in ("cls", "mv_dx", "mv_dy"):
                    self.assertTrue(
                        np.array_equal(nat_out[key], ref_out[key]),
                        f"Mismatch on {key} for {nat_spec} vs {ref_spec}",
                    )
            finally:
                nat_var.close()
                ref_var.close()


if __name__ == "__main__":
    unittest.main()
