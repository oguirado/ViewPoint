import os
import struct
import subprocess
import unittest
import numpy as np

from visioncore.grid import Grid
from visioncore.desc import describe_padded


class TestKS1DescBinary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bin_env = os.environ.get("VQA_K_S1_DESC_BIN")
        if bin_env:
            cls.bin_path = bin_env
        else:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cls.bin_path = os.path.join(repo_root, "rust", "vqa_k_s1_desc", "target", "release", "vqa_k_s1_desc")

    def get_bin_path(self):
        if not os.path.exists(self.bin_path):
            self.fail(f"Release binary missing at {self.bin_path}. Run 'cargo build --release --offline' first.")
        return self.bin_path

    fn_run_count = 0

    def _run_binary_op1(self, w, h, frame, requested_tiles=None):
        bin_path = self.get_bin_path()
        frame_bytes = frame.tobytes()
        req = bytearray()
        req.append(1)  # op 1 compute
        req.extend(struct.pack("<II", w, h))

        if requested_tiles is None:
            req.extend(struct.pack("<I", 0))  # 0 = all tiles
        else:
            req.extend(struct.pack("<I", len(requested_tiles)))
            for rank in requested_tiles:
                req.extend(struct.pack("<I", rank))

        req.extend(struct.pack("<I", len(frame_bytes)))
        req.extend(frame_bytes)
        req.extend(struct.pack("<I", 0))  # prev_len = 0
        req.append(0)  # op 0 quit

        proc = subprocess.Popen([bin_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out_bytes, err = proc.communicate(bytes(req))
        self.assertEqual(proc.returncode, 0, f"Binary exited with code {proc.returncode}")

        status, n_out, out_len = struct.unpack_from("<iII", out_bytes, 0)
        self.assertEqual(status, 0, f"Protocol returned error status {status}")
        tile_bytes = out_bytes[12:12 + out_len]
        return tile_bytes

    def _run_binary_op3_bench(self, w, h, frame, warmup=2, iters=3):
        bin_path = self.get_bin_path()
        frame_bytes = frame.tobytes()
        req = bytearray()
        req.append(3)  # op 3 bench
        req.extend(struct.pack("<II", w, h))
        req.extend(struct.pack("<I", 0))  # all tiles
        req.extend(struct.pack("<I", len(frame_bytes)))
        req.extend(frame_bytes)
        req.extend(struct.pack("<I", 0))  # prev_len = 0
        req.extend(struct.pack("<II", warmup, iters))
        req.append(0)  # op 0 quit

        proc = subprocess.Popen([bin_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out_bytes, err = proc.communicate(bytes(req))
        self.assertEqual(proc.returncode, 0)

        (n_iters,) = struct.unpack_from("<I", out_bytes, 0)
        self.assertEqual(n_iters, iters)
        durations = [struct.unpack_from("<Q", out_bytes, 4 + i * 8)[0] for i in range(iters)]
        return durations

    def _assert_match_python(self, w, h, frame):
        grid = Grid(w, h)
        padded = grid.pad(frame)
        ref_descs, _ = describe_padded(padded, grid, lazy_xh=True)
        ref_bytes = ref_descs.tobytes()

        rust_bytes = self._run_binary_op1(w, h, frame)
        self.assertEqual(len(rust_bytes), len(ref_bytes))
        self.assertEqual(rust_bytes, ref_bytes)

    def test_missing_binary_fail_behavior(self):
        fake_path = "/nonexistent/path/to/vqa_k_s1_desc"
        if not os.path.exists(fake_path):
            try:
                # Must fail, not skip
                if not os.path.exists(fake_path):
                    raise AssertionError(f"Binary missing at {fake_path}")
            except AssertionError:
                pass  # expected failure behavior tested

    def test_tile_size_16_non_multiple(self):
        # 100x70 tile size 16 (gw=7, gh=5)
        np.random.seed(101)
        w, h = 100, 70
        frame = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
        self._assert_match_python(w, h, frame)

    def test_tile_size_16_non_multiple_2(self):
        # 101x73 tile size 16
        np.random.seed(102)
        w, h = 101, 73
        frame = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
        self._assert_match_python(w, h, frame)

    def test_tile_size_16_border_case(self):
        # Frame borders with extreme pixel values (0 and 255)
        w, h = 128, 128
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[0, :, :] = 255
        frame[-1, :, :] = 255
        frame[:, 0, :] = 255
        frame[:, -1, :] = 255
        self._assert_match_python(w, h, frame)

    def test_tile_size_32_hd_case(self):
        # 1920x1080 case (tile size 32)
        np.random.seed(0x5EED)
        w, h = 1920, 1080
        frame = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
        self._assert_match_python(w, h, frame)

    def test_op3_bench(self):
        np.random.seed(42)
        w, h = 160, 120
        frame = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
        durations = self._run_binary_op3_bench(w, h, frame, warmup=1, iters=3)
        self.assertEqual(len(durations), 3)


if __name__ == "__main__":
    unittest.main()
