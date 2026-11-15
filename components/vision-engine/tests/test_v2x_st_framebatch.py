"""Tests for frame-batched subtile motion adapters st_proto8_fb and st_block4_fb."""

import importlib
import os
from pathlib import Path
import subprocess
import time
import unittest
import numpy as np

from eval.v2proto_subtile import subtile_motion as proto8_py
from eval.v2x.st_block4 import subtile_motion as block4_py
from visioncore.layout import grid_params


def _make_seeded_pair(W: int, H: int, seed: int, max_d: int):
    rng = np.random.RandomState(seed)
    bg = 200
    f1 = np.full((H, W), bg, dtype=np.uint8)
    f2 = np.full((H, W), bg, dtype=np.uint8)

    s_grid, gw, gh = grid_params(W, H)

    # Add deterministic non-moving texture on far border tiles if large enough
    if gw >= 8 and gh >= 6:
        coords = [
            (1, 1, 3, 2),
            (gw // 2, gh // 2, -4, 3),
            (gw - 2, gh - 2, 5, -2),
        ]
    else:
        coords = [(1, 1, 3, 2)]

    for tx, ty, dx, dy in coords:
        dx = max(-max_d, min(max_d, dx))
        dy = max(-max_d, min(max_d, dy))
        x0 = tx * s_grid + 2
        y0 = ty * s_grid + 2
        pw = min(8, max(4, s_grid - 4 - abs(dx)))
        ph = min(8, max(4, s_grid - 4 - abs(dy)))
        patch = rng.randint(10, 90, size=(ph, pw), dtype=np.uint8)
        f1[y0 : y0 + ph, x0 : x0 + pw] = patch
        f2[y0 + dy : y0 + dy + ph, x0 + dx : x0 + dx + pw] = patch

    return f1, f2, s_grid, gw, gh


def _make_1080p_shifted_pair(seed: int = 1080, max_d: int = 8):
    W, H = 1920, 1080
    s_grid, gw, gh = grid_params(W, H)
    rng = np.random.RandomState(seed)
    bg = 200
    f1 = np.full((H, W), bg, dtype=np.uint8)
    f2 = np.full((H, W), bg, dtype=np.uint8)

    patches = [
        (40, 40, 16, 16, 4, 2),
        (520, 330, 20, 16, -5, 3),
        (1220, 710, 16, 16, 6, 0),
        (1800, 970, 16, 16, -3, -4),
    ]
    for x0, y0, pw, ph, dx, dy in patches:
        patch = rng.randint(20, 100, size=(ph, pw), dtype=np.uint8)
        f1[y0 : y0 + ph, x0 : x0 + pw] = patch
        f2[y0 + dy : y0 + dy + ph, x0 + dx : x0 + dx + pw] = patch

    return f1, f2, s_grid, gw, gh, max_d


class TestV2XStFramebatch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        crates = [
            ("VQA_K_ST_PROTO8_BIN", "vqa_k_st_proto8", "vqa_k_st_proto8"),
            ("VQA_K_ST_BLOCK4_BIN", "vqa_k_st_block4", "vqa_k_st_block4"),
        ]
        for env_var, crate_name, bin_name in crates:
            crate_dir = cls.repo_root / "rust" / crate_name
            target_dir = crate_dir / "target"
            bin_path = os.environ.get(env_var) or str(target_dir / "release" / bin_name)
            if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
                subprocess.run(
                    [
                        "cargo",
                        "build",
                        "--release",
                        "--offline",
                        "--manifest-path",
                        str(crate_dir / "Cargo.toml"),
                        "--target-dir",
                        str(target_dir),
                    ],
                    cwd=str(cls.repo_root),
                    check=True,
                )

        import eval.v2x.st_proto8_fb as proto8_fb
        import eval.v2x.st_block4_fb as block4_fb

        cls.proto8_fb = proto8_fb
        cls.block4_fb = block4_fb

    def test_exact_equality_fixed_seed_pairs(self):
        """Assert exact equality with v2proto_subtile.py and st_block4.py for every tile of 64x48, 63x45, 320x180 and 1920x1080 pairs."""
        small_configs = [
            (64, 48, 101, 8),
            (63, 45, 202, 8),
            (320, 180, 303, 12),
        ]

        for W, H, seed, max_d in small_configs:
            with self.subTest(resolution=f"{W}x{H}"):
                f1, f2, s, gw, gh = _make_seeded_pair(W, H, seed, max_d)
                p8_worker = self.proto8_fb.get_worker()
                b4_worker = self.block4_fb.get_worker()
                p8_before = p8_worker.batch_count
                b4_before = b4_worker.batch_count

                p8_non_none = 0
                b4_non_none = 0
                for ty in range(gh):
                    for tx in range(gw):
                        xy = (tx, ty)
                        exp_p8 = proto8_py(f1, f2, s, xy, max_d)
                        got_p8 = self.proto8_fb.subtile_motion(f1, f2, s, xy, max_d)
                        self.assertEqual(
                            got_p8,
                            exp_p8,
                            f"st_proto8_fb mismatch at {W}x{H} tile {xy}",
                        )
                        if got_p8 is not None:
                            p8_non_none += 1

                        exp_b4 = block4_py(f1, f2, s, xy, max_d)
                        got_b4 = self.block4_fb.subtile_motion(f1, f2, s, xy, max_d)
                        self.assertEqual(
                            got_b4,
                            exp_b4,
                            f"st_block4_fb mismatch at {W}x{H} tile {xy}",
                        )
                        if got_b4 is not None:
                            b4_non_none += 1

                self.assertGreater(p8_non_none, 0)
                self.assertGreater(b4_non_none, 0)
                self.assertEqual(p8_worker.batch_count - p8_before, 1)
                self.assertEqual(b4_worker.batch_count - b4_before, 1)

        # 1920x1080 shifted pair across every single tile (60 x 34 = 2040 tiles)
        f1_1080, f2_1080, s_1080, gw_1080, gh_1080, max_d_1080 = _make_1080p_shifted_pair()
        p8_worker = self.proto8_fb.get_worker()
        b4_worker = self.block4_fb.get_worker()
        p8_before = p8_worker.batch_count
        b4_before = b4_worker.batch_count

        p8_non_none = 0
        b4_non_none = 0
        for ty in range(gh_1080):
            for tx in range(gw_1080):
                xy = (tx, ty)
                exp_p8 = proto8_py(f1_1080, f2_1080, s_1080, xy, max_d_1080)
                got_p8 = self.proto8_fb.subtile_motion(f1_1080, f2_1080, s_1080, xy, max_d_1080)
                self.assertEqual(got_p8, exp_p8, f"st_proto8_fb 1080p mismatch at tile {xy}")
                if got_p8 is not None:
                    p8_non_none += 1

                exp_b4 = block4_py(f1_1080, f2_1080, s_1080, xy, max_d_1080)
                got_b4 = self.block4_fb.subtile_motion(f1_1080, f2_1080, s_1080, xy, max_d_1080)
                self.assertEqual(got_b4, exp_b4, f"st_block4_fb 1080p mismatch at tile {xy}")
                if got_b4 is not None:
                    b4_non_none += 1

        self.assertGreater(p8_non_none, 0)
        self.assertGreater(b4_non_none, 0)
        self.assertEqual(p8_worker.batch_count - p8_before, 1)
        self.assertEqual(b4_worker.batch_count - b4_before, 1)

    def test_cache_invalidation_new_objects_in_place_and_max_d(self):
        """Assert cache invalidation on new objects with equal content, in-place modification, and different max_d."""
        for name, fb_mod, ref_fn in (
            ("proto8", self.proto8_fb, proto8_py),
            ("block4", self.block4_fb, block4_py),
        ):
            with self.subTest(variant=name):
                W, H, s = 64, 48, 16
                f1 = np.full((H, W), 200, dtype=np.uint8)
                f2 = np.full((H, W), 200, dtype=np.uint8)
                rng = np.random.RandomState(77)
                patch = rng.randint(20, 100, size=(8, 8), dtype=np.uint8)
                f1[20:28, 18:26] = patch
                f2[20:28, 24:32] = patch

                worker = fb_mod.get_worker()
                b0 = worker.batch_count

                res1 = fb_mod.subtile_motion(f1, f2, s, (1, 1), max_d=10)
                self.assertEqual(res1, ref_fn(f1, f2, s, (1, 1), max_d=10))
                self.assertIsNotNone(res1)
                self.assertEqual(res1[:2], (6, 0))
                self.assertEqual(worker.batch_count, b0 + 1)

                # Subsequent call on same objects and params hits cache
                res1_cached = fb_mod.subtile_motion(f1, f2, s, (0, 0), max_d=10)
                self.assertIsNone(res1_cached)
                self.assertEqual(worker.batch_count, b0 + 1)

                # 1. Cache invalidation on new objects with equal content
                f1_copy = f1.copy()
                f2_copy = f2.copy()
                self.assertTrue(np.array_equal(f1, f1_copy))
                self.assertTrue(np.array_equal(f2, f2_copy))
                res_copy = fb_mod.subtile_motion(f1_copy, f2_copy, s, (1, 1), max_d=10)
                self.assertEqual(res_copy, res1)
                self.assertEqual(worker.batch_count, b0 + 2)

                # 2. Cache invalidation on a different max_d (max_d=4 < dx=6 -> must return None)
                res_small_d = fb_mod.subtile_motion(f1_copy, f2_copy, s, (1, 1), max_d=4)
                self.assertIsNone(res_small_d)
                self.assertEqual(res_small_d, ref_fn(f1_copy, f2_copy, s, (1, 1), max_d=4))
                self.assertEqual(worker.batch_count, b0 + 3)

                # Switch back to max_d=10 on same objects -> invalidates cache and finds (6, 0) again
                res_restored_d = fb_mod.subtile_motion(f1_copy, f2_copy, s, (1, 1), max_d=10)
                self.assertEqual(res_restored_d, res1)
                self.assertEqual(worker.batch_count, b0 + 4)

                # 3. Cache invalidation on in-place modification of cur_luma
                f2_copy[:] = 200
                f2_copy[22:30, 18:26] = patch  # now moved by (0, 2) instead of (6, 0)
                res_inplace = fb_mod.subtile_motion(f1_copy, f2_copy, s, (1, 1), max_d=10)
                self.assertEqual(res_inplace, ref_fn(f1_copy, f2_copy, s, (1, 1), max_d=10))
                self.assertIsNotNone(res_inplace)
                self.assertEqual(res_inplace[:2], (0, 2))
                self.assertEqual(worker.batch_count, b0 + 5)

                # In-place modification to identical frame -> returns None
                f2_copy[:] = f1_copy
                res_zero = fb_mod.subtile_motion(f1_copy, f2_copy, s, (1, 1), max_d=10)
                self.assertIsNone(res_zero)
                self.assertEqual(worker.batch_count, b0 + 6)

    def test_bad_env_path_raises_importerror(self):
        """Assert ImportError on a bad env path for both VQA_K_ST_PROTO8_BIN and VQA_K_ST_BLOCK4_BIN."""
        cases = [
            ("VQA_K_ST_PROTO8_BIN", self.proto8_fb),
            ("VQA_K_ST_BLOCK4_BIN", self.block4_fb),
        ]
        for env_var, mod in cases:
            with self.subTest(env_var=env_var):
                old_val = os.environ.get(env_var)
                try:
                    os.environ[env_var] = "/nonexistent/bad/path/to/binary"
                    with self.assertRaises(ImportError):
                        mod._find_binary()
                    with self.assertRaises(ImportError):
                        mod.get_worker()
                    with self.assertRaises(ImportError):
                        importlib.reload(mod)
                finally:
                    if old_val is None:
                        os.environ.pop(env_var, None)
                    else:
                        os.environ[env_var] = old_val
                    mod._cleanup()
                    importlib.reload(mod)

    def test_1080p_timing_info_vm(self):
        """Print VM ms per 1080p frame for _rs vs _fb as information only."""
        import eval.v2x.st_proto8_rs as proto8_rs
        import eval.v2x.st_block4_rs as block4_rs

        f1, f2, s, gw, gh, max_d = _make_1080p_shifted_pair()
        tiles = [(tx, ty) for ty in range(gh) for tx in range(gw)]

        # Warm up workers
        self.proto8_fb.subtile_motion(f1, f2, s, (0, 0), max_d)
        self.block4_fb.subtile_motion(f1, f2, s, (0, 0), max_d)
        proto8_rs.subtile_motion(f1, f2, s, (0, 0), max_d)
        block4_rs.subtile_motion(f1, f2, s, (0, 0), max_d)

        # Fresh copies so _fb measures a full cold-cache frame batch + all 2040 tile lookups
        f1_p8 = f1.copy()
        f2_p8 = f2.copy()
        t0 = time.perf_counter()
        for xy in tiles:
            self.proto8_fb.subtile_motion(f1_p8, f2_p8, s, xy, max_d)
        fb_p8_ms = (time.perf_counter() - t0) * 1000.0

        f1_b4 = f1.copy()
        f2_b4 = f2.copy()
        t0 = time.perf_counter()
        for xy in tiles:
            self.block4_fb.subtile_motion(f1_b4, f2_b4, s, xy, max_d)
        fb_b4_ms = (time.perf_counter() - t0) * 1000.0

        # Measure _rs per-tile cost over full frame
        t0 = time.perf_counter()
        for xy in tiles:
            proto8_rs.subtile_motion(f1, f2, s, xy, max_d)
        rs_p8_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        for xy in tiles:
            block4_rs.subtile_motion(f1, f2, s, xy, max_d)
        rs_b4_ms = (time.perf_counter() - t0) * 1000.0

        print(
            f"\n[VM] 1080p frame ({len(tiles)} tiles) timing (VM diagnostic only, not host): "
            f"st_proto8_rs={rs_p8_ms:.2f} ms vs st_proto8_fb={fb_p8_ms:.2f} ms; "
            f"st_block4_rs={rs_b4_ms:.2f} ms vs st_block4_fb={fb_b4_ms:.2f} ms",
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
