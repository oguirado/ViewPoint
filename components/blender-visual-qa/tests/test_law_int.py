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

    def test_params_proof_tracks_historical_wheel(self):
        proof = json.loads((ROOT / "tests/golden/truth/params_change_proof.json").read_text())
        adopted = json.loads((ROOT / "tools/ADOPTED.json").read_text())
        historical = adopted["wheel_notice_repair"]
        self.assertEqual(proof["engine_new"], "8dec8e83768afef63445b03476c6ed673f04c5bc")
        self.assertEqual(proof["new_engine_wheel"], historical["path"])
        notice = proof["repin_wheel_notice"]
        self.assertEqual(notice["from_wheel_sha256"], proof["new_wheel_sha256"])
        self.assertEqual(notice["to_wheel_sha256"], historical["new_sha256"])
        self.assertTrue(proof["repin_law1"]["params_unchanged"])


if __name__ == "__main__":
    unittest.main()
