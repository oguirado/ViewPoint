"""Real geometry boundary controls; no replacement evaluator or fabricated PASS."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from tools.pipeline import stages, engine
from tools.mesh import api as mesh
from test_f2u1_mesh import glb, policy


class RegressionBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AF_E2E_OUTPUT"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw.glb"
        self.raw.write_bytes(glb())
        self.pol = policy(sample_budget=512, raster_resolution=96)
        core.atomic_write_json(self.root / "policy.json", self.pol)
        # NativeStages only checks the PNG signature before geometry operations.
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.backend = stages.NativeStages(source=self.raw, reference=self.reference, provider="byo",
            options={"mesh_policy": self.root / "policy.json", "method": "identity",
                     "target_faces": None, "seed": 42, "timeout_s": 30})

    def test_native_identity_passes_without_test_comparator(self):
        out = self.root / "stage"
        out.mkdir()
        with mock.patch.object(stages.topology, "run", side_effect=AssertionError("legacy boundary")):
            result = self.backend.stage_topology(out, self.raw)
        self.assertEqual(result["rc"], 0)
        self.assertEqual((out / result["subject"]).read_bytes(), self.raw.read_bytes())
        receipt = engine.load(out / "topology/master.regression.json")
        self.assertIsInstance(receipt["unavailable"], dict)
        self.assertEqual(receipt["metrics"]["hausdorff_relative_ppm"], 0)

    def test_real_different_geometry_fails_acceptance_not_contract(self):
        from tools.pipeline.topology_bridge import verify_regression
        candidate = self.root / "different.glb"
        candidate.write_bytes(glb([{"name": "body", "scale": [1, 1, 0.4]}]))
        rc, rec = mesh.compare_meshes(self.raw, candidate, policy=self.pol)
        self.assertEqual(rc, 5, rec)
        with self.assertRaises(engine.Refusal) as error:
            verify_regression(rc, rec, self.raw, candidate, self.pol)
        self.assertEqual(error.exception.rc, 5)

    def test_malformed_and_replayed_receipts_are_refused(self):
        from tools.pipeline.topology_bridge import verify_regression
        rc, rec = mesh.compare_meshes(self.raw, self.raw, policy=self.pol)
        self.assertEqual(rc, 0, rec)
        mutations = [("unavailable", [], 2), ("rc", True, 2),
                     ("candidate_sha256", "0" * 64, 4), ("toolchain_sha256", "0" * 64, 4)]
        for key, value, want in mutations:
            with self.subTest(key=key):
                bad = copy.deepcopy(rec)
                bad[key] = value
                with self.assertRaises(engine.Refusal) as error:
                    verify_regression(rc, bad, self.raw, self.raw, self.pol)
                self.assertEqual(error.exception.rc, want)
        bad = copy.deepcopy(rec)
        del bad["metrics"]["raw_diag_um"]
        with self.assertRaises(engine.Refusal) as error:
            verify_regression(rc, bad, self.raw, self.raw, self.pol)
        self.assertEqual(error.exception.rc, 2)


if __name__ == "__main__":
    unittest.main()
