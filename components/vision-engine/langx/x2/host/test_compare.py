#!/usr/bin/env python3
"""
Unit tests for LANG-X X2 host comparison harness (langx/x2/host/compare.py).
Runs without requiring a real browser or CHROME_BIN.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from langx.x2.host.compare import run_comparison, calculate_median, find_json_diff_path, first_differing_byte_offset


class TestCompareHarness(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_x2_compare_")
        self.fixtures_dir = os.path.join(self.temp_dir, "fixtures")
        os.makedirs(self.fixtures_dir, exist_ok=True)
        # Create a manifest and a dummy steps file
        with open(os.path.join(self.fixtures_dir, "manifest.json"), "w") as f:
            json.dump({"pages": ["page1"]}, f)
        with open(os.path.join(self.fixtures_dir, "page1.json"), "w") as f:
            json.dump({"steps": []}, f)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_fake_seed_script(self, name, json_stdout, launch_ms=10, steps_ms=None, exit_code=0):
        """Helper to write a python script pretending to be a seed process."""
        if steps_ms is None:
            steps_ms = [5, 15]

        script_path = os.path.join(self.temp_dir, f"{name}.py")
        code = f"""import sys, json, os, argparse

parser = argparse.ArgumentParser()
parser.add_argument("--page")
parser.add_argument("--steps")
parser.add_argument("--timing")
parser.add_argument("--timeout-s")
args, _ = parser.parse_known_args()

timing_path = args.timing
if timing_path:
    with open(timing_path, "w") as f:
        json.dump({{"launch_ms": {launch_ms}, "steps_ms": {steps_ms}}}, f)

exit_code = {exit_code}
if exit_code != 0:
    sys.exit(exit_code)

stdout_val = {json.dumps(json_stdout)}
sys.stdout.write(stdout_val)
sys.stdout.flush()
"""
        with open(script_path, "w") as f:
            f.write(code)

        return [sys.executable, script_path]

    def test_skip_path_when_chrome_bin_unset(self):
        out_stream = io.StringIO()
        out_json = os.path.join(self.temp_dir, "out.json")
        res = run_comparison(
            seeds=["py_w1"],
            fixtures_dir=self.fixtures_dir,
            out_path=out_json,
            env={},
            stdout_stream=out_stream
        )
        self.assertEqual(res, 0)
        self.assertTrue(out_stream.getvalue().startswith("SKIP"))
        self.assertFalse(os.path.exists(out_json))

    def test_median_arithmetic(self):
        self.assertEqual(calculate_median([1, 3, 5]), 3)
        self.assertEqual(calculate_median([10, 20, 30, 40]), 25)
        self.assertEqual(calculate_median([10, 20, 31, 40]), 25.5)
        self.assertEqual(calculate_median([]), None)

    def test_byte_offset_and_json_diff(self):
        b1 = b'{"a": 1, "b": "hello"}'
        b2 = b'{"a": 1, "b": "world"}'
        off = first_differing_byte_offset(b1, b2)
        self.assertEqual(off, 15)

        obj1 = json.loads(b1.decode("utf-8"))
        obj2 = json.loads(b2.decode("utf-8"))
        path = find_json_diff_path(obj1, obj2)
        self.assertEqual(path, "$.b")

    def test_all_equal(self):
        out_json = os.path.join(self.temp_dir, "out.json")
        out_stream = io.StringIO()

        doc = {"status": "ok", "elements": [1, 2, 3]}
        s1 = self._create_fake_seed_script("s1", json.dumps(doc), launch_ms=10, steps_ms=[10, 20])
        s2 = self._create_fake_seed_script("s2", json.dumps(doc), launch_ms=12, steps_ms=[11, 21])

        command_map = {"s1": s1, "s2": s2}

        res = run_comparison(
            seeds=["s1", "s2"],
            fixtures_dir=self.fixtures_dir,
            reps=3,
            out_path=out_json,
            command_map=command_map,
            env={"CHROME_BIN": "/usr/bin/chrome"},
            stdout_stream=out_stream
        )
        self.assertEqual(res, 0)
        self.assertTrue(os.path.exists(out_json))

        with open(out_json, "r") as f:
            data = json.load(f)

        p_rep = data["pages"]["page1"]
        self.assertTrue(p_rep["determinism"]["s1"])
        self.assertTrue(p_rep["determinism"]["s2"])
        self.assertEqual(p_rep["conformance"]["dissenting_seeds"], [])

        self.assertEqual(data["timing"]["s1"]["launch_ms_median"], 10)
        self.assertEqual(data["timing"]["s1"]["steps_ms_median"], 15)

    def test_one_dissenting_seed(self):
        out_json = os.path.join(self.temp_dir, "out.json")
        out_stream = io.StringIO()

        doc_maj = {"status": "ok", "value": 100}
        doc_dis = {"status": "ok", "value": 200}

        s1 = self._create_fake_seed_script("s1", json.dumps(doc_maj))
        s2 = self._create_fake_seed_script("s2", json.dumps(doc_maj))
        s3 = self._create_fake_seed_script("s3", json.dumps(doc_dis))

        command_map = {"s1": s1, "s2": s2, "s3": s3}

        res = run_comparison(
            seeds=["s1", "s2", "s3"],
            fixtures_dir=self.fixtures_dir,
            reps=3,
            out_path=out_json,
            command_map=command_map,
            env={"CHROME_BIN": "/usr/bin/chrome"},
            stdout_stream=out_stream
        )
        self.assertEqual(res, 0)

        with open(out_json, "r") as f:
            data = json.load(f)

        p_rep = data["pages"]["page1"]
        self.assertEqual(p_rep["conformance"]["dissenting_seeds"], ["s3"])
        details = p_rep["dissenting_details"]["s3"]
        self.assertEqual(details["first_differing_json_path"], "$.value")
        self.assertIsInstance(details["first_differing_byte_offset"], int)

    def test_nondeterministic_seed(self):
        out_json = os.path.join(self.temp_dir, "out.json")
        out_stream = io.StringIO()

        # Build a script that changes stdout each time based on a temp counter
        counter_file = os.path.join(self.temp_dir, "counter.txt")

        script_path = os.path.join(self.temp_dir, "nondet.py")
        code = f"""import sys, json, argparse
