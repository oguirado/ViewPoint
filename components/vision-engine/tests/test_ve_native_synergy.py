"""Unit VE-NATIVE-1: Rust-Python synergy parity, mutation, fallback, dual_verify, and 1280x900 benchmarks."""
from pathlib import Path
import time
import unittest

import numpy as np

from eval import native_parity_s12
from tests.test_e1_change_pipeline import BASE_E_SHA, blob_sha, source
from visioncore import canon, change, desc, errors as vc_errors, grid as vgrid, layout, regions
from vnative import backend
from vnative.adapters import cu_regions, kernels

ROOT = Path(__file__).resolve().parent.parent


class TestVeNativeSynergy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = kernels.get_native_module()
        if cls.mod is None:
            raise unittest.SkipTest("visioncore_native.abi3.so not built")

    def tearDown(self):
        if self.mod is not None:
            self.mod.set_mutant(None)

    # ========================================================================
    # Brief 1: SAD windows with SIMD (_tile_sad_windows, _tile_sad, _tile_sad_clip)
    # ========================================================================

    def test_brief1_sad_windows_parity_random_and_edges(self):
        # 1. Edge: 1x1 frame with s=1
        y1 = np.array([[255]], dtype=np.int16)
        y2 = np.array([[-255]], dtype=np.int16)
        for c in [(0, 0), (1, 0), (0, -1)]:
            ref_s, ref_c = change._tile_sad_windows(y1, y2, c, 1, [0], [0])
            nat_s, nat_c = self.mod._tile_sad_windows(y1, y2, c, 1, [0], [0])
            np.testing.assert_array_equal(nat_s, ref_s)
            np.testing.assert_array_equal(nat_c, ref_c)

        # 2. Edge: empty tile request, scalar 0-D tile request, 2-D broadcast tile request
        rng = np.random.default_rng(1001)
        yc = rng.integers(-32768, 32768, (32, 48), dtype=np.int16)
        yp = rng.integers(-32768, 32768, (32, 48), dtype=np.int16)
        empty = np.array([], dtype=np.int64)
        rs, rc_arr = change._tile_sad_windows(yc, yp, (2, -1), 16, empty, empty)
        ns, nc_arr = self.mod._tile_sad_windows(yc, yp, (2, -1), 16, empty, empty)
        self.assertEqual(ns.shape, rs.shape)
        self.assertEqual(ns.dtype, rs.dtype)
        np.testing.assert_array_equal(ns, rs)
        np.testing.assert_array_equal(nc_arr, rc_arr)

        # Scalar 0-D request
        rs0, rc0 = change._tile_sad_windows(yc, yp, (2, -1), 16, 1, -1)
        ns0, nc0 = self.mod._tile_sad_windows(yc, yp, (2, -1), 16, 1, -1)
        self.assertEqual(int(ns0), int(rs0))
        self.assertEqual(int(nc0), int(rc0))

        # 2-D broadcast request
        tx_2d = np.array([[0], [2], [-1]], dtype=np.int64)
        ty_2d = np.array([[0, 1]], dtype=np.int64)
        rs2, rc2 = change._tile_sad_windows(yc, yp, (-3, 4), 16, tx_2d, ty_2d)
        ns2, nc2 = self.mod._tile_sad_windows(yc, yp, (-3, 4), 16, tx_2d, ty_2d)
        self.assertEqual(ns2.shape, rs2.shape)
        np.testing.assert_array_equal(ns2, rs2)
        np.testing.assert_array_equal(nc2, rc2)

        # 3. Randomized & extreme int16 values across odd sizes, negative indices, and clipping shifts
        for seed in range(6):
            rng = np.random.default_rng(2000 + seed)
            for s in (4, 8, 16, 32):
                for h, w in [(s * 2, s * 3), (s * 3 + 5, s * 4 + 7)]:
                    gh, gw = h // s, w // s
                    for kind in ("random", "extremes"):
                        if kind == "random":
                            y_cur = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
                            y_prev = rng.integers(-32768, 32768, (h, w), dtype=np.int16)
                        else:
                            y_cur = rng.choice(np.array([-32768, 32767], dtype=np.int16), (h, w))
                            y_prev = rng.choice(np.array([-32768, 32767], dtype=np.int16), (h, w))
                        tx = rng.integers(-gw, gw, size=gh * gw + 3, dtype=np.int64)
                        ty = rng.integers(-gh, gh, size=gh * gw + 3, dtype=np.int64)
                        for vec in [(0, 0), (1, -1), (s - 1, -(s - 1)), (s, 0), (-w - 5, h + 5)]:
                            r_sad, r_cnt = change._tile_sad_windows(y_cur, y_prev, vec, s, tx, ty)
                            n_sad, n_cnt = kernels._tile_sad_windows(y_cur, y_prev, vec, s, tx, ty)
                            self.assertEqual(n_sad.tobytes(), r_sad.tobytes())
                            self.assertEqual(n_cnt.tobytes(), r_cnt.tobytes())

                            r_s2, r_val = change._tile_sad(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                            n_s2, n_val = kernels._tile_sad(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                            self.assertEqual(n_s2.tobytes(), r_s2.tobytes())
                            self.assertEqual(n_val.tobytes(), r_val.tobytes())

                            r_sc, r_cc = change._tile_sad_clip(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                            n_sc, n_cc = kernels._tile_sad_clip(y_cur, y_prev, vec, s, gh, gw, tx, ty)
                            self.assertEqual(n_sc.tobytes(), r_sc.tobytes())
                            self.assertEqual(n_cc.tobytes(), r_cc.tobytes())

        # Out-of-bounds tile index raises IndexError
        with self.assertRaises(IndexError):
            kernels._tile_sad_windows(yc, yp, (0, 0), 16, [10], [0])
        with self.assertRaises(IndexError):
            kernels._tile_sad_windows(yc, yp, (0, 0), 16, [-10], [0])

        # Non-contiguous luma buffer raises ValueError (zero-copy C-contiguous check)
        with self.assertRaises(ValueError):
            self.mod._tile_sad_windows(yc[:, ::2], yp[:, ::2], (0, 0), 16, [0], [0])

    def test_brief1_mutant_sad_plus_one_killed(self):
        s0_rs = (ROOT / "rust/visioncore_native/src/s0.rs").read_text()
        self.assertIn("MUTANT:SAD_PLUS_ONE: tile_sad += 1;", s0_rs)

        y_cur = np.zeros((32, 32), dtype=np.int16)
        y_prev = np.zeros((32, 32), dtype=np.int16)
        tx = np.array([0, 1], dtype=np.int64)
        ty = np.array([0, 1], dtype=np.int64)
        ref_sad, _ = change._tile_sad_windows(y_cur, y_prev, (0, 0), 16, tx, ty)
        with kernels.mutant_scope("SAD_PLUS_ONE"):
            mut_sad, _ = kernels._tile_sad_windows(y_cur, y_prev, (0, 0), 16, tx, ty)
        self.assertFalse(np.array_equal(mut_sad, ref_sad))

    # ========================================================================
    # Brief 2: Motion (global_vector, _axis_shift, _tile_sums, _islands, island_vector)
    # ========================================================================

    def test_brief2_motion_parity_random_and_edges(self):
        # 1. Edge: length-1 projections, flat projections, exact 3x winner threshold, tie-breaks
        self.assertEqual(kernels._axis_shift(np.array([42], np.int64), np.array([42], np.int64), 4), 0)
        flat = np.full(32, 100, dtype=np.int64)
        self.assertEqual(kernels._axis_shift(flat, flat, 8), 0)

        # Randomized 1D projections across seeds
        for seed in range(25):
            rng = np.random.default_rng(3000 + seed)
            n = int(rng.integers(1, 90))
            dmax = int(rng.integers(1, 70))
            cur = rng.integers(-100000, 100000, size=n, dtype=np.int64)
            prev = rng.integers(-100000, 100000, size=n, dtype=np.int64)
            self.assertEqual(kernels._axis_shift(cur, prev, dmax), change._axis_shift(cur, prev, dmax))
            # Shifted signal with known displacement
            if n > 16:
                shift = int(rng.integers(-min(8, n // 3), min(8, n // 3) + 1))
                shifted = np.roll(prev, shift)
                self.assertEqual(kernels._axis_shift(shifted, prev, dmax), change._axis_shift(shifted, prev, dmax))

        # 2. Full-frame global_vector, _tile_sums, _islands, island_vector across odd and standard sizes
        for seed in range(8):
            rng = np.random.default_rng(4000 + seed)
            for h, w, s in [(1, 1, 16), (16, 16, 16), (63, 45, 16), (96, 128, 32), (135, 195, 128)]:
                y_cur = rng.integers(-32768, 32767, (h, w), dtype=np.int16)
                y_prev = np.roll(y_cur, shift=(int(rng.integers(-5, 6)), int(rng.integers(-5, 6))), axis=(0, 1))
                self.assertEqual(kernels.global_vector(y_cur, y_prev, s), change.global_vector(y_cur, y_prev, s))

            for gh, gw, s in [(1, 1, 16), (3, 4, 16), (5, 7, 8)]:
                absdiff = rng.integers(0, 1000, (gh * s, gw * s), dtype=np.int16)
                np.testing.assert_array_equal(
                    kernels._tile_sums(absdiff, s, gh, gw),
                    change._tile_sums(absdiff, s, gh, gw),
                )
                mask = rng.choice([False, True], size=(gh, gw))
                r_lab, r_n = change._islands(mask)
                n_lab, n_n = kernels._islands(mask)
                self.assertEqual(n_n, r_n)
                self.assertEqual(n_lab.tobytes(), r_lab.tobytes())
                if mask.any():
                    yc = rng.integers(-2000, 2000, (gh * s, gw * s), dtype=np.int16)
                    yp = np.roll(yc, shift=(-2, 3), axis=(0, 1))
                    self.assertEqual(
                        kernels.island_vector(mask, yc, yp, s),
                        change.island_vector(mask, yc, yp, s),
                    )

        # Non-contiguous check
        bad = np.zeros((32, 64), dtype=np.int16)[:, ::2]
        with self.assertRaises(ValueError):
            self.mod.global_vector(bad, bad, 16)

    def test_brief2_mutants_swap_dx_dy_and_drop_last_island_killed(self):
        par_rs = (ROOT / "rust/visioncore_native/src/par.rs").read_text()
        self.assertIn("MUTANT:SWAP_DX_DY: return (gdy, gdx);", par_rs)
        self.assertIn("MUTANT:DROP_LAST_ISLAND:", par_rs)

        rng = np.random.default_rng(4242)
        y_prev = rng.integers(0, 1000, (64, 64), dtype=np.int16)
        y_cur = np.roll(y_prev, shift=(0, 4), axis=(0, 1))
        ref_mv = change.global_vector(y_cur, y_prev, 16)
        self.assertEqual(ref_mv, (4, 0))
        with kernels.mutant_scope("SWAP_DX_DY"):
            mut_mv = kernels.global_vector(y_cur, y_prev, 16)
        self.assertNotEqual(mut_mv, ref_mv)

        mask = np.array([[True, False, True], [False, False, False], [True, False, True]], dtype=bool)
        ref_lab, ref_n = change._islands(mask)
        with kernels.mutant_scope("DROP_LAST_ISLAND"):
            mut_lab, mut_n = kernels._islands(mask)
        self.assertNotEqual(mut_n, ref_n)
        self.assertFalse(np.array_equal(mut_lab, ref_lab))

    # ========================================================================
    # Brief 3: S1 descriptors (luma_of_padded, _compute, ycocg, isqrt)
    # ========================================================================

    def test_brief3_s1_descriptors_parity_random_and_edges(self):
        # 1. Edge: isqrt on 0, 1, squares +/- 1, 2**52 - 1, and multi-dimensional arrays
        vals = np.array([0, 1, 2, 3, 4, 15, 16, 17, 255, 256, (1 << 52) - 1], dtype=np.int64)
        np.testing.assert_array_equal(kernels.isqrt(vals), desc.isqrt(vals))
        vals_2d = vals[:9].reshape(3, 3)
        np.testing.assert_array_equal(kernels.isqrt(vals_2d), desc.isqrt(vals_2d))

        # 2. Edge: 1x1 ycocg and luma_of_padded
        px = np.array([[[255, 0, 128]]], dtype=np.uint8)
        np.testing.assert_array_equal(kernels.luma_of_padded(px), desc.luma_of_padded(px))
        ry, rco, rcg = desc.ycocg(px)
        ny, nco, ncg = kernels.ycocg(px)
        np.testing.assert_array_equal(ny, ry)
        np.testing.assert_array_equal(nco, rco)
        np.testing.assert_array_equal(ncg, rcg)

        # 3. Full TILE_DTYPE byte-exact parity across frame sizes, plane types, and partial idx subsets
        for seed in range(6):
            rng = np.random.default_rng(5000 + seed)
            for W, H in [(16, 16), (63, 45), (127, 95), (320, 180)]:
                g = vgrid.Grid(W, H)
                for kind in ("random", "saturated", "flat"):
                    if kind == "random":
                        rgb = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
                    elif kind == "saturated":
                        rgb = rng.choice(np.array([0, 1, 254, 255], dtype=np.uint8), (H, W, 3))
                    else:
                        rgb = np.full((H, W, 3), 77, dtype=np.uint8)
                    padded = g.pad(rgb)
                    ref_y = desc.luma_of_padded(padded)
                    nat_y = kernels.luma_of_padded(padded)
                    self.assertEqual(nat_y.tobytes(), ref_y.tobytes())

                    ref_tiles, _ = desc.describe_padded(padded, g)
                    nat_tiles = kernels.describe_padded_tiles(padded, g)
                    self.assertEqual(nat_tiles.tobytes(), ref_tiles.tobytes())

                    # Partial idx subset test
                    if g.N > 2:
                        sub_idx = np.arange(0, g.N, 2, dtype=np.int64)
                        out_ref = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                        out_nat = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                        desc._compute(out_ref, sub_idx, padded, g, desc.PARAMS, ref_y)
                        kernels._compute(out_nat, sub_idx, padded, g, desc.PARAMS, nat_y)
                        self.assertEqual(out_nat.tobytes(), out_ref.tobytes())

    def test_brief3_mutant_flip_desc_byte_0_killed(self):
        s1_rs = (ROOT / "rust/visioncore_native/src/s1.rs").read_text()
        self.assertIn("MUTANT:FLIP_DESC_BYTE_0: out_bytes[0] ^= 1;", s1_rs)

        g = vgrid.Grid(32, 32)
        rgb = np.full((32, 32, 3), 120, dtype=np.uint8)
        padded = g.pad(rgb)
        ref_tiles, _ = desc.describe_padded(padded, g)
        with kernels.mutant_scope("FLIP_DESC_BYTE_0"):
            mut_tiles = kernels.describe_padded_tiles(padded, g)
            self.assertNotEqual(mut_tiles.tobytes(), ref_tiles.tobytes())
            with self.assertRaises(backend.VNativeError) as ctx:
                kernels.run_dual_verify(source("clean_static"))
            self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")

    # ========================================================================
    # Brief 4: S4 regions (_islands, regions_of, adjacency, match_regions)
    # ========================================================================

    def test_brief4_s4_regions_parity_random_and_edges(self):
        W, H = 320, 180
        s, gw, gh = layout.grid_params(W, H)

        for seed in range(10):
            rng = np.random.default_rng(6000 + seed)
            n_dirty = int(rng.integers(0, gw * gh))
            tiles = np.zeros(gw * gh, dtype=desc.TILE_DTYPE)
            mortons = [layout.morton(tx, ty) for ty in range(gh) for tx in range(gw)]
            tiles["morton"] = sorted(mortons)
            if n_dirty > 0:
                chosen = rng.choice(gw * gh, size=n_dirty, replace=False)
                tiles["cls"][chosen] = rng.choice([2, 3, 4, 5, 6], size=n_dirty)
                tiles["flags"][chosen] = rng.integers(0, 256, size=n_dirty, dtype=np.uint8)
                tiles["resid"][chosen] = rng.integers(1, 255, size=n_dirty, dtype=np.uint8)
                tiles["edge"][chosen] = rng.integers(0, 255, size=n_dirty, dtype=np.uint8)
                tiles["mv_dx"][chosen] = rng.choice([-4, 0, 4, 8], size=n_dirty)
                tiles["mv_dy"][chosen] = rng.choice([-4, 0, 4], size=n_dirty)

            ref_regs = regions.regions_of(tiles, W, H, seed)
            nat_regs = kernels.regions_of(tiles, W, H, seed)
            self.assertEqual(canon.dumps(nat_regs), canon.dumps(ref_regs))

            # Order independence on permuted tile array
            perm = rng.permutation(len(tiles))
            nat_perm = kernels.regions_of(tiles[perm], W, H, seed)
            self.assertEqual(canon.dumps(nat_perm), canon.dumps(ref_regs))

            # Adjacency parity
            ref_adj = regions.adjacency(ref_regs)
            nat_adj = kernels.adjacency(nat_regs)
            self.assertEqual(canon.dumps(nat_adj), canon.dumps(ref_adj))

            # match_regions parity against previous frame
            if seed > 0:
                ref_links = regions.match_regions(prev_regs, ref_regs, global_mv=(4, -2), s=s)
                nat_links = kernels.match_regions(prev_regs, nat_regs, global_mv=(4, -2), s=s)
                self.assertEqual(canon.dumps(nat_links), canon.dumps(ref_links))
            prev_regs = ref_regs

        # Negative error cases: out-of-grid Morton and grid mismatch
        bad_tiles = np.zeros(1, dtype=desc.TILE_DTYPE)
        bad_tiles[0]["morton"] = layout.morton(200, 200)
        with self.assertRaises(vc_errors.VqaError) as e1:
            kernels.regions_of(bad_tiles, W, H, 0)
        self.assertEqual(e1.exception.code, "E_VC_ID_RANGE")

    def test_brief4_mutants_sort_bbox_and_merge_mov_killed(self):
        par_rs = (ROOT / "rust/visioncore_native/src/par.rs").read_text()
        self.assertIn("MUTANT:SORT_REGIONS_BY_BBOX: out.sort_by_key(|r| r.bbox_t);", par_rs)
        self.assertIn("MUTANT:MERGE_DISTINCT_MOV_GROUPS: g_key = (1u8, 0, 0);", par_rs)

        W, H = 320, 180
        # Two islands where bbox raster order (ty, tx) differs from Morton order:
        # (tx=2, ty=0) has morton=4,ty=0; (tx=0, ty=1) has morton=2,ty=1 -> Morton order is (0,1) then (2,0),
        # whereas bbox (ty, tx) order is (2,0) then (0,1).
        t1 = np.zeros(2, dtype=desc.TILE_DTYPE)
        t1[0]["morton"] = layout.morton(2, 0)
        t1[0]["cls"] = 2
        t1[1]["morton"] = layout.morton(0, 1)
        t1[1]["cls"] = 2
        ref_r1 = regions.regions_of(t1, W, H, 0)
        with kernels.mutant_scope("SORT_REGIONS_BY_BBOX"):
            mut_r1 = kernels.regions_of(t1, W, H, 0)
        self.assertNotEqual([r["id"] for r in mut_r1], [r["id"] for r in ref_r1])

        # Two touching MOV tiles with distinct motion vectors (4, 0) and (8, 0)
        t2 = np.zeros(2, dtype=desc.TILE_DTYPE)
        t2[0]["morton"] = layout.morton(0, 0)
        t2[0]["cls"] = 3
        t2[0]["mv_dx"] = 4
        t2[1]["morton"] = layout.morton(1, 0)
        t2[1]["cls"] = 3
        t2[1]["mv_dx"] = 8
        ref_r2 = regions.regions_of(t2, W, H, 0)
        self.assertEqual(len(ref_r2), 2)
        with kernels.mutant_scope("MERGE_DISTINCT_MOV_GROUPS"):
            mut_r2 = kernels.regions_of(t2, W, H, 0)
        self.assertNotEqual(len(mut_r2), len(ref_r2))

    # ========================================================================
    # Fallback, native_parity_s12, and dual_verify across golden streams
    # ========================================================================

    def test_automatic_python_fallback_when_native_absent(self):
        rng = np.random.default_rng(777)
        f0 = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
        f1 = np.roll(f0, shift=(2, -3, 0), axis=(0, 1, 2))
        nat_pair = cu_regions.pair_regions_plus(f0, f1)
        with kernels.fallback_scope():
            self.assertFalse(kernels.is_native_available())
            py_pair = cu_regions.pair_regions_plus(f0, f1)
            rep = native_parity_s12.parity(kernels, sizes=[(16, 16), (63, 45)])
            self.assertEqual(rep["status"], "OK")
        self.assertEqual(canon.dumps(nat_pair), canon.dumps(py_pair))

    def test_native_parity_s12_and_dual_verify_goldens(self):
        rep = native_parity_s12.parity(self.mod, sizes=[(16, 16), (63, 45), (320, 180)])
        self.assertEqual(rep["status"], "OK", rep)

        for sc, sha in sorted(BASE_E_SHA.items()):
            res = kernels.run_dual_verify(source(sc))
            self.assertEqual(blob_sha(res), sha, sc)

    # ========================================================================
    # 1280x900 Benchmark (Python reference vs Rust visioncore_native)
    # ========================================================================

    def test_benchmark_1280x900_all_four_briefs(self):
        W, H = 1280, 900
        g = vgrid.Grid(W, H)
        s, gw, gh = g.s, g.gw, g.gh
        rng = np.random.default_rng(9001280)
        rgb0 = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
        rgb1 = np.roll(rgb0, shift=(4, -6, 0), axis=(0, 1, 2))
        pad0 = g.pad(rgb0)
        pad1 = g.pad(rgb1)
        y0 = desc.luma_of_padded(pad0)
        y1 = desc.luma_of_padded(pad1)
        tx = g.tx
        ty = g.ty
        idx = np.arange(g.N, dtype=np.int64)
        out_py = np.zeros(g.N, dtype=desc.TILE_DTYPE)
        out_rs = np.zeros(g.N, dtype=desc.TILE_DTYPE)

        # Prepare S4 classified tiles on 1280x900
        t0, _ = desc.describe_padded(pad0, g, y_plane=y0, lazy_xh=True)
        st = change.ClassState.init(t0)
        change.classify_first(st, t0, y_cur=y0)
        t1, eq = desc.describe_padded(pad1, g, words_prev=pad0, prev_tiles=t0, y_plane=y1, lazy_xh=True)
        classified, _, _ = change.classify_tiles(st, t1, eq, y_cur=y1, grid=(s, gw, gh))

        def bench(fn, iters=15):
            fn()  # warmup
            samples = []
            for _ in range(iters):
                t_a = time.perf_counter_ns()
                fn()
                samples.append(time.perf_counter_ns() - t_a)
            samples.sort()
            return samples[len(samples) // 2] / 1e6  # median ms

        b1_py = bench(lambda: change._tile_sad_windows(y1, y0, (4, -6), s, tx, ty))
        b1_rs = bench(lambda: self.mod._tile_sad_windows(y1, y0, (4, -6), s, tx, ty))

        b2_py = bench(lambda: change.global_vector(y1, y0, s))
        b2_rs = bench(lambda: self.mod.global_vector(y1, y0, s))

        b3_py = bench(lambda: (desc.luma_of_padded(pad1), desc._compute(out_py, idx, pad1, g, desc.PARAMS, y1)))
        b3_rs = bench(lambda: (self.mod.luma_of_padded(pad1), self.mod._compute(out_rs, idx, pad1, g, desc.PARAMS, y1)))

        b4_py = bench(lambda: regions.adjacency(regions.regions_of(classified, W, H, 1)))
        b4_rs = bench(lambda: self.mod.adjacency(self.mod.regions_of(classified, W, H, 1)))

        print(
            f"\n[BENCHMARK 1280x900 s={s} N={g.N}] "
            f"B1_sad_windows: py={b1_py:.3f}ms native={b1_rs:.3f}ms ({b1_py / max(b1_rs, 1e-6):.2f}x) | "
            f"B2_global_vector: py={b2_py:.3f}ms native={b2_rs:.3f}ms ({b2_py / max(b2_rs, 1e-6):.2f}x) | "
            f"B3_s1_luma_compute: py={b3_py:.3f}ms native={b3_rs:.3f}ms ({b3_py / max(b3_rs, 1e-6):.2f}x) | "
            f"B4_s4_regions_adj: py={b4_py:.3f}ms native={b4_rs:.3f}ms ({b4_py / max(b4_rs, 1e-6):.2f}x)"
        )
        self.assertGreater(b1_py, 0.0)
        self.assertGreater(b1_rs, 0.0)


if __name__ == "__main__":
    unittest.main()
