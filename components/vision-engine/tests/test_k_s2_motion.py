import os
import struct
import subprocess
import unittest
import numpy as np

from visioncore.change import global_vector, _islands, island_vector


class TestKS2Motion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bin_env = os.environ.get("VQA_K_S2_MOTION_BIN")
        if bin_env:
            cls.bin_path = bin_env
        else:
            repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            cls.bin_path = os.path.join(
                repo_root, "rust", "vqa_k_s2_motion", "target", "release", "vqa_k_s2_motion"
            )

        if not os.path.exists(cls.bin_path):
            cls.fail(cls, f"Release binary missing at {cls.bin_path}. Run 'cargo build --release --offline' first.")

    def run_binary_op1(self, width, height, s, y_cur, y_prev, mask):
        gh, gw = mask.shape
        y_cur_bytes = y_cur.astype("<i2").tobytes()
        y_prev_bytes = y_prev.astype("<i2").tobytes()
        mask_bytes = mask.astype(np.uint8).tobytes()

        req = bytearray()
        req.append(1)  # op 1
        req.extend(struct.pack("<qqq", width, height, s))
        req.extend(struct.pack("<QQ", len(y_cur_bytes), len(y_prev_bytes)))
        req.extend(y_cur_bytes)
        req.extend(y_prev_bytes)
        req.extend(struct.pack("<Q", len(mask_bytes)))
        req.extend(mask_bytes)
        req.append(0)  # op 0 (quit)

        proc = subprocess.Popen(
            [self.bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_data, stderr_data = proc.communicate(input=bytes(req))
        self.assertEqual(proc.returncode, 0, f"Process exited with error: {stderr_data.decode()}")

        # Parse response
        # status(i32), gdx(i32), gdy(i32), lab_len(u64)
        header_fmt = "<iiiQ"
        header_size = struct.calcsize(header_fmt)
        self.assertGreaterEqual(len(stdout_data), header_size)

        status, gdx, gdy, lab_len = struct.unpack_from(header_fmt, stdout_data, 0)
        self.assertEqual(status, 0)
        self.assertEqual(lab_len, gh * gw)

        offset = header_size
        lab_bytes_len = lab_len * 4
        self.assertGreaterEqual(len(stdout_data), offset + lab_bytes_len + 12)

        lab_arr = np.frombuffer(stdout_data[offset : offset + lab_bytes_len], dtype="<i4").reshape(gh, gw)
        offset += lab_bytes_len

        n_islands, idx, idy = struct.unpack_from("<iii", stdout_data, offset)

        return (gdx, gdy), lab_arr, n_islands, (idx, idy)

    def test_cases(self):
        # Seeded test cases
        rng = np.random.RandomState(42)

        cases = [
            # (width, height, tile_size, description)
            (64, 48, 16, "standard 16"),
            (128, 96, 32, "standard 32"),
            (70, 50, 16, "non-multiple 16"),
            (100, 70, 32, "non-multiple 32"),
            (1920, 1080, 16, "1080p case"),
        ]

        for width, height, s, desc in cases:
            with self.subTest(case=desc):
                gh, gw = height // s, width // s
                y_cur = rng.randint(-1000, 1000, size=(height, width), dtype=np.int16)
                y_prev = rng.randint(-1000, 1000, size=(height, width), dtype=np.int16)

                # Add frame borders (extreme values at edge)
                y_cur[0, :] = 2000
                y_cur[-1, :] = -2000
                y_cur[:, 0] = 2000
                y_cur[:, -1] = -2000

                mask = rng.choice([True, False], size=(gh, gw))

                # Python reference
                py_gv = global_vector(y_cur, y_prev, s)
                py_lab, py_n_islands = _islands(mask)
                py_iv = island_vector(mask, y_cur, y_prev, s)

                # Rust binary output
                rs_gv, rs_lab, rs_n_islands, rs_iv = self.run_binary_op1(
                    width, height, s, y_cur, y_prev, mask
                )

                self.assertEqual(py_gv, rs_gv, f"Global vector mismatch in {desc}")
                np.testing.assert_array_equal(py_lab, rs_lab, err_msg=f"Islands label mismatch in {desc}")
                self.assertEqual(py_n_islands, rs_n_islands, f"Island count mismatch in {desc}")
                self.assertEqual(py_iv, rs_iv, f"Island vector mismatch in {desc}")


if __name__ == "__main__":
    unittest.main()
