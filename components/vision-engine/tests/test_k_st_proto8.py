"""Integration and conformance tests for vqa_k_st_proto8 Rust binary and adapter."""

import os
from pathlib import Path
import unittest
import numpy as np

from eval.v2proto_subtile import subtile_motion as python_subtile_motion


class TestKStProto8(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.bin_path = os.environ.get("VQA_K_ST_PROTO8_BIN")
        if not cls.bin_path:
            cls.bin_path = str(cls.repo_root / "rust" / "vqa_k_st_proto8" / "target" / "release" / "vqa_k_st_proto8")

        if not (os.path.isfile(cls.bin_path) and os.access(cls.bin_path, os.X_OK)):
            cls.fail(cls, f"Required release binary missing at '{cls.bin_path}'. Build it first.")

    def test_missing_binary_raises(self):
        """Must fail / raise ImportError when binary is absent (never skip)."""
        from eval.v2x.st_proto8_rs import _find_binary
        old_env = os.environ.get("VQA_K_ST_PROTO8_BIN")
        try:
            os.environ["VQA_K_ST_PROTO8_BIN"] = "/nonexistent/path/vqa_k_st_proto8"
            with self.assertRaises(ImportError):
                _find_binary()
        finally:
            if old_env is None:
                os.environ.pop("VQA_K_ST_PROTO8_BIN", None)
            else:
                os.environ["VQA_K_ST_PROTO8_BIN"] = old_env

    def test_adapter_matches_python_reference_ram_fixtures(self):
        """Generate fixtures in RAM with fixed numpy seeds and assert byte-equal outputs."""
        from eval.v2x.st_proto8_rs import subtile_motion as rs_subtile_motion

        # Test configurations: (W, H, s, max_d, seed)
        configs = [
            # Sizes not multiple of tile size
            (35, 41, 16, 10, 101),
            (100, 75, 32, 15, 102),
            # Frame borders and tile size 16 & 32
            (128, 128, 16, 15, 103),
            (128, 128, 32, 15, 104),
            # Non-square sizes
            (320, 180, 16, 15, 105),
            (320, 180, 32, 15, 106),
            # Full 1080p case
            (1920, 1080, 16, 15, 107),
        ]

        for W, H, s, max_d, seed in configs:
            rng = np.random.RandomState(seed)
            bg = 200
            fg = 50

            f1 = np.full((H, W), bg, dtype=np.uint8)
            f2 = np.full((H, W), bg, dtype=np.uint8)

            # Add random background noise
            noise_mask = rng.rand(H, W) < 0.05
            f1[noise_mask] = rng.randint(0, 256, size=int(np.sum(noise_mask)))
            f2[noise_mask] = f1[noise_mask]

            # Place a moving element near border or inside tile
            obj_w = min(20, W - 4)
            obj_h = min(20, H - 4)
            x0 = rng.randint(0, max(1, W - obj_w))
            y0 = rng.randint(0, max(1, H - obj_h))
            dx = rng.randint(-max_d, max_d + 1)
            dy = rng.randint(-max_d, max_d + 1)

            f1[y0:y0 + obj_h, x0:x0 + obj_w] = fg

            tx0 = x0 + dx
            ty0 = y0 + dy
            # Clip moved object within frame
            for r in range(obj_h):
                for c in range(obj_w):
                    px = tx0 + c
                    py = ty0 + r
                    if 0 <= px < W and 0 <= py < H:
                        f2[py, px] = fg

            # Evaluate grid tiles
            gw = (W + s - 1) // s
            gh = (H + h_step - 1) // s if 'h_step' in locals() else (H + s - 1) // s

            # Test multiple tile coordinates per frame
            for tx in range(min(gw, 4)):
                for ty in range(min(gh, 4)):
                    py_res = python_subtile_motion(f1, f2, s=s, tile_xy=(tx, ty), max_d=max_d)
                    rs_res = rs_subtile_motion(f1, f2, s=s, tile_xy=(tx, ty), max_d=max_d)

                    self.assertEqual(
                        py_res, rs_res,
                        f"Mismatch at config ({W}x{H}, s={s}, max_d={max_d}), tile=({tx}, {ty})"
                    )

    def test_edge_cases_conformance(self):
        """Hand-checked edge cases comparing adapter to Python reference."""
        from eval.v2x.st_proto8_rs import subtile_motion as rs_subtile_motion

        W, H, s, max_d = 128, 128, 32, 16
        bg, fg = 200, 50

        # Case 1: Caret moving right 12 px
        f1 = np.full((H, W), bg, dtype=np.uint8)
        f2 = np.full((H, W), bg, dtype=np.uint8)
        f1[8:24, 8:10] = fg
        f2[8:24, 20:22] = fg

        py_res = python_subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=max_d)
        rs_res = rs_subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=max_d)
        self.assertEqual(py_res, (12, 0, py_res[2]))
        self.assertEqual(py_res, rs_res)

        # Case 2: Shape change without translation -> None
        f1 = np.full((H, W), bg, dtype=np.uint8)
        f2 = np.full((H, W), bg, dtype=np.uint8)
        f1[8:24, 8:24] = fg
        f2[8:24, 8:24] = fg
        f2[12:20, 12:20] = 100

        py_res = python_subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=max_d)
        rs_res = rs_subtile_motion(f1, f2, s=s, tile_xy=(0, 0), max_d=max_d)
        self.assertIsNone(py_res)
        self.assertEqual(py_res, rs_res)


if __name__ == "__main__":
    unittest.main()
