"""REPIN B: config/vision.json pins the tracked ve-sealed-1-law2 wheel; a law1 (or any other) wheel is refused fail-closed.

python3 -m unittest tests.test_repin_law2 -v
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from blender_visual_qa.truth import pin as P  # noqa: E402

LAW2_SHA = "ebcf3854d14ab77e1ae7b6d65fd88ed07e417cef920e3c2b7f6b4149e7416e89"
LAW1_SHA = "de745d6ed2074334ad97a77f2c4f6b2847d4bdc3e5739979c563cc8604f8cb94"
TRACKED = ROOT / "tools" / "wheels" / "vision_engine-1.1.0-py3-none-any.whl"
LAW1_SCRATCH = ROOT / "build" / "room_B1" / "wheels" / "vision_engine-1.1.0-py3-none-any.whl"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RepinLaw2(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="repin_", dir=ROOT / "build" if (ROOT / "build").is_dir() else None))
        self.addCleanup(shutil.rmtree, self.td, True)
        self.cfg = json.loads((ROOT / "config" / "vision.json").read_text())

    def cfg_with(self, wheel, want):
        p = self.td / "vision.json"
        p.write_text(json.dumps({"engine_path": str(wheel), "pin": {"wheel_sha256": want}}))
        return p

    def test_pin_fields(self):
        pin = self.cfg["pin"]
        self.assertEqual((pin["tag"], pin["commit"], pin["wheel_sha256"]),
                         ("ve-sealed-1-law2", "bc1c8f6ef065921428b276a50937d8103979eac0", LAW2_SHA))

    def test_pin_sha_equals_tracked_wheel_sha(self):
        self.assertEqual(self.cfg["engine_path"], "tools/wheels/vision_engine-1.1.0-py3-none-any.whl")
        self.assertTrue(TRACKED.is_file())
        self.assertEqual(sha(TRACKED), self.cfg["pin"]["wheel_sha256"])
        self.assertEqual(P.configured_wheel(), str(ROOT / self.cfg["engine_path"]))

    def test_engine_path_not_scratch(self):
        self.assertFalse(self.cfg["engine_path"].startswith("build/"))

    def test_adopted_readoption(self):
        r = json.loads((ROOT / "tools" / "ADOPTED.json").read_text())["wheel_readoption"]
        self.assertEqual((r["old_sha256"], r["new_sha256"], r["wave"]), (LAW1_SHA, LAW2_SHA, "REPIN"))

    def test_other_wheel_refused(self):
        bad = self.td / "other.whl"
        bad.write_bytes(TRACKED.read_bytes() + b"\0")
        with self.assertRaises(P.PinError) as cm:
            P.configured_wheel(self.cfg_with(bad, LAW2_SHA))
        self.assertIn("E_PIN", str(cm.exception))
        self.assertIn("hash mismatch", str(cm.exception))

    @unittest.skipUnless(LAW1_SCRATCH.is_file() and sha(LAW1_SCRATCH) == LAW1_SHA, "law1 wheel scratch copy absent")
    def test_law1_wheel_refused_under_law2_pin(self):
        with self.assertRaises(P.PinError) as cm:
            P.configured_wheel(self.cfg_with(LAW1_SCRATCH, LAW2_SHA))
        self.assertIn(LAW1_SHA, str(cm.exception))
        self.assertEqual(P.configured_wheel(self.cfg_with(TRACKED, LAW2_SHA)), str(TRACKED))


if __name__ == "__main__":
    unittest.main()
