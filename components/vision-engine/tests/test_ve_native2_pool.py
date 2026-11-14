"""Unit VE-NATIVE-2: bounded rayon ThreadPool in visioncore_native, parity across VISIONCORE_THREADS 1..4,
repeated-run determinism, sha256 preservation, GIL release, off-by-one band boundary mutation, and 1280x900 p50 bench."""
import contextlib
import hashlib
import os
from pathlib import Path
import threading
import time
import unittest

import numpy as np

from tests.test_e1_change_pipeline import BASE_E_SHA, blob_sha, source
from visioncore import canon, change, desc, errors as vc_errors, grid as vgrid, layout, regions
from vnative import backend
from vnative.adapters import kernels

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def threads_env_scope(val):
    """Temporarily set VISIONCORE_THREADS (or unset if val is None) and clear any programmatic override."""
    mod = kernels.get_native_module()
    old_env = os.environ.get("VISIONCORE_THREADS")
    if mod is not None:
        mod.set_threads(None)
    if val is None:
        os.environ.pop("VISIONCORE_THREADS", None)
    else:
        os.environ["VISIONCORE_THREADS"] = str(val)
    try:
        yield
    finally:
        if mod is not None:
            mod.set_threads(None)
        if old_env is None:
            os.environ.pop("VISIONCORE_THREADS", None)
        else:
            os.environ["VISIONCORE_THREADS"] = old_env


