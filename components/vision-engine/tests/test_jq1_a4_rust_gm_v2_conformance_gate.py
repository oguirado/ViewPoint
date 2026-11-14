"""tests/test_jq1_a4_rust_gm_v2_conformance_gate.py - Native parity gate for rust/vqa_k_gm_v2 (defect D1)."""

import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import numpy as np

from eval.v2x import gm_bands_hv
from eval.v2x import gm_v2_oracle as oracle


def resolve_v2_binary() -> str:
    """Resolve repo-relative executable path or VQA_K_GM_V2_BIN override."""
    env_bin = os.environ.get("VQA_K_GM_V2_BIN")
    if env_bin:
        return env_bin
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(repo_root, "rust", "vqa_k_gm_v2", "target", "release", "vqa_k_gm_v2")


def run_pipe_conformance_check(test_case: unittest.TestCase, cmd):
    """Run pipe conformance sequence (op 1, op 2 explicit consensus, op 2 large_dy, op 1, op 0)."""
    if isinstance(cmd, (str, bytes)):
        cmd_args = [str(cmd)]
    else:
        cmd_args = [str(x) for x in cmd]

    proc = subprocess.Popen(
        cmd_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    try:
        # 1. Send op 1 flat_window_drag
        cases = oracle.conformance_cases()
        flat_case = next(c for c in cases if c["case_id"] == "flat_window_drag")

        req1 = oracle.encode_request(flat_case, op=1)
        proc.stdin.write(req1)
        proc.stdin.flush()

        hdr1_raw = proc.stdout.read(29)
        test_case.assertEqual(len(hdr1_raw), 29, "Op 1 response header truncation")
        status1, has_gm1, dx1, dy1, score1, gh1, gw1 = struct.unpack("<iBiiqII", hdr1_raw)
        mask1_bytes = proc.stdout.read(gh1 * gw1)
        test_case.assertEqual(len(mask1_bytes), gh1 * gw1, "Op 1 response mask truncation")
        resp1_raw = hdr1_raw + mask1_bytes

        resp1 = oracle.decode_response(resp1_raw, op=1)
        test_case.assertEqual(resp1["status"], 0)

        # 2. Send op 2 explicit consensus (textured block on background 40, 480x640, s=16, dx=8, dy=8)
        h2, w2, s2 = 480, 640, 16
        dx2, dy2 = 8, 8
        prev2 = np.full((h2, w2), 40, dtype=np.uint8)
        cur2 = np.full((h2, w2), 40, dtype=np.uint8)
        rng2 = np.random.default_rng(12345)
        block_h, block_w = 128, 128
        by0, bx0 = 100, 100
        textured_block = rng2.integers(0, 256, (block_h, block_w), dtype=np.uint8)
        prev2[by0 : by0 + block_h, bx0 : bx0 + block_w] = textured_block
        cur2[by0 + dy2 : by0 + dy2 + block_h, bx0 + dx2 : bx0 + dx2 + block_w] = textured_block

        case_tex = {
            "h": h2,
            "w": w2,
            "s": s2,
            "dx": dx2,
            "dy": dy2,
            "prev": prev2,
            "cur": cur2,
        }
        req2_a = oracle.encode_request(case_tex, op=2)
        proc.stdin.write(req2_a)
        proc.stdin.flush()

        hdr2_a_raw = proc.stdout.read(12)
        test_case.assertEqual(len(hdr2_a_raw), 12, "Op 2 (textured) response header truncation")
        status2_a, gh2_a, gw2_a = struct.unpack("<iII", hdr2_a_raw)
        mask2_a_bytes = proc.stdout.read(gh2_a * gw2_a)
        test_case.assertEqual(len(mask2_a_bytes), gh2_a * gw2_a, "Op 2 (textured) response mask truncation")
        resp2_a = oracle.decode_response(hdr2_a_raw + mask2_a_bytes, op=2)

        test_case.assertEqual(resp2_a["status"], 0)
        exp_mask_tex = gm_bands_hv.consensus_tiles(prev2, cur2, s2, dx2, dy2)
        np.testing.assert_array_equal(resp2_a["mask"], exp_mask_tex)
        # Background tiles (e.g. tile [0, 0]) must be False
        test_case.assertFalse(resp2_a["mask"][0, 0])

        # 3. Send op 2 explicit consensus (large_dy_1088_1920, s=16, dx=0, dy=300)
        h3, w3, s3 = 1088, 1920, 16
        dx3, dy3 = 0, 300
        rng3 = np.random.default_rng(54321)
        prev3 = rng3.integers(0, 256, (h3, w3), dtype=np.uint8)
        cur3 = np.empty_like(prev3)
        cur3[dy3:, :] = prev3[:-dy3, :]
        cur3[:dy3, :] = rng3.integers(0, 256, (dy3, w3), dtype=np.uint8)

        case_large = {
            "h": h3,
            "w": w3,
            "s": s3,
            "dx": dx3,
            "dy": dy3,
            "prev": prev3,
            "cur": cur3,
        }
        req2_b = oracle.encode_request(case_large, op=2)
        proc.stdin.write(req2_b)
        proc.stdin.flush()

        hdr2_b_raw = proc.stdout.read(12)
        test_case.assertEqual(len(hdr2_b_raw), 12, "Op 2 (large_dy) response header truncation")
        status2_b, gh2_b, gw2_b = struct.unpack("<iII", hdr2_b_raw)
        mask2_b_bytes = proc.stdout.read(gh2_b * gw2_b)
        test_case.assertEqual(len(mask2_b_bytes), gh2_b * gw2_b, "Op 2 (large_dy) response mask truncation")
        resp2_b = oracle.decode_response(hdr2_b_raw + mask2_b_bytes, op=2)

        test_case.assertEqual(resp2_b["status"], 0)
        test_case.assertEqual(resp2_b["gh"], 68, "Grid height must be 68 for 1088x1920 with s=16")
        test_case.assertEqual(resp2_b["gw"], 120, "Grid width must be 120 for 1088x1920 with s=16")
        exp_mask_large = gm_bands_hv.consensus_tiles(prev3, cur3, s3, dx3, dy3)
        np.testing.assert_array_equal(resp2_b["mask"], exp_mask_large)

        # 4. Send the same op 1 flat_window_drag request
        proc.stdin.write(req1)
        proc.stdin.flush()

        hdr3_raw = proc.stdout.read(29)
        test_case.assertEqual(len(hdr3_raw), 29, "Second Op 1 response header truncation")
        status3, has_gm3, dx3_res, dy3_res, score3, gh3, gw3 = struct.unpack("<iBiiqII", hdr3_raw)
        mask3_bytes = proc.stdout.read(gh3 * gw3)
        test_case.assertEqual(len(mask3_bytes), gh3 * gw3, "Second Op 1 response mask truncation")
        resp3_raw = hdr3_raw + mask3_bytes

        # Assert before and after op 1 responses are byte-identical
        test_case.assertEqual(resp1_raw, resp3_raw, "Before and after op 1 responses must be byte-identical")

        # 5. Send op 0 quit
        proc.stdin.write(struct.pack("<i", 0))
        proc.stdin.flush()
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class TestFixtureSanityAndReference(unittest.TestCase):
    """Independent subtests/tests for reference cases and fixtures so missing native does not conceal fixture mistakes."""

    def test_explicit_consensus_fixture_properties(self):
        """Verify explicit consensus fixture with background 40 and textured block against gm_bands_hv."""
        h, w, s = 480, 640, 16
        dx, dy = 8, 8
        prev = np.full((h, w), 40, dtype=np.uint8)
        cur = np.full((h, w), 40, dtype=np.uint8)

        rng = np.random.default_rng(42)
        block_h, block_w = 128, 128
        by0, bx0 = 100, 100
        textured_block = rng.integers(0, 256, (block_h, block_w), dtype=np.uint8)

        prev[by0 : by0 + block_h, bx0 : bx0 + block_w] = textured_block
        cur[by0 + dy : by0 + dy + block_h, bx0 + dx : bx0 + dx + block_w] = textured_block

        mask = gm_bands_hv.consensus_tiles(prev, cur, s, dx, dy)
        gh, gw = (h + s - 1) // s, (w + s - 1) // s
        self.assertEqual(mask.shape, (gh, gw))

        # Background tiles (e.g. tile [0, 0]) must be False
        self.assertFalse(mask[0, 0])
        # Changed tiles in block must have True consensus tiles
        self.assertTrue(np.any(mask))

    def test_large_dy_explicit_consensus_grid_dimensions(self):
        """Verify 1088x1920 with s=16 produces 68x120 tile grid rather than prototype 34x60."""
        h, w, s = 1088, 1920, 16
        dx, dy = 0, 300
        rng = np.random.default_rng(1234)
        prev = rng.integers(0, 256, (h, w), dtype=np.uint8)
        cur = np.empty_like(prev)
        cur[dy:, :] = prev[:-dy, :]
        cur[:dy, :] = rng.integers(0, 256, (dy, w), dtype=np.uint8)

        mask = gm_bands_hv.consensus_tiles(prev, cur, s, dx, dy)
        gh, gw = (h + s - 1) // s, (w + s - 1) // s
        self.assertEqual(gh, 68)
        self.assertEqual(gw, 120)
        self.assertEqual(mask.shape, (68, 120))
        self.assertNotEqual((gh, gw), (34, 60))

    def test_fake_binary_pipe_conformance(self):
        """Verify pipe protocol sequence (op 1, op 2, op 1, op 0) against fake Python server."""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        fake_server_script = f"""#!/usr/bin/env python3
import sys, os, struct, importlib
sys.path.insert(0, {repr(repo_root)})
import numpy as np
from eval.v2x import gm_bands_hv

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer

while True:
    op_raw = stdin.read(4)
    if not op_raw or len(op_raw) < 4:
        break
    op = struct.unpack("<i", op_raw)[0]
    if op == 0:
        break
    elif op == 1:
        hdr = stdin.read(20)
        h, w, max_d, k_top, s = struct.unpack("<IIiII", hdr)
        prev_bytes = stdin.read(h * w)
        cur_bytes = stdin.read(h * w)
        prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w))
        cur = np.frombuffer(cur_bytes, dtype=np.uint8).reshape((h, w))

        res = gm_bands_hv.estimate_global_motion(prev, cur, max_d)
        if res is not None:
            has_gm, dx, dy, score = 1, int(res[0]), int(res[1]), int(res[2])
        else:
            has_gm, dx, dy, score = 0, 0, 0, 0

        mask = gm_bands_hv.consensus_tiles(prev, cur, s, dx, dy)
        gh, gw = mask.shape[0], mask.shape[1]
        mask_bytes = np.ascontiguousarray(mask, dtype=np.uint8).tobytes()

        resp_hdr = struct.pack("<iBiiqII", 0, has_gm, dx, dy, score, gh, gw)
        stdout.write(resp_hdr + mask_bytes)
        stdout.flush()
    elif op == 2:
        hdr = stdin.read(20)
        h, w, s, dx, dy = struct.unpack("<IIIii", hdr)
        prev_bytes = stdin.read(h * w)
        cur_bytes = stdin.read(h * w)
        prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w))
        cur = np.frombuffer(cur_bytes, dtype=np.uint8).reshape((h, w))

        mask = gm_bands_hv.consensus_tiles(prev, cur, s, dx, dy)
        gh, gw = mask.shape[0], mask.shape[1]
        mask_bytes = np.ascontiguousarray(mask, dtype=np.uint8).tobytes()

        resp_hdr = struct.pack("<iII", 0, gh, gw)
        stdout.write(resp_hdr + mask_bytes)
        stdout.flush()
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = os.path.join(tmpdir, "fake_v2_server.py")
            with open(fake_bin, "w") as f:
                f.write(fake_server_script)
            os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IEXEC)

            run_pipe_conformance_check(self, [sys.executable, fake_bin])


class TestRustGMV2ConformanceGate(unittest.TestCase):
    """Native parity gate for rust/vqa_k_gm_v2 (defect D1)."""

    def test_d1_rust_gm_v2_oracle_conformance(self):
        # D1: native binary required; an absent or mismatched implementation is a failure.
        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        report = oracle.check_binary(bin_path, "eval.v2x.gm_bands_hv")
        self.assertTrue(report["all_pass"], f"Oracle check failed: {report}")
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 71)

    def test_d1_rust_gm_v2_pipe_conformance(self):
        # D1: native binary required; an absent or mismatched implementation is a failure.
        bin_path = resolve_v2_binary()
        self.assertTrue(os.path.exists(bin_path), f"Native binary missing at {bin_path}")
        self.assertTrue(os.access(bin_path, os.X_OK), f"Native binary not executable at {bin_path}")

        run_pipe_conformance_check(self, [bin_path])


if __name__ == "__main__":
    unittest.main()
