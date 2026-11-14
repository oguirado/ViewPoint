"""Hardening for READY finding RD4-04: CU_HARVEST_SUB validation in harvest.sh."""

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OS_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
HARVEST_SH = os.path.join(OS_ROOT, "docker", "cu", "diag", "harvest.sh")


class TestHarvestSubValidation(unittest.TestCase):
    def setUp(self):
        self.stub_dir = tempfile.mkdtemp()
        self.docker_stub = os.path.join(self.stub_dir, "docker")
        self.calls_log = os.path.join(self.stub_dir, "docker_calls.log")
        with open(self.docker_stub, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
            f.write(f'echo "$@" >> "{self.calls_log}"\n')
            f.write("exit 0\n")
        os.chmod(self.docker_stub, 0o755)

        self.out_dir = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env["PATH"] = self.stub_dir + os.path.pathsep + self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.stub_dir, ignore_errors=True)
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _assert_docker_not_called(self):
        self.assertFalse(
            os.path.exists(self.calls_log) and os.path.getsize(self.calls_log) > 0,
            "docker stub was called when it should not have been",
        )

    def test_invalid_cu_harvest_sub_returns_rc2(self):
        invalid_subs = ["abc", "1e5", "-1", "1234567890", " "]
        for sub in invalid_subs:
            with self.subTest(sub=sub):
                env = dict(self.env)
                env["CU_HARVEST_SUB"] = sub
                res = subprocess.run(
                    ["bash", HARVEST_SH, "vqa-valid-container", self.out_dir],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 2, f"Expected rc 2 for CU_HARVEST_SUB={sub!r}, got {res.returncode}")
                self.assertNotEqual(res.returncode, 1, "Exit code must never be 1")
                self.assertIn("harvest: bad CU_HARVEST_SUB", res.stderr)
                self.assertEqual(os.listdir(self.out_dir), [], "OUTDIR must stay empty on validation failure")
                self._assert_docker_not_called()

    def test_usage_and_empty_container_name_return_rc2(self):
        # 1-argument usage call
        res_usage = subprocess.run(
            ["bash", HARVEST_SH, "vqa-valid-container"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_usage.returncode, 2)
        self.assertNotEqual(res_usage.returncode, 1)
        self.assertIn("usage: harvest.sh NAME OUTDIR", res_usage.stderr)
        self._assert_docker_not_called()

        # Empty container name call
        res_empty = subprocess.run(
            ["bash", HARVEST_SH, "", self.out_dir],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_empty.returncode, 2)
        self.assertNotEqual(res_empty.returncode, 1)
        self.assertIn("usage: harvest.sh NAME OUTDIR", res_empty.stderr)
        self.assertEqual(os.listdir(self.out_dir), [])
        self._assert_docker_not_called()

        # Empty container name call with CU_HARVEST_SUB=""
        env_empty_sub = dict(self.env, CU_HARVEST_SUB="")
        res_empty_sub = subprocess.run(
            ["bash", HARVEST_SH, "", self.out_dir],
            env=env_empty_sub,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_empty_sub.returncode, 2)
        self.assertNotEqual(res_empty_sub.returncode, 1)
        self.assertEqual(os.listdir(self.out_dir), [])
        self._assert_docker_not_called()


if __name__ == "__main__":
    unittest.main()
