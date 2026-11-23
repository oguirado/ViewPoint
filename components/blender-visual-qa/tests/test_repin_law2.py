"""Verify the active Vision wheel pin and retain the historical repin chain.

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

MONOREPO_SHA = "e02622a34169c976ea884a50391692b39cd8b09950b7c8d4d90efaf8b5514a97"
WHEEL_NOTICE_SHA = "b9574df83e41c283a4c29d585ac5b81b9f005876f53fac84e24df5c5a713a372"
PREV_SHA = "ebcf3854d14ab77e1ae7b6d65fd88ed07e417cef920e3c2b7f6b4149e7416e89"
LAW1_SHA = "de745d6ed2074334ad97a77f2c4f6b2847d4bdc3e5739979c563cc8604f8cb94"
WHEEL_PATH = "tools/wheels/vision_engine-1.1.1-py3-none-any.whl"
WHEEL_SHA = "4efce651bab7d04f04c70fdf3b40e360f74aa2e296d28bdb36aaf7d72bb05480"
SOURCE_COMMIT = "c3b0f53560dcb45e6c503d95e2e848cd3b8f16b7"
TOOLING_COMMIT = "8dec8e83768afef63445b03476c6ed673f04c5bc"
TRACKED = ROOT / WHEEL_PATH
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
        self.assertEqual(pin["tag"], SOURCE_COMMIT[:8])
        self.assertEqual(pin["commit"], SOURCE_COMMIT)
        self.assertEqual(pin["wheel_sha256"], WHEEL_SHA)
        self.assertEqual((pin["version"], pin["api_version"]), ("1.1.1", "1.1.0"))
        self.assertEqual(self.cfg["tooling_commit"], TOOLING_COMMIT)

    def test_pin_sha_equals_tracked_wheel_sha(self):
        self.assertEqual(self.cfg["engine_path"], WHEEL_PATH)
        self.assertTrue(TRACKED.is_file())
        self.assertEqual(sha(TRACKED), self.cfg["pin"]["wheel_sha256"])
        self.assertEqual(P.configured_wheel(), str(ROOT / self.cfg["engine_path"]))

    def test_engine_path_not_scratch(self):
        self.assertFalse(self.cfg["engine_path"].startswith("build/"))

    def test_adopted_readoption(self):
        adopted = json.loads((ROOT / "tools" / "ADOPTED.json").read_text())
        r = adopted["wheel_readoption"]
        self.assertEqual((r["old_sha256"], r["new_sha256"], r["wave"]), (PREV_SHA, MONOREPO_SHA, "PR1"))
        self.assertEqual(adopted["wheel_notice_repair"], {
            "old_sha256": MONOREPO_SHA,
            "new_sha256": WHEEL_NOTICE_SHA,
            "path": "tools/wheels/vision_engine-1.1.0-py3-none-any.whl",
            "wave": "PR1-WHEEL-IMPLEMENT",
        })
        self.assertEqual(adopted["wheel_source_repin"], {
            "api_version": "1.1.0", "commit": SOURCE_COMMIT, "new_sha256": WHEEL_SHA,
            "old_sha256": WHEEL_NOTICE_SHA, "path": WHEEL_PATH,
            "tooling_commit": TOOLING_COMMIT, "version": "1.1.1",
        })

    def test_other_wheel_refused(self):
        bad = self.td / "other.whl"
        bad.write_bytes(TRACKED.read_bytes() + b"\0")
        with self.assertRaises(P.PinError) as cm:
            P.configured_wheel(self.cfg_with(bad, WHEEL_SHA))
        self.assertIn("E_PIN", str(cm.exception))
        self.assertIn("hash mismatch", str(cm.exception))

    @unittest.skipUnless(LAW1_SCRATCH.is_file() and sha(LAW1_SCRATCH) == LAW1_SHA, "law1 wheel scratch copy absent")
    def test_law1_wheel_refused_under_law2_pin(self):
        with self.assertRaises(P.PinError) as cm:
            P.configured_wheel(self.cfg_with(LAW1_SCRATCH, WHEEL_SHA))
        self.assertIn(LAW1_SHA, str(cm.exception))
        self.assertEqual(P.configured_wheel(self.cfg_with(TRACKED, WHEEL_SHA)), str(TRACKED))


if __name__ == "__main__":
    unittest.main()
