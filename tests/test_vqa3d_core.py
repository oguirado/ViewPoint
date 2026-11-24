"""Hosted CI runs the component's recorded visual QA suite without 3D runtimes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class VQA3DCore(unittest.TestCase):
    def test_component_recorded_suite(self):
        component = Path(__file__).resolve().parents[1] / "components/blender-visual-qa"
        with tempfile.TemporaryDirectory(prefix="vqa3d-tests-", dir="/dev/shm") as ram_root:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                 "-p", "test_vqa3d*.py"],
                cwd=component,
                env=dict(os.environ, VQA_RAMWS_ROOT=ram_root),
                capture_output=True, text=True, timeout=880,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
