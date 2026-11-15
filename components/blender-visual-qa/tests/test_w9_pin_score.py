"""W9 P (BFX9d coupling): truth/score.py must import the hash-pinned engine wheel, never a live checkout."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config/vision.json").read_text())
WHEEL = Path(CFG["engine_path"])
WHEEL = WHEEL if WHEEL.is_absolute() else ROOT / WHEEL
PROBE = ("import sys;from blender_visual_qa.truth import score;import visioncore;from visioncore import params;"
         "import hashlib,json;print(json.dumps([visioncore.__file__,params.__file__,"
         "hashlib.sha256(json.dumps(params.effective(None),sort_keys=True,default=str).encode()).hexdigest()]))")


def _dirty_engine(dst):
    """Copy of the pinned sources with one default changed: stands in for a dirty live vision-engine tree."""
    import zipfile
    with zipfile.ZipFile(WHEEL) as z:
        z.extractall(dst)
    p = Path(dst) / "visioncore/params.py"
    text = p.read_text()
    assert "H_DEAD" in text
    p.write_text(text + "\nH_DEAD_DIRTY_MARKER = 1\n")
    return dst


def _probe(extra_path=None, code=PROBE):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("VISIONCORE_PATH", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (extra_path, str(ROOT)) if p)
    return subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, env=env, text=True,
                          capture_output=True, timeout=60)


class ScorePinsWheel(unittest.TestCase):
    def test_no_live_checkout_literal(self):
        src = (ROOT / "blender_visual_qa/truth/score.py").read_text()
        self.assertNotIn("/vision-engine", src)

    def test_dirty_live_tree_on_path_does_not_change_results(self):
        scratch = ROOT / "build/room_P"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            dirty = _dirty_engine(Path(tmp) / "live")
            clean = _probe()
            self.assertEqual(clean.returncode, 0, clean.stderr)
            with_dirty = _probe(str(dirty))
            self.assertEqual(with_dirty.returncode, 0, with_dirty.stderr)
            a, b = json.loads(clean.stdout), json.loads(with_dirty.stdout)
            self.assertTrue(a[0].startswith(str(WHEEL) + "/"), a)
            self.assertEqual(a, b)

    def test_preloaded_live_engine_is_refused(self):
        scratch = ROOT / "build/room_P"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            dirty = _dirty_engine(Path(tmp) / "live")
            code = "import sys;sys.path.insert(0,%r);import visioncore;" % str(dirty) + PROBE
            r = _probe(code=code)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("E_PIN", r.stderr)

    def test_wrong_wheel_hash_is_refused(self):
        scratch = ROOT / "build/room_P"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            repo = Path(tmp) / "repo"
            (repo / "config").mkdir(parents=True)
            shutil.copytree(ROOT / "blender_visual_qa", repo / "blender_visual_qa",
                            ignore=shutil.ignore_patterns("__pycache__"))
            cfg = json.loads(json.dumps(CFG))
            cfg["engine_path"] = str(WHEEL)
            cfg["pin"]["wheel_sha256"] = "0" * 64
            (repo / "config/vision.json").write_text(json.dumps(cfg))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo))
            r = subprocess.run([sys.executable, "-B", "-c", "from blender_visual_qa.truth import score"],
                               cwd=repo, env=env, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("E_PIN", r.stderr)

    def test_configless_scratch_tree_never_falls_back_to_live_checkout(self):
        scratch = ROOT / "build/room_P"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(ROOT / "blender_visual_qa", repo / "blender_visual_qa",
                            ignore=shutil.ignore_patterns("__pycache__"))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo))
            env.pop("VISIONCORE_PATH", None)
            env.pop("BVQA_TEST_ENGINE", None)
            env.pop("BT_EXPECT_ENGINE", None)
            r = subprocess.run([sys.executable, "-B", "-c", "from blender_visual_qa.truth import score"],
                               cwd=repo, env=env, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("E_PIN", r.stderr)  # JW9c-2: fail closed, never a silent unpinned import


if __name__ == "__main__":
    unittest.main()
