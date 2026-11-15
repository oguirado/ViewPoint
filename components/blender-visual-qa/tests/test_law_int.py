"""STORAGE LAW integration (lane F): router catalogue fresh after the LAW code, golden proof pinned to the law1 wheel."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LawIntegration(unittest.TestCase):
    def test_router_catalogue_is_fresh(self):
        p = subprocess.run([sys.executable, "-B", str(ROOT / "catalogue/router/build_router_catalogue.py"), "--root", str(ROOT), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_params_proof_tracks_pin(self):
        proof = json.loads((ROOT / "tests/golden/truth/params_change_proof.json").read_text())
        pin = json.loads((ROOT / "config/vision.json").read_text())["pin"]
        self.assertEqual((proof["engine_new"], proof["new_wheel_sha256"]), (pin["commit"], pin["wheel_sha256"]))
        self.assertTrue(proof["repin_law1"]["params_unchanged"])


if __name__ == "__main__":
    unittest.main()
