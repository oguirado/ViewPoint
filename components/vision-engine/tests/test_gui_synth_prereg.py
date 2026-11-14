"""P1 W2 prereg: seal/verify, tamper of one byte of scenes.py in a scratch copy -> E_PREREG_SHA rc 5."""
import json
import os
import shutil
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = ("rng", "font", "raster", "scenes", "gen", "labels")


class TestPrereg(unittest.TestCase):
    def test_schema_and_pinned_fields(self):
        p = json.load(open(os.path.join(REPO, "eval/gui_synth/PREREG.json")))
        self.assertEqual(p["schema"], "vqa.gui_synth.prereg/v1")
        self.assertEqual(p["card"], "P1")
        self.assertEqual(p["splits"], {"DEV": [0, 64], "GATE": [1000000, 1000032], "TEST": [2000000, 2000032]})
        self.assertEqual(p["alpha"], "1/20")
        self.assertEqual(p["frames_per_scene"], 120)
        for key in ("sources", "sources_tree_sha256", "kinds", "primary", "guards", "dev_sha_table_sha256"):
            self.assertIn(key, p)

    def test_verify_current_tree(self):
        from eval.gui_synth import prereg
        self.assertTrue(prereg.verify())

    def test_gate_test_ranges_ok_when_sealed(self):
        from eval.gui_synth import splits
        splits.check("GATE", [1_000_000, 1_000_031], "evaluate")
        splits.check("TEST", [2_000_000, 2_000_031], "evaluate")

    def test_tampered_scenes_refused(self):
        from eval.gui_synth import prereg
        with tempfile.TemporaryDirectory(dir="/dev/shm") as d:
            shutil.copytree(os.path.join(REPO, "eval"), os.path.join(d, "eval"), ignore=shutil.ignore_patterns("__pycache__", "results"))
            f = os.path.join(d, "eval/gui_synth/scenes.py")
            with open(f, "ab") as fh:
                fh.write(b"\n#x")
            with self.assertRaises(Exception) as cm:
                prereg.verify(os.path.join(d, "eval/gui_synth/PREREG.json"), root=d)
            self.assertEqual(getattr(cm.exception, "code", None), "E_PREREG_SHA")
            self.assertEqual(getattr(cm.exception, "rc", None), 5)


if __name__ == "__main__":
    unittest.main()
