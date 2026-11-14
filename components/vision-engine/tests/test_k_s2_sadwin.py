"""Integration tests for vqa_k_s2_sadwin binary against Python reference."""
import os
import struct
import subprocess
import unittest
from pathlib import Path
import numpy as np

from visioncore import change

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BIN = REPO_ROOT / "rust" / "vqa_k_s2_sadwin" / "target" / "release" / "vqa_k_s2_sadwin"


class TestSadWinBinary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bin_path_str = os.environ.get("VQA_K_S2_SADWIN_BIN")
        if bin_path_str:
            cls.bin_path = Path(bin_path_str)
        else:
            cls.bin_path = DEFAULT_BIN

        if not cls.bin_path.is_file():
            cls.fail(cls, f"Required release binary missing at {cls.bin_path}. Run cargo build --release first.")

    fnv64_init = 0xcbf29ce484222325
    fnv64_prime = 0x100000001b3

    @classmethod
    def _splitmix64(cls, seed):
        state = seed & 0xFFFFFFFFFFFFFFFF
        while True:
            state = (state + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
            z = state
            z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
            z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
            yield (z ^ (z >> 31)) & 0xFFFFFFFFFFFFFFFF

    def _call_binary(self, h, w, dx, dy, s, y_cur, y_prev, tx, ty):
        proc = subprocess.Popen(
            [str(self.bin_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        req_buf = bytearray()
        req_buf.append(1)  # op 1 compute
        req_buf.extend(struct.pack("<5q2Q", h, w, dx, dy, s, y_cur.size, tx.size))

        y_cur_flat = y_cur.astype(np.int64).ravel()
        y_prev_flat = y_prev.astype(np.int64).ravel()

        req_buf.extend(struct.pack(f"<{len(y_cur_flat)}q", *y_cur_flat))
        req_buf.extend(struct.pack(f"<{len(y_prev_flat)}q", *y_prev_flat))

        tx_flat = tx.astype(np.int64).ravel()
        ty_flat = ty.astype(np.int64).ravel()

        req_buf.extend(struct.pack(f"<{len(tx_flat)}q", *tx_flat))
        req_buf.extend(struct.pack(f"<{len(ty_flat)}q", *ty_flat))

        req_buf.append(0)  # op 0 quit

        stdout, stderr = proc.communicate(bytes(req_buf))
        self.assertEqual(proc.returncode, 0, f"Binary process exited with code {proc.returncode}: {stderr.decode()}")

        status = struct.unpack("<i", stdout[:4])[0]
        if status != 0:
            return status, None, None

        offset = 4
        n_sad = struct.unpack("<Q", stdout[offset:offset+8])[0]
        offset += 8
        sads = struct.unpack(f"<{n_sad}q", stdout[offset:offset+8*n_sad])
        offset += 8 * n_sad

        n_cnt = struct.unpack("<Q", stdout[offset:offset+8])[0]
        offset += 8
        counts = struct.unpack(f"<{n_cnt}q", stdout[offset:offset+8*n_cnt])

        return 0, np.array(sads, dtype=np.int64), np.array(counts, dtype=np.int64)

    def test_conformance_cases(self):
        rng = np.random.default_rng(0x5EED)

        # Test cases including tile sizes 16, 32, non-multiple dimensions, frame borders, 1080p
        configs = [
            (16, 1080, 1920, 16),
            (32, 64, 64, 32),
            (16, 40, 48, 20),  # non-multiple dimension
            (32, 70, 64, 35),  # non-multiple dimension
        ]

        for s, frame_h, frame_w, _ in configs:
            h = frame_h
            w = frame_w

            y_cur = rng.integers(-32768, 32767, (h, w), dtype=np.int16)
            y_prev = rng.integers(-32768, 32767, (h, w), dtype=np.int16)

            max_tx = w // s
            max_ty = h // s

            for dx, dy in [(0, 0), (s // 2, -s // 2), (s + 2, -s - 2), (w, h)]:
                # Requested tile coordinates including borders, positive, negative
                tx = np.array([0, max_tx - 1, -1, -max_tx, 0], dtype=np.int64)
                ty = np.array([0, max_ty - 1, -1, -max_ty, max_ty - 1], dtype=np.int64)

                ref_sad, ref_count = change._tile_sad_windows(y_cur, y_prev, (dx, dy), s, tx, ty)

                st, bin_sad, bin_count = self._call_binary(h, w, dx, dy, s, y_cur, y_prev, tx, ty)
                self.assertEqual(st, 0)
                np.testing.assert_array_equal(bin_sad, ref_sad)
                np.testing.assert_array_equal(bin_count, ref_count)

    def test_bench_op(self):
        h, w, s = 32, 32, 16
        y_cur = np.zeros((h, w), dtype=np.int16)
        y_prev = np.zeros((h, w), dtype=np.int16)
        tx = np.array([0], dtype=np.int64)
        ty = np.array([0], dtype=np.int64)

        proc = subprocess.Popen(
            [str(self.bin_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        req_buf = bytearray()
        req_buf.append(3)  # op 3 bench
        req_buf.extend(struct.pack("<5q2Q", h, w, 0, 0, s, y_cur.size, tx.size))
        req_buf.extend(struct.pack(f"<{y_cur.size}q", *y_cur.ravel().astype(np.int64)))
        req_buf.extend(struct.pack(f"<{y_prev.size}q", *y_prev.ravel().astype(np.int64)))
        req_buf.extend(struct.pack("<1q", 0))
        req_buf.extend(struct.pack("<1q", 0))
        req_buf.extend(struct.pack("<2I", 2, 5))  # warmup 2, iters 5
        req_buf.append(0)  # op 0 quit

        stdout, stderr = proc.communicate(bytes(req_buf))
        self.assertEqual(proc.returncode, 0)

        n_iters = struct.unpack("<I", stdout[:4])[0]
        self.assertEqual(n_iters, 5)
        times = struct.unpack("<5Q", stdout[4:4+8*5])
        self.assertEqual(len(times), 5)


if __name__ == "__main__":
    unittest.main()
