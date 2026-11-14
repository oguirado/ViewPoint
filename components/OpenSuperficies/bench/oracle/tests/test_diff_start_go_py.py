"""Differential test comparing Go diffstart (ParseStart & ParseStartInfo) with Python sampler.read_start."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from bench.oracle import sampler

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CORPUS_PATH = os.path.join(REPO_ROOT, "tools", "diffstart", "corpus.json")


class TestDiffStartGoPy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The oracle mutant gate (scripts/cu/tests/test_oracle_gates.py) runs bench/oracle/tests in a scratch copy that holds
        # only docker/cu, bench, vision and scripts/cu; this Go differential needs the module root. The full repo always has go.mod.
        if not os.path.isfile(os.path.join(REPO_ROOT, "go.mod")):
            raise unittest.SkipTest("needs the full repo (go.mod, tools/diffstart, src/cubook); scratch mutant copy")
        cls.temp_dir = tempfile.mkdtemp(prefix="diffstart_test_")
        cls.bin_path = os.path.join(cls.temp_dir, "diffstart")
        env = dict(os.environ)
        env.pop("OS_CU_TEST", None)
        cmd = ["go", "build", "-p", "5", "-o", cls.bin_path, "./tools/diffstart"]
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            shutil.rmtree(cls.temp_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to build tools/diffstart: {proc.stderr}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_corpus_differential(self):
        with open(CORPUS_PATH, "r", encoding="utf-8") as f:
            corpus = json.load(f)

        self.assertGreaterEqual(len(corpus), 30, "Corpus must have at least 30 cases")

        payloads = [case["payload"] for case in corpus]

        input_data = json.dumps(payloads).encode("utf-8")
        env = dict(os.environ)
        env.pop("OS_CU_TEST", None)
        proc = subprocess.run(
            [self.bin_path],
            input=input_data,
            capture_output=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"tools/diffstart process failed with exit code {proc.returncode}: {proc.stderr.decode()}",
        )

        go_lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
        self.assertEqual(
            len(go_lines),
            len(corpus),
            f"Go output lines count ({len(go_lines)}) != corpus length ({len(corpus)})",
        )

        for idx, (case, go_line) in enumerate(zip(corpus, go_lines)):
            name = case.get("name", f"case_{idx}")
            expected_go_ok = case["go_ok"]
            expected_py_ok = case["py_ok"]

            go_res = json.loads(go_line)
            actual_go_ok = go_res.get("ok")
            self.assertEqual(
                actual_go_ok,
                expected_go_ok,
                f"[{name}] Go verdict mismatch: expected {expected_go_ok}, got {actual_go_ok}",
            )

            raw_payload = base64.b64decode(case["payload"])
            run_dir = tempfile.mkdtemp(prefix=f"run_{idx}_", dir=self.temp_dir)
            start_path = os.path.join(run_dir, "START")
            with open(start_path, "wb") as f:
                f.write(raw_payload)

            actual_py_ok = False
            try:
                sampler.read_start(run_dir)
                actual_py_ok = True
            except Exception:
                actual_py_ok = False

            self.assertEqual(
                actual_py_ok,
                expected_py_ok,
                f"[{name}] Python verdict mismatch: expected {expected_py_ok}, got {actual_py_ok}",
            )

            if expected_go_ok != expected_py_ok:
                reason = case.get("divergence_reason")
                self.assertTrue(
                    reason and isinstance(reason, str) and len(reason.strip()) > 0,
                    f"[{name}] Case where go_ok ({expected_go_ok}) differs from py_ok ({expected_py_ok}) must have a non-empty divergence_reason",
                )


if __name__ == "__main__":
    unittest.main()