parser = argparse.ArgumentParser()
parser.add_argument("--timing")
args, _ = parser.parse_known_args()
if args.timing:
    with open(args.timing, "w") as f:
        json.dump({{"launch_ms": 5, "steps_ms": [10]}}, f)

c = 0
try:
    with open("{counter_file}", "r") as f:
        c = int(f.read().strip())
except Exception:
    pass
with open("{counter_file}", "w") as f:
    f.write(str(c + 1))

sys.stdout.write(json.dumps({{"val": c}}))
"""
        with open(script_path, "w") as f:
            f.write(code)

        s1 = self._create_fake_seed_script("s1", json.dumps({"val": 0}))
        s2 = self._create_fake_seed_script("s2", json.dumps({"val": 0}))
        nd = [sys.executable, script_path]

        command_map = {"s1": s1, "s2": s2, "nd": nd}

        res = run_comparison(
            seeds=["s1", "s2", "nd"],
            fixtures_dir=self.fixtures_dir,
            reps=3,
            out_path=out_json,
            command_map=command_map,
            env={"CHROME_BIN": "/usr/bin/chrome"},
            stdout_stream=out_stream
        )
        self.assertEqual(res, 0)

        with open(out_json, "r") as f:
            data = json.load(f)

        p_rep = data["pages"]["page1"]
        self.assertFalse(p_rep["determinism"]["nd"])
        self.assertIn("nd", p_rep["conformance"]["dissenting_seeds"])

    def test_seed_exits_non_zero(self):
        out_json = os.path.join(self.temp_dir, "out.json")
        out_stream = io.StringIO()

        s1 = self._create_fake_seed_script("s1", json.dumps({"status": "ok"}))
        s_err = self._create_fake_seed_script("s_err", json.dumps({"status": "error"}), exit_code=1)

        command_map = {"s1": s1, "s_err": s_err}

        res = run_comparison(
            seeds=["s1", "s_err"],
            fixtures_dir=self.fixtures_dir,
            reps=2,
            out_path=out_json,
            command_map=command_map,
            env={"CHROME_BIN": "/usr/bin/chrome"},
            stdout_stream=out_stream
        )
        self.assertEqual(res, 0)

        with open(out_json, "r") as f:
            data = json.load(f)

        p_rep = data["pages"]["page1"]
        self.assertFalse(p_rep["determinism"]["s_err"])
        self.assertIn("s_err", p_rep["conformance"]["dissenting_seeds"])


if __name__ == "__main__":
    unittest.main()
