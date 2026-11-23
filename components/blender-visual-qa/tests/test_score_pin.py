"""The configured wheel wins over score's legacy checkout fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PinnedScore(unittest.TestCase):
    def test_configured_engine_precedes_checkout(self):
        cfg = json.loads((ROOT / 'config/vision.json').read_text())
        wheel = Path(cfg['engine_path'])
        wheel = wheel if wheel.is_absolute() else ROOT / wheel
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'config'), PYTHONDONTWRITEBYTECODE='1')
        r = subprocess.run([sys.executable, '-B', '-c',
                            'from blender_visual_qa.truth import score; import visioncore; print(visioncore.__file__)'],
                           cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().startswith(str(wheel)), r.stdout)

    def test_startup_refuses_wrong_wheel_hash(self):
        scratch = ROOT / 'build/room_I'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            config = Path(tmp) / 'config'
            config.mkdir()
            shutil.copy2(ROOT / 'config/sitecustomize.py', config)
            cfg = json.loads((ROOT / 'config/vision.json').read_text())
            wheel = Path(cfg['engine_path'])
            cfg['engine_path'] = str(wheel if wheel.is_absolute() else ROOT / wheel)
            cfg['pin']['wheel_sha256'] = '0' * 64
            (config / 'vision.json').write_text(json.dumps(cfg))
            env = dict(os.environ, PYTHONPATH=str(config), PYTHONDONTWRITEBYTECODE='1')
            r = subprocess.run([sys.executable, '-B', '-c', 'print("MUST_NOT_RUN")'],
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2)
            self.assertNotIn('MUST_NOT_RUN', r.stdout)
            self.assertIn('E_PIN', r.stderr)
