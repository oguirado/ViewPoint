"""Real CPU geometry witness; absent production acceptance is never forged.

The fixture is constructed and rendered by Blender operators inside the memory
scope. No third-party asset, model, image fixture or GPU is used.
"""
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
from tools.blender.glb_capture import run_owned
from tools.pipeline import engine, run_asset, stages


def options(root):
    policy = {"sample_budget": 512, "raster_resolution": 96, "view_basis": "gltf-y-up-ortho4",
              "unit": "metre", "transform": "world", "weld_tolerance_um": 0,
              "allow_open": False, "require_uv": False, "component_allowlist": {"body": 1},
              "timeout_s": 60, "repair": {"max_passes": 1, "max_hole_edges": 0, "cull_max_permille": 0}}
    core.atomic_write_json(root / "policy.json", policy)
    return {"mesh_policy": root / "policy.json", "timeout_s": 90, "method": "identity",
            "target_faces": None, "seed": 42, "pipeline_type": "512", "texture_resolution": 64,
            "max_stretch": 4000, "required_checks": []}


class BlenderIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = os.environ.get("AF_E2E_OUTPUT")
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="procedural-e2e-", dir=parent)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        blender = shutil.which("blender")
        if blender is None:
            raise AssertionError("Blender absent: real witness cannot see; never skip")
        run_owned([blender, "-b", "--factory-startup", "--threads", "2", "--python-exit-code", "2",
                   "--python", str(ROOT / "tools/pipeline/fixture_blender.py"), "--", str(cls.root)],
                  timeout_s=120, cwd=cls.root)
        cls.kw = options(cls.root)

        # Import here because the geometry fixture imports options from this module.
        from test_n18_int_geometry import scope_for_fixture

        source = cls.root / "prop.glb"
        raw = source.read_bytes()
        backend = cls.backend()
        bake = cls.root / "scope-bake"
        bake.mkdir()
        baked = bake / backend.stage_uv_bake_orm(bake, source)["subject"]
        export = cls.root / "scope-export"
        export.mkdir()
        exported = export / backend.stage_export(export, baked)["subject"]

        # Identity repair and topology preserve raw bytes. Pin the CPU bake and
        # export separately so the pipeline must qualify each actual subject.
        policy = engine.load(cls.kw["mesh_policy"])
        policy["geometry_scope"] = scope_for_fixture((cls.root / "authority").resolve(), raw, {
            "mesh": raw, "topology": raw, "uv_bake_orm": baked.read_bytes(),
            "export": exported.read_bytes()}, cube=True)
        core.atomic_write_json(cls.kw["mesh_policy"], policy)

    @classmethod
    def backend(cls, reference="reference.png"):
        return stages.NativeStages(source=cls.root / "prop.glb", reference=cls.root / reference,
                                   provider="byo", options=cls.kw.copy())

    def test_real_byo_chain_crosses_repaired_boundary_but_holds_missing_acceptance(self):
        result = engine.Pipeline(self.root / "native-run", self.backend()).run()
        self.assertEqual([r["rc"] for r in result["stages"][:3]], [0, 0, 0], result)
        # This is the actual landed API, not a replacement comparator.
        self.assertEqual(result["stages"][3]["rc"], 0, result)
        receipt = engine.load(result["stages"][3]["receipt"]["path"])
        self.assertEqual(receipt["reason_codes"], [])
        self.assertGreater(receipt["checks_evaluated"], 0)
        self.assertEqual(result["rc"], 3, result)
        self.assertEqual(result["verdict"], "CANNOT_SEE")
        self.assertTrue(any(row["rc"] == 3 for row in result["stages"][4:]))
        self.assertIsNone(result["artifact"])

    def test_real_cpu_bake_and_material_bound_export(self):
        backend = self.backend()
        directory = self.root / "real-maps"
        directory.mkdir()
        result = backend.stage_uv_bake_orm(directory, self.root / "prop.glb")
        self.assertEqual(result["rc"], 0, result)
        self.assertGreater(result["checks_evaluated"], 0)
        export = self.root / "real-export"
        export.mkdir()
        result = backend.stage_export(export, directory / result["subject"])
        self.assertEqual(result["rc"], 0, result)
        self.assertTrue((export / "asset.glb").is_file())
        self.assertEqual(engine.load(export / "regression.json")["metrics"]["hausdorff_relative_ppm"], 0)
        # Both missing acceptance suppliers stay incomplete even for self-render.
        with self.assertRaises(engine.Refusal) as refusal:
            backend.stage_appearance(export, export / "asset.glb")
        self.assertEqual(refusal.exception.rc, 3)
        with self.assertRaises(engine.Refusal) as refusal:
            backend.stage_vqa3d(export, export / "asset.glb")
        self.assertEqual(refusal.exception.rc, 3)

    def test_unbuilt_trellis_service_cannot_see(self):
        backend = stages.NativeStages(source=self.root / "reference.png", reference=self.root / "reference.png",
                                      provider="trellis2", options=self.kw.copy())
        directory = self.root / "unbuilt-provider"
        directory.mkdir()
        with self.assertRaises(engine.Refusal) as raised:
            backend.stage_provider(directory, None)
        self.assertEqual(raised.exception.rc, 3)
        self.assertFalse(list(directory.iterdir()))

    def test_byo_input_does_not_assert_image_gate_or_licence_approval(self):
        directory = self.root / "byo-intake"
        directory.mkdir()
        result = self.backend().stage_input(directory, None)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(engine.load(directory / "intake.json")["scope"], "image_gate_not_applicable_to_glb")

    def test_cached_prefix_detects_actual_glb_tampering(self):
        backend = self.backend()
        out = self.root / "tamper-run"
        first = engine.Pipeline(out, backend).run()
        receipt = engine.load(first["stages"][2]["receipt"]["path"])
        path = Path(receipt["subject"])
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        result = engine.Pipeline(out, self.backend()).run()
        self.assertEqual(result["stages"][2]["rc"], 4)
        self.assertNotEqual(result["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
