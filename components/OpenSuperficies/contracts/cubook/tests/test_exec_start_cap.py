"""contracts/cubook/tests/test_exec_start_cap.py: START file cap and validation tests for srf-cu-exec."""
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

FIXTURE_DIR = os.path.join(ROOT, "fixtures", "exec", "wl_v3_hscroll_ok")

_BIN_DIR = None


def get_bin_dir():
    global _BIN_DIR
    if _BIN_DIR is not None and os.path.exists(_BIN_DIR):
        return _BIN_DIR
    _BIN_DIR = tempfile.mkdtemp(prefix="cubook_exec_cap_bin_")
    env = dict(os.environ, PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin")
    env.pop("OS_CU_TEST", None)
    p = subprocess.run(
        ["go", "build", "-p", "5", "-o", os.path.join(_BIN_DIR, "srf-cu-exec"), "./cmd/srf-cu-exec"],
        cwd=REPO,
        env=env,
        capture_output=True,
        timeout=120,
    )
    if p.returncode != 0:
        shutil.rmtree(_BIN_DIR)
        _BIN_DIR = None
        raise RuntimeError(f"Failed to build srf-cu-exec: {p.stderr.decode()}")
    return _BIN_DIR


class TestExecStartCap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin_dir = get_bin_dir()
        cls.bin_path = os.path.join(cls.bin_dir, "srf-cu-exec")

        with open(os.path.join(FIXTURE_DIR, "argv.json")) as f:
            cls.argv = json.load(f)
        with open(os.path.join(FIXTURE_DIR, "expected_rc")) as f:
            cls.expected_rc = int(f.read().strip())
        with open(os.path.join(FIXTURE_DIR, "expected_stdout")) as f:
            cls.expected_stdout = f.read()
        with open(os.path.join(FIXTURE_DIR, "files", "START"), "rb") as f:
            cls.fixture_start_bytes = f.read()

    @classmethod
    def tearDownClass(cls):
        if _BIN_DIR and os.path.exists(_BIN_DIR):
            shutil.rmtree(_BIN_DIR)

    def run_exec_with_start(self, setup_start_fn):
        work_dir = tempfile.mkdtemp(prefix="cubook_exec_start_case_")
        try:
            # Copy pre.json into work_dir
            shutil.copy(os.path.join(FIXTURE_DIR, "files", "pre.json"), os.path.join(work_dir, "pre.json"))

            start_path = os.path.join(work_dir, "START")
            setup_start_fn(start_path, work_dir)

            env = dict(os.environ, OS_CU_TEST="1", PATH=os.environ.get("PATH", "") + ":/usr/local/go/bin")
            p = subprocess.run(
                [self.bin_path] + self.argv,
                cwd=work_dir,
                env=env,
                capture_output=True,
                timeout=60,
            )
            act_path = os.path.join(work_dir, "act.jsonl")
            act_exists = os.path.exists(act_path)
            return p.returncode, p.stdout.decode(), p.stderr.decode(), act_exists
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def test_padded_4096_bytes_passes(self):
        padded = self.fixture_start_bytes.rstrip(b"\r\n") + b" " * (4096 - len(self.fixture_start_bytes.rstrip(b"\r\n")))
        self.assertEqual(len(padded), 4096)

        def setup(start_path, work_dir):
            with open(start_path, "wb") as f:
                f.write(padded)

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, self.expected_rc)
        self.assertEqual(stdout, self.expected_stdout)
        self.assertTrue(act_exists, "act.jsonl should be written for valid 4096-byte START")

    def test_padded_4097_bytes_fails(self):
        padded = self.fixture_start_bytes.rstrip(b"\r\n") + b" " * (4097 - len(self.fixture_start_bytes.rstrip(b"\r\n")))
        self.assertEqual(len(padded), 4097)

        def setup(start_path, work_dir):
            with open(start_path, "wb") as f:
                f.write(padded)

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")

    def test_padded_65536_bytes_fails(self):
        padded = self.fixture_start_bytes.rstrip(b"\r\n") + b" " * (65536 - len(self.fixture_start_bytes.rstrip(b"\r\n")))
        self.assertEqual(len(padded), 65536)

        def setup(start_path, work_dir):
            with open(start_path, "wb") as f:
                f.write(padded)

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")

    def test_empty_start_fails(self):
        def setup(start_path, work_dir):
            with open(start_path, "wb") as f:
                f.write(b"")

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")

    def test_symlink_start_fails(self):
        def setup(start_path, work_dir):
            real_start = os.path.join(work_dir, "real_START")
            with open(real_start, "wb") as f:
                f.write(self.fixture_start_bytes)
            os.symlink(real_start, start_path)

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")

    def test_fifo_start_fails(self):
        def setup(start_path, work_dir):
            os.mkfifo(start_path)

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")

    def test_hardlink_start_fails(self):
        def setup(start_path, work_dir):
            with open(start_path, "wb") as f:
                f.write(self.fixture_start_bytes)
            os.link(start_path, os.path.join(work_dir, "second_link"))

        rc, stdout, stderr, act_exists = self.run_exec_with_start(setup)
        self.assertNotEqual(rc, 1, "rc must never be 1")
        self.assertEqual(rc, 5)
        self.assertIn("start_unsafe", stderr)
        self.assertFalse(act_exists, "act.jsonl should not be written on start failure")


if __name__ == "__main__":
    unittest.main()
