"""tests/test_jq1_a4_oracle_check_binary_mismatch_arms.py - Adversarial tests for gm_v2_oracle.check_binary mismatch attribution."""

import argparse
import os
import stat
import struct
import sys
import tempfile
import unittest
import numpy as np

from eval.v2x import gm_v2_oracle as oracle

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

HELPER_SCRIPT_CODE = f"""#!/usr/bin/env python3
import sys
import os
import struct
import argparse
import numpy as np

# Ensure repository root is on sys.path
repo_root = {repr(REPO_ROOT)}
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from eval.v2x import gm_bands_hv as mod

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="clean", choices=[
        "clean", "status", "has_gm", "dx", "dy", "score", "gh", "gw", "dx_and_score"
    ])
    args = parser.parse_args()

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    case_count = 0

    while True:
        op_raw = stdin.read(4)
        if not op_raw or len(op_raw) < 4:
            break
        op = struct.unpack("<i", op_raw)[0]
        if op == 0:
            break
        elif op == 1:
            # Framing per README / oracle: h (u32), w (u32), max_d (i32), k_top (u32), s (u32) -> 20 bytes header
            hdr = stdin.read(20)
            if len(hdr) < 20:
                break
            h, w, max_d, k_top, s = struct.unpack("<IIiII", hdr)
            prev_bytes = stdin.read(h * w)
            cur_bytes = stdin.read(h * w)
            prev = np.frombuffer(prev_bytes, dtype=np.uint8).reshape((h, w))
            cur = np.frombuffer(cur_bytes, dtype=np.uint8).reshape((h, w))

            res = mod.estimate_global_motion(prev, cur, max_d)
            if res is not None:
                has_gm = 1
                dx = int(res[0])
                dy = int(res[1])
                score = int(res[2])
            else:
                has_gm = 0
                dx = 0
                dy = 0
                score = 0

            mask = mod.consensus_tiles(prev, cur, s, dx, dy)
            gh = int(mask.shape[0])
            gw = int(mask.shape[1])
            status = 0

            if case_count == 0:
                if args.mode == "status":
                    status = 5
                elif args.mode == "has_gm":
                    has_gm = 0 if has_gm == 1 else 1  # mutate has_gm (0 -> 1 for case 0)
                elif args.mode == "dx":
                    dx = 17  # mutated value (advertised 17)
                elif args.mode == "dy":
                    dy = 25  # mutated value (advertised 25)
                elif args.mode == "score":
                    score = score + 1  # reference plus 1
                elif args.mode == "gh":
                    gh = 31  # mutated value (30 -> 31)
                elif args.mode == "gw":
                    gw = 41  # mutated value (40 -> 41)
                elif args.mode == "dx_and_score":
                    dx = 17
                    score = score + 1

            # Build advertised gh x gw bytes mask
            if gh == mask.shape[0] and gw == mask.shape[1]:
                mask_bytes = bytes(np.ascontiguousarray(mask, dtype=np.uint8).tobytes())
            else:
                # Dimension mutant: emit exactly gh * gw bytes so protocol alignment is maintained
                mask_bytes = bytes(gh * gw)

            case_count += 1

            # Response 29-byte header: status (i32), has_gm (u8), dx (i32), dy (i32), score (i64), gh (u32), gw (u32)
            resp_hdr = struct.pack("<iBiiqII", status, has_gm, dx, dy, score, gh, gw)
            stdout.write(resp_hdr)
            stdout.write(mask_bytes)
            stdout.flush()

if __name__ == "__main__":
    main()
"""


class TestCheckBinaryMismatchArms(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.script_path = os.path.join(self.tmpdir.name, "helper.py")
        with open(self.script_path, "w") as f:
            f.write(HELPER_SCRIPT_CODE)
        os.chmod(self.script_path, os.stat(self.script_path).st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_check_binary(self, mode: str) -> dict:
        cmd = [sys.executable, self.script_path, "--mode", mode]
        return oracle.check_binary(cmd, "eval.v2x.gm_bands_hv")

    def test_clean_helper_passes_all_cases(self):
        """Clean helper passes all 71 cases (VM timing context)."""
        report = self._run_check_binary("clean")
        self.assertTrue(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 71)
        self.assertTrue(all(c["pass"] for c in report["cases"]))

    def test_mismatch_status_arm(self):
        """Status mutant attributes status mismatch on first case (VM timing context)."""
        report = self._run_check_binary("status")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "status")
        self.assertEqual(case0["expected"], 0)
        self.assertEqual(case0["actual"], 5)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_has_gm_arm(self):
        """has_gm mutant attributes has_gm mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_has_gm = ref_tuple[0]

        report = self._run_check_binary("has_gm")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "has_gm")
        self.assertEqual(case0["expected"], exp_has_gm)
        self.assertEqual(case0["actual"], 1 - exp_has_gm)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_dx_arm(self):
        """dx mutant attributes dx mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_dx = ref_tuple[1]

        report = self._run_check_binary("dx")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "dx")
        self.assertEqual(case0["expected"], exp_dx)
        self.assertEqual(case0["actual"], 17)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_dy_arm(self):
        """dy mutant attributes dy mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_dy = ref_tuple[2]

        report = self._run_check_binary("dy")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "dy")
        self.assertEqual(case0["expected"], exp_dy)
        self.assertEqual(case0["actual"], 25)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_score_arm(self):
        """score mutant attributes score mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        ref_score = ref_tuple[3]

        report = self._run_check_binary("score")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "score")
        self.assertEqual(case0["expected"], ref_score)
        self.assertEqual(case0["actual"], ref_score + 1)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_gh_arm(self):
        """gh mutant attributes gh mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_gh = ref_tuple[4]

        report = self._run_check_binary("gh")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "gh")
        self.assertEqual(case0["expected"], exp_gh)
        self.assertEqual(case0["actual"], 31)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_gw_arm(self):
        """gw mutant attributes gw mismatch on first case (VM timing context)."""
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_gw = ref_tuple[5]

        report = self._run_check_binary("gw")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "gw")
        self.assertEqual(case0["expected"], exp_gw)
        self.assertEqual(case0["actual"], 41)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])

    def test_mismatch_precedence_order(self):
        """Assert full field precedence: status -> has_gm -> dx -> dy -> score -> gh -> gw -> mask (VM timing context)."""
        # dx vs score
        ref_tuple, _ = oracle.expected("eval.v2x.gm_bands_hv", oracle.conformance_cases()[0])
        exp_dx = ref_tuple[1]

        report = self._run_check_binary("dx_and_score")
        self.assertFalse(report["all_pass"])
        self.assertEqual(report["total_cases"], 71)
        self.assertEqual(report["passed_cases"], 70)

        case0 = report["cases"][0]
        self.assertFalse(case0["pass"])
        self.assertEqual(case0["first_mismatch"], "dx")
        self.assertEqual(case0["expected"], exp_dx)
        self.assertEqual(case0["actual"], 17)

        for c in report["cases"][1:]:
            self.assertTrue(c["pass"])


if __name__ == "__main__":
    unittest.main()
