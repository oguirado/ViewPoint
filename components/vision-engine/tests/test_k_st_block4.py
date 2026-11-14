"""Tests for Rust subtile_motion kernel vqa_k_st_block4 and its Python adapter."""

import os
import struct
import subprocess
import sys
import unittest
import numpy as np

from eval.v2x.st_block4 import subtile_motion as py_subtile
from visioncore.layout import grid_params

# Path to binary
DEFAULT_BIN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust",
    "vqa_k_st_block4",
    "target",
    "release",
    "vqa_k_st_block4",
)
BIN_PATH = os.environ.get("VQA_K_ST_BLOCK4_BIN", DEFAULT_BIN_PATH)


class TestKStBlock4(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(BIN_PATH) or not os.path.isfile(BIN_PATH):
            cls.fail(f"Required release binary missing at {BIN_PATH}")

        import eval.v2x.st_block4_rs as rs_mod
        cls.rs_subtile = staticmethod(rs_mod.subtile_motion)

    def test_missing_binary_raises_importerror(self):
        """Test that importing adapter with invalid env VQA_K_ST_BLOCK4_BIN raises ImportError."""
        orig_env = os.environ.get("VQA_K_ST_BLOCK4_BIN")
        try:
            os.environ["VQA_K_ST_BLOCK4_BIN"] = "/nonexistent/path/to/binary"
            sys.modules.pop("eval.v2x.st_block4_rs", None)
            with self.assertRaises(ImportError) as ctx:
                import eval.v2x.st_block4_rs
            self.assertIn("not found", str(ctx.exception))
        finally:
            if orig_env is not None:
                os.environ["VQA_K_ST_BLOCK4_BIN"] = orig_env
            else:
                os.environ.pop("VQA_K_ST_BLOCK4_BIN", None)
            sys.modules.pop("eval.v2x.st_block4_rs", None)

    def test_direct_binary_protocol(self):
        """Test the stdin/stdout binary protocol directly (op 1 & op 3)."""
        proc = subprocess.Popen(
            [BIN_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )

        W, H, s, max_d = 128, 128, 32, 16
        f1 = np.full((H, W), 200, dtype=np.uint8)
        f2 = np.full((H, W), 200, dtype=np.uint8)
        f1[40:56, 40:42] = 50
        f2[40:56, 52:54] = 50

        # Op 1 batch request
        tiles = [(1, 1), (0, 0)]
        req = bytearray()
        req.extend(b"\x01")
        req.extend(struct.pack("<iiiiI", W, H, s, max_d, len(tiles)))
        for tx, ty in tiles:
            req.extend(struct.pack("<ii", tx, ty))
        req.extend(struct.pack("<Q", W * H))
        req.extend(f1.tobytes())
        req.extend(f2.tobytes())

        proc.stdin.write(req)
        proc.stdin.flush()

        header = proc.stdout.read(8)
        status, n_out = struct.unpack("<iI", header)
        self.assertEqual(status, 0)
        self.assertEqual(n_out, 2)

        raw_res = proc.stdout.read(16 * n_out)
        dx0, dy0, nb0, v0 = struct.unpack_from("<iiii", raw_res, 0)
        dx1, dy1, nb1, v1 = struct.unpack_from("<iiii", raw_res, 16)

        self.assertEqual((dx0, dy0, v0), (12, 0, 1))
        self.assertEqual(v1, 0)

        # Op 3 bench request
        bench_req = bytearray()
        bench_req.extend(b"\x03")
        bench_req.extend(struct.pack("<iiiiI", W, H, s, max_d, len(tiles)))
        for tx, ty in tiles:
            bench_req.extend(struct.pack("<ii", tx, ty))
        bench_req.extend(struct.pack("<Q", W * H))
        bench_req.extend(f1.tobytes())
        bench_req.extend(f2.tobytes())
        bench_req.extend(struct.pack("<II", 2, 5))  # warmup=2, iters=5

        proc.stdin.write(bench_req)
        proc.stdin.flush()

        n_iters = struct.unpack("<I", proc.stdout.read(4))[0]
        self.assertEqual(n_iters, 5)
        raw_times = proc.stdout.read(8 * 5)
        times = struct.unpack("<5Q", raw_times)
        for t in times:
            self.assertGreaterEqual(t, 0)

        # Op 0 quit
        proc.stdin.write(b"\x00")
        proc.stdin.flush()
        proc.wait(timeout=1.0)

    def test_default_args(self):
        """Test that subtile_motion works with default arguments s=None, tile_xy=(0,0), max_d=16."""
        W, H = 128, 128
        f1 = np.full((H, W), 200, dtype=np.uint8)
        f2 = np.full((H, W), 200, dtype=np.uint8)
        f1[40:56, 40:42] = 50
        f2[40:56, 52:54] = 50

        res_py = py_subtile(f1, f2, s=None, tile_xy=(1, 1), max_d=16)
        res_rs = self.rs_subtile(f1, f2, s=None, tile_xy=(1, 1), max_d=16)
        self.assertEqual(res_py, res_rs)

    def test_adapter_vs_reference_cases(self):
        """Test Python adapter against Python reference across multiple RAM generated cases."""
        state = 0x5EED

        def next_u64():
            nonlocal state
            state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
            z = state
            z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
            z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
            return (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF

        configs = [
            (128, 128, 32, 16),
            (128, 122, 32, 16),
            (130, 130, 16, 12),
            (1920, 1080, 32, 16),
        ]

        for W, H, s, max_d in configs:
            _, gw, gh = grid_params(W, H)
            f1 = np.zeros((H, W), dtype=np.uint8)
            f2 = np.zeros((H, W), dtype=np.uint8)

            for y in range(H):
                for x in range(W):
                    val = next_u64() & 0xFF
                    f1[y, x] = val
                    f2[y, x] = val

            f1[0:8, 0:8] = 255
            f2[0:8, 2:10] = 255

            for _ in range(5):
                rx = next_u64() % (W - 16)
                ry = next_u64() % (H - 16)
                f2[ry:ry+8, rx:rx+8] = (next_u64() & 0xFF)

            test_tiles = [
                (0, 0),
                (gw - 1, 0),
                (0, gh - 1),
                (gw - 1, gh - 1),
                (gw // 2, gh // 2),
            ]

            for tile_xy in test_tiles:
                res_py = py_subtile(f1, f2, s=s, tile_xy=tile_xy, max_d=max_d)
                res_rs = self.rs_subtile(f1, f2, s=s, tile_xy=tile_xy, max_d=max_d)
                self.assertEqual(
                    res_py,
                    res_rs,
                    f"Mismatch at tile_xy={tile_xy} for W={W}, H={H}, s={s}, max_d={max_d}"
                )


if __name__ == "__main__":
    unittest.main()