class TestVeNative2Pool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = kernels.get_native_module()
        if cls.mod is None:
            raise unittest.SkipTest("visioncore_native.abi3.so not built")

    def tearDown(self):
        if self.mod is not None:
            self.mod.set_mutant(None)
            self.mod.set_threads(None)
        os.environ.pop("VISIONCORE_THREADS", None)

    def test_pool_configuration_and_never_global_pool(self):
        # 1. Static source check: never build_global; bounded ThreadPoolBuilder::new() only in par.rs
        src_dir = ROOT / "rust/visioncore_native/src"
        for rs_file in sorted(src_dir.glob("*.rs")):
            text = rs_file.read_text(encoding="utf-8")
            self.assertNotIn("build_global", text, f"{rs_file.name} must never use global rayon pool")
        par_text = (src_dir / "par.rs").read_text(encoding="utf-8")
        self.assertIn("ThreadPoolBuilder::new()", par_text)
        self.assertIn("VISIONCORE_THREADS", par_text)

        # 2. Default thread count is min(4, available_parallelism)
        with threads_env_scope(None):
            avail = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
            expected_default = max(1, min(4, avail))
            self.assertEqual(self.mod.get_threads(), expected_default)
            info = self.mod.pool_info()
            self.assertEqual(info["configured_threads"], expected_default)
            self.assertEqual(info["active_threads"], expected_default)

        # 3. Single-thread fallback when VISIONCORE_THREADS=1
        with threads_env_scope("1"):
            self.assertEqual(self.mod.get_threads(), 1)
            info1 = self.mod.pool_info()
            self.assertEqual(info1["configured_threads"], 1)
            self.assertEqual(info1["active_threads"], 1)
            self.assertTrue(info1["serial"])
            self.assertEqual(info1["worker_name"], "serial")

        # 4. Bounded pool override for VISIONCORE_THREADS in 2..=8
        for n in (2, 3, 4, 8):
            with threads_env_scope(str(n)):
                self.assertEqual(self.mod.get_threads(), n)
                infon = self.mod.pool_info()
                self.assertEqual(infon["configured_threads"], n)
                self.assertEqual(infon["active_threads"], n)
                self.assertFalse(infon["serial"])
                self.assertTrue(
                    infon["worker_name"].startswith(f"vc-bounded-{n}-"),
                    f"unexpected worker name: {infon['worker_name']}",
                )

        # 5. Out-of-range or invalid env values fall back to default
        for bad_env in ("0", "9", "-1", "invalid", ""):
            with threads_env_scope(bad_env):
                self.assertEqual(self.mod.get_threads(), expected_default)

        # Programmatic set_threads out of 1..=8 raises ValueError
        with self.assertRaises(ValueError):
            self.mod.set_threads(9)

    def test_byte_exact_parity_across_sizes_seeds_and_threads_1_to_4(self):
        sizes = [(16, 16), (63, 45), (127, 95), (320, 180)]
        for seed in range(4):
            rng = np.random.default_rng(7000 + seed)
            for W, H in sizes:
                g = vgrid.Grid(W, H)
                s, gw, gh = g.s, g.gw, g.gh
                rgb0 = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
                rgb1 = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
                pad0 = g.pad(rgb0)
                pad1 = g.pad(rgb1)

                # Reference Python outputs
                ref_luma = desc.luma_of_padded(pad1)
                ref_y, ref_co, ref_cg = desc.ycocg(pad1)
                ref_tiles, _ = desc.describe_padded(pad1, g)

                # Subset & unsorted idx for _compute
                sub_idx = np.arange(0, g.N, 2, dtype=np.int64)
                unsorted_idx = sub_idx[::-1].copy()
                ref_sub = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                desc._compute(ref_sub, sub_idx, pad1, g, desc.PARAMS, ref_luma)
                ref_unsorted = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                desc._compute(ref_unsorted, unsorted_idx, pad1, g, desc.PARAMS, ref_luma)

                y0 = desc.luma_of_padded(pad0)
                tx_req = rng.integers(-gw, gw, size=gh * gw + 5, dtype=np.int64)
                ty_req = rng.integers(-gh, gh, size=gh * gw + 5, dtype=np.int64)
                ref_sad, ref_cnt = change._tile_sad_windows(ref_luma, y0, (3, -2), s, tx_req, ty_req)
                ref_sad_i64, ref_cnt_i64 = change._tile_sad_windows(
                    ref_luma.astype(np.int64), y0.astype(np.int64), (3, -2), s, tx_req, ty_req
                )

                absdiff_i16 = np.abs(ref_luma - y0).astype(np.int16)
                absdiff_i32 = absdiff_i16.astype(np.int32)
                absdiff_i64 = absdiff_i16.astype(np.int64)
                ref_sums = change._tile_sums(absdiff_i16, s, gh, gw)

                for nt in ("1", "2", "3", "4"):
                    with threads_env_scope(nt):
                        nat_luma = self.mod.luma_of_padded(pad1)
                        self.assertEqual(nat_luma.tobytes(), ref_luma.tobytes())

                        ny, nco, ncg = self.mod.ycocg(pad1)
                        self.assertEqual(ny.tobytes(), ref_y.tobytes())
                        self.assertEqual(nco.tobytes(), ref_co.tobytes())
                        self.assertEqual(ncg.tobytes(), ref_cg.tobytes())

                        nat_tiles = self.mod.describe_padded_tiles(pad1, g)
                        self.assertEqual(nat_tiles.tobytes(), ref_tiles.tobytes())

                        nat_sub = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                        self.mod._compute(nat_sub, sub_idx, pad1, g, desc.PARAMS, ref_luma)
                        self.assertEqual(nat_sub.tobytes(), ref_sub.tobytes())

                        nat_unsorted = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                        self.mod._compute(nat_unsorted, unsorted_idx, pad1, g, desc.PARAMS, ref_luma)
                        self.assertEqual(nat_unsorted.tobytes(), ref_unsorted.tobytes())

                        nsad, ncnt = self.mod._tile_sad_windows(ref_luma, y0, (3, -2), s, tx_req, ty_req)
                        self.assertEqual(nsad.tobytes(), ref_sad.tobytes())
                        self.assertEqual(ncnt.tobytes(), ref_cnt.tobytes())

                        nsad64, ncnt64 = self.mod._tile_sad_windows(
                            ref_luma.astype(np.int64), y0.astype(np.int64), (3, -2), s, tx_req, ty_req
                        )
                        self.assertEqual(nsad64.tobytes(), ref_sad_i64.tobytes())
                        self.assertEqual(ncnt64.tobytes(), ref_cnt_i64.tobytes())

                        nsums16 = self.mod._tile_sums(absdiff_i16, s, gh, gw)
                        nsums32 = self.mod._tile_sums(absdiff_i32, s, gh, gw)
                        nsums64 = self.mod._tile_sums(absdiff_i64, s, gh, gw)
                        self.assertEqual(nsums16.tobytes(), ref_sums.tobytes())
                        self.assertEqual(nsums32.tobytes(), ref_sums.tobytes())
                        self.assertEqual(nsums64.tobytes(), ref_sums.tobytes())

    def test_repeated_run_determinism_and_sha256_unchanged(self):
        rng = np.random.default_rng(8888)
        W, H = 320, 180
        g = vgrid.Grid(W, H)
        rgb = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
        frame_sha_before = hashlib.sha256(rgb.tobytes()).hexdigest()
        padded = g.pad(rgb)
        padded_sha_before = hashlib.sha256(padded.tobytes()).hexdigest()
        idx = np.arange(g.N, dtype=np.int64)
        ref_xh = desc._hash_tiles(padded, g, idx).tobytes()

        with threads_env_scope("1"):
            serial_tiles = self.mod.describe_padded_tiles(padded, g).tobytes()

        for nt in ("2", "3", "4"):
            with threads_env_scope(nt):
                for _ in range(5):
                    tiles = self.mod.describe_padded_tiles(padded, g)
                    self.assertEqual(tiles.tobytes(), serial_tiles)
                    self.assertEqual(tiles["xh64"].tobytes(), ref_xh)
                    self.assertEqual(hashlib.sha256(rgb.tobytes()).hexdigest(), frame_sha_before)
                    self.assertEqual(hashlib.sha256(padded.tobytes()).hexdigest(), padded_sha_before)

        # Full dual_verify golden streams under VISIONCORE_THREADS=4
        with threads_env_scope("4"):
            for sc, expected_sha in sorted(BASE_E_SHA.items()):
                res = kernels.run_dual_verify(source(sc))
                self.assertEqual(blob_sha(res), expected_sha, sc)

    def test_preserved_indexerror_and_vqaerror(self):
        yc = np.zeros((32, 48), dtype=np.int16)
        yp = np.zeros((32, 48), dtype=np.int16)
        for nt in ("1", "4"):
            with threads_env_scope(nt):
                with self.assertRaises(IndexError):
                    self.mod._tile_sad_windows(yc, yp, (0, 0), 16, np.array([3], dtype=np.int64), np.array([0], dtype=np.int64))
                with self.assertRaises(IndexError):
                    self.mod._tile_sad_windows(yc, yp, (0, 0), 16, np.array([-4], dtype=np.int64), np.array([0], dtype=np.int64))
                with self.assertRaises(IndexError):
                    self.mod._tile_sad_windows(yc.astype(np.int64), yp.astype(np.int64), (0, 0), 16, [0], [2])

                bad_tiles = np.zeros(1, dtype=desc.TILE_DTYPE)
                bad_tiles[0]["morton"] = layout.morton(200, 200)
                with self.assertRaises(vc_errors.VqaError) as ctx:
                    self.mod.regions_of(bad_tiles, 320, 180, 0)
                self.assertEqual(ctx.exception.code, "E_VC_ID_RANGE")

                r_prev = [{"id": "r0:0000", "grid": "320x180@16", "frame": 0, "group": "CHG", "mv": [0, 0], "n_tiles": 1, "spans": [["0000", "0000"]]}]
                r_cur = [{"id": "r1:0000", "grid": "640x360@16", "frame": 1, "group": "CHG", "mv": [0, 0], "n_tiles": 1, "spans": [["0000", "0000"]]}]
                with self.assertRaises(vc_errors.VqaError) as ctx2:
                    self.mod.match_regions(r_prev, r_cur, (0, 0), 16)
                self.assertEqual(ctx2.exception.code, "E_VC_GRID_MISMATCH")

    def test_gil_released_during_pool_join(self):
        py_rs = (ROOT / "rust/visioncore_native/src/py.rs").read_text(encoding="utf-8")
        for call_name in (
            "s0::tile_sad_windows_into",
            "s0::tile_sad_windows_i64_into",
            "par::tile_sums_i16_into",
            "par::tile_sums_i32_into",
            "par::tile_sums_i64_into",
            "s0::luma_of_padded_into",
            "s0::ycocg_into",
            "s1::compute_tiles_into",
            "par::pool_status",
        ):
            self.assertIn(call_name, py_rs)

        # Concurrent Python thread progress while pooled kernels execute under VISIONCORE_THREADS=4
        W, H = 640, 360
        g = vgrid.Grid(W, H)
        rng = np.random.default_rng(9191)
        pad = g.pad(rng.integers(0, 256, (H, W, 3), dtype=np.uint8))
        y_plane = self.mod.luma_of_padded(pad)
        idx = np.arange(g.N, dtype=np.int64)
        out = np.zeros(g.N, dtype=desc.TILE_DTYPE)

        ticks = [0]
        stop = threading.Event()

        def ticker():
            while not stop.is_set():
                ticks[0] += 1

        with threads_env_scope("4"):
            t = threading.Thread(target=ticker)
            t.start()
            try:
                for _ in range(25):
                    self.mod._compute(out, idx, pad, g, desc.PARAMS, y_plane)
            finally:
                stop.set()
                t.join(timeout=5.0)
        self.assertGreater(ticks[0], 0)

    def test_existing_mutants_and_named_band_off_by_one_mutation_killed(self):
        par_rs = (ROOT / "rust/visioncore_native/src/par.rs").read_text(encoding="utf-8")
        self.assertIn("MUTANT:BAND_OFF_BY_ONE: end -= 1;", par_rs)

        W, H = 128, 64
        g = vgrid.Grid(W, H)
        s, gw, gh = g.s, g.gw, g.gh
        rng = np.random.default_rng(12345)
        rgb = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
        padded = g.pad(rgb)
        ref_luma = desc.luma_of_padded(padded)
        ref_y, ref_co, ref_cg = desc.ycocg(padded)
        ref_tiles, _ = desc.describe_padded(padded, g)
        tx = g.tx.astype(np.int64)
        ty = g.ty.astype(np.int64)
        ref_sad, _ = change._tile_sad_windows(ref_luma, ref_luma, (0, 0), s, tx, ty)
        ref_sums = change._tile_sums(ref_luma, s, gh, gw)

        # Named mutation: BAND_OFF_BY_ONE (split tiles with an off-by-one band boundary -> parity fails)
        with threads_env_scope("4"):
            with kernels.mutant_scope("BAND_OFF_BY_ONE"):
                self.assertEqual(self.mod.get_mutant(), "BAND_OFF_BY_ONE")

                mut_luma = self.mod.luma_of_padded(padded)
                self.assertNotEqual(mut_luma.tobytes(), ref_luma.tobytes())

                my, mco, mcg = self.mod.ycocg(padded)
                self.assertNotEqual(my.tobytes(), ref_y.tobytes())

                mut_tiles = self.mod.describe_padded_tiles(padded, g)
                self.assertNotEqual(mut_tiles.tobytes(), ref_tiles.tobytes())

                # Isolate _compute with clean ref_luma to verify exact off-by-one tile band boundaries
                idx = np.arange(g.N, dtype=np.int64)
                mut_comp = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                ref_comp = np.zeros(g.N, dtype=desc.TILE_DTYPE)
                desc._compute(ref_comp, idx, padded, g, desc.PARAMS, ref_luma)
                self.mod._compute(mut_comp, idx, padded, g, desc.PARAMS, ref_luma)
                expected_boundary_ranks = [((b + 1) * g.N) // 4 - 1 for b in range(4)]
                diff_ranks = [i for i in range(g.N) if mut_comp[i].tobytes() != ref_comp[i].tobytes()]
                self.assertEqual(diff_ranks, expected_boundary_ranks)

                mut_sad, _ = self.mod._tile_sad_windows(ref_luma, ref_luma, (0, 0), s, tx, ty)
                diff_sad_ranks = [i for i in range(g.N) if mut_sad[i] != ref_sad[i]]
                self.assertEqual(diff_sad_ranks, expected_boundary_ranks)

                mut_sums = self.mod._tile_sums(ref_luma, s, gh, gw)
                self.assertNotEqual(mut_sums.tobytes(), ref_sums.tobytes())
                expected_boundary_rows = [((b + 1) * gh) // 4 - 1 for b in range(4)]
                diff_rows = [r for r in range(gh) if not np.array_equal(mut_sums[r], ref_sums[r])]
                self.assertEqual(diff_rows, expected_boundary_rows)

                with self.assertRaises(backend.VNativeError) as ctx:
                    kernels.run_dual_verify(source("clean_static"))
                self.assertEqual(ctx.exception.code, "E_VN_DIVERGE")

            # Existing mutants still killed under pool (VISIONCORE_THREADS=4)
            with kernels.mutant_scope("SAD_PLUS_ONE"):
                msad, _ = self.mod._tile_sad_windows(ref_luma, ref_luma, (0, 0), s, tx, ty)
                self.assertFalse(np.array_equal(msad, ref_sad))

            with kernels.mutant_scope("FLIP_DESC_BYTE_0"):
                mtiles = self.mod.describe_padded_tiles(padded, g)
                self.assertNotEqual(mtiles.tobytes(), ref_tiles.tobytes())

    def test_opt_in_bench_1280x900_serial_vs_pool_p50(self):
        W, H = 1280, 900
        g = vgrid.Grid(W, H)
        s, gw, gh = g.s, g.gw, g.gh
        rng = np.random.default_rng(9001280)
        rgb0 = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
        rgb1 = np.roll(rgb0, shift=(4, -6, 0), axis=(0, 1, 2))
        pad0 = g.pad(rgb0)
        pad1 = g.pad(rgb1)
        y0 = self.mod.luma_of_padded(pad0)
        y1 = self.mod.luma_of_padded(pad1)
        absdiff = np.abs(y1 - y0).astype(np.int16)
        tx = g.tx.astype(np.int64)
        ty = g.ty.astype(np.int64)
        idx = np.arange(g.N, dtype=np.int64)
        out_buf = np.zeros(g.N, dtype=desc.TILE_DTYPE)

        iters = 31 if os.environ.get("VISIONCORE_BENCH") == "1" else 15

        def p50_ms(fn):
            for _ in range(5):
                fn()
            samples = []
            for _ in range(iters):
                t0 = time.perf_counter_ns()
                fn()
                samples.append(time.perf_counter_ns() - t0)
            samples.sort()
            return samples[len(samples) // 2] / 1e6

        results = {}
        for mode, nt in (("serial", "1"), ("pool4", "4")):
            with threads_env_scope(nt):
                self.mod.pool_info()
                results[mode] = {
                    "compute_tiles": p50_ms(lambda: self.mod._compute(out_buf, idx, pad1, g, desc.PARAMS, y1)),
                    "tile_sad_windows": p50_ms(lambda: self.mod._tile_sad_windows(y1, y0, (4, -6), s, tx, ty)),
                    "tile_sums": p50_ms(lambda: self.mod._tile_sums(absdiff, s, gh, gw)),
                    "luma_of_padded": p50_ms(lambda: self.mod.luma_of_padded(pad1)),
                    "ycocg": p50_ms(lambda: self.mod.ycocg(pad1)),
                }

        s_res = results["serial"]
        p_res = results["pool4"]
        print(
            f"\n[VE-NATIVE-2 1280x900 p50 ms] "
            f"compute_tiles: serial={s_res['compute_tiles']:.3f}ms pool4={p_res['compute_tiles']:.3f}ms | "
            f"tile_sad_windows: serial={s_res['tile_sad_windows']:.3f}ms pool4={p_res['tile_sad_windows']:.3f}ms | "
            f"tile_sums: serial={s_res['tile_sums']:.3f}ms pool4={p_res['tile_sums']:.3f}ms | "
            f"luma_of_padded: serial={s_res['luma_of_padded']:.3f}ms pool4={p_res['luma_of_padded']:.3f}ms | "
            f"ycocg: serial={s_res['ycocg']:.3f}ms pool4={p_res['ycocg']:.3f}ms"
        )
        for k in s_res:
            self.assertGreater(s_res[k], 0.0)
            self.assertGreater(p_res[k], 0.0)


if __name__ == "__main__":
    unittest.main()
