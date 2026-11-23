"""Hosted CI runs the component's recorded visual QA suite without 3D runtimes."""
from pathlib import Path
import subprocess
import sys
import unittest


class VQA3DCore(unittest.TestCase):
    def test_component_recorded_suite(self):
        component = Path(__file__).resolve().parents[1] / "components/blender-visual-qa"
        result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                                 "-p", "test_vqa3d*.py"], cwd=component,
                                capture_output=True, text=True, timeout=880)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
