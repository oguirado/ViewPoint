"""tests/test_k_gm_proto.py - Unit test suite comparing Rust vqa_k_gm_proto binary and adapter to Python reference."""

import os
import unittest
from pathlib import Path
import numpy as np

from eval import v2proto_global_motion as py_gm
from eval.v2x import gm_proto_rs as adapter_gm

ROOT = Path(__file__).resolve().parents[1]
BIN_ENV = os.environ.get("VQA_K_GM_PROTO_BIN")
DEFAULT_BIN = ROOT / "rust/vqa_k_gm_proto/target/release/vqa_k_gm_proto"


def make_random_frame(width, height, seed, border=0):
    rng = np.random.RandomState(seed)
    frame = rng.randint(0, 256, size=(height, width), dtype=np.uint8)
    if border > 0:
        frame[:border, :] = 30
        frame[-border:, :] = 30
        frame[:, :border] = 30
        frame[:, -border:] = 30
    return frame


class TestKGmProto(unittest.TestCase):

    def setUp(self):
        if BIN_ENV and Path(BIN_ENV).exists():
            self.binary_path = Path(BIN_ENV)
        elif DEFAULT_BIN.exists():
            self.binary_path = DEFAULT_BIN
        else:
            self.fail(
                f"Release binary missing at {DEFAULT_BIN} or VQA_K_GM_PROTO_BIN. "
                "Must build with: cargo build --release --offline in rust/vqa_k_gm_proto"
            )

    def test_missing_binary_raises_import_error(self):
        """Adapter raises error if binary path is invalid."""
        old_bin = adapter_gm.BINARY_PATH
        try:
            adapter_gm.BINARY_PATH = Path("/nonexistent/path/to/binary")
            adapter_gm._RUNNER = None
            with self.assertRaises((ImportError, RuntimeError, FileNotFoundError)):
                adapter_gm.estimate_global_motion(
                    np.zeros((32, 32), dtype=np.uint8),
                    np.zeros((32, 32), dtype=np.uint8),
                    8,
                )
        finally:
            adapter_gm.BINARY_PATH = old_bin
            adapter_gm._RUNNER = None

    def _check_case(self, prev, cur, max_d, s_tile=None):
        H, W = cur.shape
        py_res = py_gm.estimate_global_motion(prev, cur, max_d)
        rs_res = adapter_gm.estimate_global_motion(prev, cur, max_d)

        self.assertEqual(
            py_res,
            rs_res,
            f"Global motion result mismatch for {W}x{H} max_d={max_d}: py={py_res}, rs={rs_res}",
        )

        if py_res is not None:
            dx, dy, _ = py_res
            if s_tile is None:
                s_tile = py_gm.grid_params(W, H)[0]
            py_mask = py_gm.consensus_tiles(prev, cur, s_tile, dx, dy)
            rs_mask = adapter_gm.consensus_tiles(prev, cur, s_tile, dx, dy)

            np.testing.assert_array_equal(
                py_mask,
                rs_mask,
                err_msg=f"Consensus mask mismatch for {W}x{H} s={s_tile} dx={dx} dy={dy}",
            )

    def test_small_non_multiple_tile_size(self):
        """Test sizes that are not multiples of tile size (e.g. 100x70, 105x85)."""
        W, H = 100, 70
        big = make_random_frame(W + 50, H + 50, seed=1001, border=5)
        dx, dy = 7, 5
        prev = big[20 : 20 + H, 20 : 20 + W]
        cur = big[20 - dy : 20 - dy + H, 20 - dx : 20 - dx + W]

        self._check_case(prev, cur, max_d=20)

    def test_tile_sizes_16_and_32(self):
        """Test tile sizes 16 and 32."""
        W1, H1 = 256, 256
        big1 = make_random_frame(W1 + 40, H1 + 40, seed=2002)
        prev1 = big1[20 : 20 + H1, 20 : 20 + W1]
        cur1 = big1[15 : 15 + H1, 12 : 12 + W1]
        self._check_case(prev1, cur1, max_d=20, s_tile=16)

        W2, H2 = 512, 512
        big2 = make_random_frame(W2 + 40, H2 + 40, seed=3003)
        prev2 = big2[20 : 20 + H2, 20 : 20 + W2]
        cur2 = big2[10 : 10 + H2, 10 : 10 + W2]
        self._check_case(prev2, cur2, max_d=20, s_tile=32)

    def test_frame_borders(self):
        """Test frames with dark borders."""
        W, H = 320, 240
        big = make_random_frame(W + 60, H + 60, seed=4004, border=15)
        dx, dy = -12, 8
        prev = big[30 : 30 + H, 30 : 30 + W]
        cur = big[30 - dy : 30 - dy + H, 30 - dx : 30 - dx + W]

        self._check_case(prev, cur, max_d=30)

    def test_1080p_case(self):
        """Test one 1920x1080 case."""
        W, H = 1920, 1080
        big = make_random_frame(W + 100, H + 100, seed=5005, border=20)
        dx, dy = 16, -24
        prev = big[50 : 50 + H, 50 : 50 + W]
        cur = big[50 - dy : 50 - dy + H, 50 - dx : 50 - dx + W]

        self._check_case(prev, cur, max_d=50)

    def test_flat_frames(self):
        """Flat frames return None identically."""
        prev = np.full((128, 128), 128, dtype=np.uint8)
        cur = np.full((128, 128), 128, dtype=np.uint8)

        self._check_case(prev, cur, max_d=32)


if __name__ == "__main__":
    unittest.main()
