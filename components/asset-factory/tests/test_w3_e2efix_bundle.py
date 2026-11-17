"""Measured CPU bake, semantic replay and immutable refusal controls."""
import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from tools.blender import bake
from tools.blender.glb_capture import run_owned
from tools.pipeline import bundle, engine, stages


class ProductionBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AF_E2E_OUTPUT"))
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        blender = shutil.which("blender")
        if blender is None:
            raise AssertionError("real CPU bake cannot see Blender")
        run_owned(["taskset", "-c", "0-15", "nice", "-n", "15", blender, "-b", "--factory-startup",
            "--threads", "2", "--python-exit-code", "2", "--python",
            str(ROOT / "tools/pipeline/fixture_blender.py"), "--", str(cls.root)], timeout_s=120, cwd=cls.root)
        cls.baked = cls.root / "baked"
        cls.result = bake.bake_bundle(cls.root / "prop.glb", cls.root / "prop.glb", cls.baked,
                                      res=64, timeout_s=90)
        if cls.result["rc"] != 0:
            raise AssertionError(cls.result)
        cls.policy = {"res": 64, "margin_px": 8, "cage_extrusion_um": 5000,
                      "max_stretch_permille": 4000, "timeout_s": 90}

    def verify(self, path=None, digest=None, **changes):
        kwargs = dict(expected_receipt_sha256=digest or self.result["receipt_sha256"],
            expected_high_sha256=core.sha256_file(self.root / "prop.glb"),
            expected_low_sha256=core.sha256_file(self.root / "prop.glb"),
            expected_policy=self.policy, timeout_s=90)
        kwargs.update(changes)
        return bundle.verify_bundle(path or self.baked, **kwargs)

    def altered(self, name, mutate, reseal=False):
        root = self.root / name
        shutil.copytree(self.baked, root)
        mutate(root)
        digest = self.result["receipt_sha256"]
        if reseal:
            rec = engine.load(root / "bundle_receipt.json")
            rec["files"] = [{"path": p.name, "sha256": core.sha256_file(p)}
                for p in sorted(root.iterdir()) if p.name != "bundle_receipt.json"]
            core.atomic_write_json(root / "bundle_receipt.json", rec)
            digest = core.sha256_file(root / "bundle_receipt.json")
        before = {p.name: p.read_bytes() for p in root.iterdir()}
        result = self.verify(root, digest)
        self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
        return result

    def test_real_production_bundle_passes_and_stays_map_scoped(self):
        result = self.verify()
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["scope"], "production-uv-maps")
        self.assertIs(result["production_evidence"], True)
        self.assertIn("appearance", result["pending_gates"])
        self.assertIn("vqa3d", result["pending_gates"])

    def test_tampered_map_stale_and_missing_member_incomplete(self):
        result = self.altered("tampered", lambda p: (p / "normal.png").write_bytes(b"tampered"))
        self.assertEqual(result["rc"], 4, result)
        result = self.altered("missing", lambda p: (p / "pbr_receipt.json").unlink())
        self.assertEqual(result["rc"], 3, result)
        result = self.altered("resealed-missing", lambda p: (p / "pbr_receipt.json").unlink(), reseal=True)
        self.assertEqual(result["rc"], 3, result)

    def test_resealed_broken_chain_still_refuses(self):
        def change(p):
            rec = engine.load(p / "uv_receipt.json")
            rec["prev_sha256"] = "0" * 64
            core.atomic_write_json(p / "uv_receipt.json", rec)
        result = self.altered("broken-chain", change, reseal=True)
        self.assertEqual(result["rc"], 4, result)

    def test_resealed_semantic_forgery_and_injected_runner_refuse(self):
        def change(p):
            rec = engine.load(p / "bake_receipt.json")
            rec["checks"]["coverage_ppm"] = 1
            core.atomic_write_json(p / "bake_receipt.json", rec)
            # Reseal every descendant link, so only semantic replay can see
            # this forgery. A broken hash chain would be a weaker control.
            for name, predecessor in (("uv", "bake"), ("orm", "uv"), ("pbr", "orm")):
                rec = engine.load(p / (name + "_receipt.json"))
                rec["prev_sha256"] = core.sha256_file(p / (predecessor + "_receipt.json"))
                core.atomic_write_json(p / (name + "_receipt.json"), rec)
        result = self.altered("forged-checks", change, reseal=True)
        self.assertEqual(result["rc"], 4, result)
        def inject(p):
            rec = engine.load(p / "bundle_receipt.json")
            rec["runner"] = {"kind": "injected", "identity": "scoped_runner"}
            core.atomic_write_json(p / "bundle_receipt.json", rec)
        result = self.altered("injected", inject, reseal=True)
        self.assertEqual(result["rc"], 3, result)

    def test_replayed_source_and_policy_refuse(self):
        self.assertEqual(self.verify(expected_high_sha256="0" * 64)["rc"], 4)
        policy = copy.deepcopy(self.policy)
        policy["res"] = 128
        self.assertEqual(self.verify(expected_policy=policy)["rc"], 4)

    def test_complete_geometry_prefix_never_forges_missing_acceptance(self):
        policy = {"sample_budget": 512, "raster_resolution": 96, "view_basis": "gltf-y-up-ortho4",
            "unit": "metre", "transform": "world", "weld_tolerance_um": 0,
            "allow_open": False, "require_uv": False, "component_allowlist": {"body": 1},
            "timeout_s": 60, "repair": {"max_passes": 1, "max_hole_edges": 0, "cull_max_permille": 0}}
        core.atomic_write_json(self.root / "policy.json", policy)
        options = {"mesh_policy": self.root / "policy.json", "timeout_s": 90, "method": "identity",
            "target_faces": None, "seed": 42, "texture_resolution": 64, "max_stretch": 4000,
            "required_checks": []}
        backend = stages.NativeStages(source=self.root / "prop.glb", reference=self.root / "reference.png",
            provider="byo", options=options)
        result = engine.Pipeline(self.root / "native-prefix", backend).run()
        self.assertEqual([row["rc"] for row in result["stages"][:6]], [0] * 6, result)
        self.assertEqual(result["rc"], 3, result)
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertIsNone(result["artifact"])
        # Relative inner evidence references survive the pipeline's atomic move.
        stage = engine.load(result["stages"][4]["receipt"]["path"])
        proof_path = next(Path(r["path"]) for r in stage["evidence"]
                          if Path(r["path"]).name == "production-verification.json")
        proof = engine.load(proof_path)
        for ref in proof["semantic_receipts"]:
            self.assertEqual(core.sha256_file(proof_path.parent / ref["path"]), ref["sha256"])

    def test_explicit_face_target_never_bypasses_real_regression(self):
        from tools.pipeline import topology_bridge
        policy = {"sample_budget": 512, "raster_resolution": 96, "view_basis": "gltf-y-up-ortho4",
            "unit": "metre", "transform": "world", "weld_tolerance_um": 0,
            "allow_open": False, "require_uv": False, "component_allowlist": {"body": 1}, "timeout_s": 60}
        original = (self.root / "prop.glb").read_bytes()
        with self.assertRaises(engine.Refusal) as error:
            topology_bridge.run_candidate(self.root / "prop.glb", self.root / "collapsed-topology",
                method="decimate", target_faces=4, seed=42, policy=policy, timeout_s=90)
        self.assertEqual(error.exception.rc, 5)
        self.assertEqual((self.root / "prop.glb").read_bytes(), original)

    def test_pipeline_invokes_production_verifier_before_admission(self):
        backend = stages.NativeStages(source=self.root / "prop.glb", reference=self.root / "reference.png",
            provider="byo", options={"timeout_s": 90, "texture_resolution": 64, "max_stretch": 4000})
        out = self.root / "refused-stage"
        out.mkdir()
        def baked(high, low, destination, **kwargs):
            shutil.copytree(self.baked, destination)
            return copy.deepcopy(self.result)
        with mock.patch.object(stages.bake, "bake_bundle", baked), mock.patch.object(
                stages.bundle, "verify_bundle", return_value={"rc": 4, "status": "STALE",
                    "reason_codes": ["planted_production_failure"]}) as verify:
            with self.assertRaises(engine.Refusal) as error:
                backend.stage_uv_bake_orm(out, self.root / "prop.glb")
        self.assertEqual(error.exception.rc, 4)
        verify.assert_called_once()
        self.assertFalse((out / "named_low.glb").exists())


if __name__ == "__main__":
    unittest.main()
