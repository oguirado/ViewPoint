"""Extension conformance test module asserting new cases for wlcap and mcp exist and pass contracts/cubook/run_conformance.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(ROOT))
WORK = os.environ.get("CUBOOK_WORK") or os.path.join(REPO, "build", "cubook_conf")

NEW_CASES = [
    ("wlcap", "bench_keep_rc2"),
    ("wlcap", "bench_analyze_host_rc2"),
    ("wlcap", "analyze_wait_zero_rc2"),
    ("wlcap", "xshm_unavailable_rc5"),
    ("mcp", "tools_call_unknown"),
    ("mcp", "jsonrpc_bad_line"),
]

_BIN = {}


def real_bin():
    if "d" in _BIN:
        return _BIN["d"]
    d = os.environ.get("CUBOOK_BIN_DIR")
    if not d:
        d = os.path.join(WORK, "bin")
        os.makedirs(d, exist_ok=True)
        env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin")
        p = subprocess.run(["go", "build", "-o", d + "/", "./cmd/srf-cu-exec", "./cmd/srf-cu-mcp",
                            "./cmd/srf-cu-snap", "./cmd/srf-cu-wlcap"], cwd=REPO, env=env, capture_output=True, timeout=400)
        assert p.returncode == 0, p.stderr.decode()[:400]
    _BIN["d"] = d
    return d


class TestConformanceExtWlcapMcp(unittest.TestCase):
    def test_new_case_dirs_exist(self):
        for cli, case in NEW_CASES:
            case_dir = os.path.join(ROOT, "fixtures", cli, case)
            self.assertTrue(os.path.isdir(case_dir), f"Missing fixture directory: {case_dir}")
            for fname in ("argv.json", "env.json", "expected_rc", "expected_stdout", "meta.json"):
                fpath = os.path.join(case_dir, fname)
                self.assertTrue(os.path.isfile(fpath), f"Missing required fixture file: {fpath}")

    def test_conformance_passes_for_wlcap_and_mcp(self):
        bin_dir = real_bin()
        runner = os.path.join(ROOT, "run_conformance.py")
        for cli in ("wlcap", "mcp"):
            out = tempfile.mktemp(dir=WORK, suffix=".json")
            p = subprocess.run([sys.executable, "-B", runner, "--bin-dir", bin_dir, "--only", cli, "--json", out],
                               capture_output=True, timeout=300)
            self.assertEqual(p.returncode, 0, f"Conformance failed for {cli}: {p.stderr.decode()}")
            with open(out) as f:
                res = json.load(f)
            if os.path.exists(out):
                os.remove(out)
            failed = res.get("summary", {}).get("failed", res.get("failed", []))
            self.assertEqual(failed, [], f"Failed cases for {cli}: {failed}")


if __name__ == "__main__":
    unittest.main()
