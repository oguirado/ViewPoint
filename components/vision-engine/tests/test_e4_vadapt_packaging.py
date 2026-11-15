"""E4 N2: JC-7 gui nested screenshot path, JC-8 wheel contains every subpackage."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from vadapt.gui import GuiAdapter
from vadapt.synth.pngenc import encode_png

ROOT = Path(__file__).resolve().parent.parent


class GuiNestedPath(unittest.TestCase):
    def test_gui_nested_path(self):  # JC-7
        with tempfile.TemporaryDirectory() as t:
            run, out = Path(t) / "run", Path(t) / "out"
            for sub in ("sub", "other"):
                (run / "screens" / sub).mkdir(parents=True)
            for k, rel in enumerate(("sub/a.png", "other/a.png")):
                (run / "screens" / rel).write_bytes(encode_png(np.full((48, 64, 3), 10 * k + 5, np.uint8)))
            (run / "steps.jsonl").write_text("".join(
                json.dumps({"step": k, "file": rel, "t_us": k}) + "\n" for k, rel in enumerate(("sub/a.png", "other/a.png"))))
            m = GuiAdapter().adapt(run, out)
            files = [r["file"] for r in m["frames"]]
            self.assertEqual(len(set(files)), 2)  # duplicate basenames stay distinct
            for r in json.loads((out / "frames.manifest.json").read_text())["frames"]:
                self.assertTrue((out / r["file"]).is_file(), r["file"])
            self.assertTrue(any(f.endswith("screens/sub/a.png") for f in files))


class WheelContents(unittest.TestCase):
    def test_wheel_contains_subpackages(self):  # JC-8
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "src"
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
                "build", ".git", "__pycache__", "eval", "tests", "*.egg-info"))
            r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q",
                                "-w", str(Path(t) / "w"), str(src)], capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-800:])
            whl = next((Path(t) / "w").glob("*.whl"))
            names = set(zipfile.ZipFile(whl).namelist())
            for need in ("visioncore/__init__.py", "visioncore/schemas/vqa.frame.v1.schema.json", "vmem/__init__.py",
                         "vadapt/__init__.py", "vadapt/base.py", "vadapt/synth/__init__.py",
                         "vadapt/godot/__init__.py", "vadapt/gui/__init__.py", "vadapt/blender/__init__.py"):
                self.assertIn(need, names)


if __name__ == "__main__":
    unittest.main()
