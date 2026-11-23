"""Live CPU Blender witnesses using the existing vqa3d evaluators, not copies.

Missing Blender or sibling evaluators fails this suite; it is never skipped.
The adapter here is test-only: F2-U1 remains the production integration point.
"""
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "blender-visual-qa"))
from tools.blender import topology as top
from tools.blender.topology_glb import digest, inspect_glb, json_digest
from blender_visual_qa.assets import glb_core, heal, lod_ladder, mesh_gate

POLICY = {"silhouette_iou_min_ppm": 950000, "hausdorff_max_ppm": 20000,
          "mesh_policy": {"sample_budget": 6000, "raster_resolution": 96}}


def measured_compare(raw, candidate, *, policy):
    before, after = glb_core.read_glb(raw), glb_core.read_glb(candidate)
    points, _ = lod_ladder.tri_soup(before.prims)
    diagonal = lod_ladder.bbox_diag_um(points)
    forward, backward = lod_ladder.hausdorff_um(before.prims, after.prims, diagonal,
                                              samples=policy["sample_budget"])
    iou = heal.silhouette_iou_ppm(before, after, res=policy["raster_resolution"])
    rc, mesh = mesh_gate.gate_scene(after, require_uv=False)
    report = {"schema": "af.mesh-regression/v1", "rc": rc, "status": top.STATUS[rc],
              "input_sha256": digest(raw.read_bytes()), "candidate_sha256": digest(candidate.read_bytes()),
              "policy_sha256": json_digest(policy),
              "toolchain_sha256": json_digest({module.__name__: digest(Path(module.__file__).read_bytes())
                                               for module in (glb_core, heal, lod_ladder, mesh_gate)}),
              "measurement_method": "sampled_surface_distance_and_fixed_view_raster",
              "failed": mesh.get("failed", []), "unavailable": [],
              "metrics": {"raw_diag_um": diagonal, "hausdorff_forward_um": forward,
                          "hausdorff_backward_um": backward, "silhouette_iou_ppm": iou}}
    return rc, report


class BlenderWitnesses(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name)
        cls.raw = cls.base / "raw.glb"
        executable = shutil.which("blender")
        if executable is None:
            raise AssertionError("Blender unavailable: live gate CANNOT_SEE")
        expression = ("import bpy; bpy.ops.object.select_all(action='SELECT'); "
                      "bpy.ops.object.delete(use_global=False); "
                      "bpy.context.scene.render.engine='CYCLES'; bpy.context.scene.cycles.device='CPU'; "
                      "bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1); "
                      "obj=bpy.context.object; "
                      "[obj.data.uv_layers.remove(uv) for uv in list(obj.data.uv_layers)]; "
                      "bpy.ops.export_scene.gltf(filepath=" + repr(str(cls.raw)) +
                      ", export_format='GLB', use_selection=True)")
        rc, log = top.default_runner(top.SCOPE_ARGV + [executable, "-b", "--factory-startup",
                                                      "--threads", "1", "--python-exit-code", "2",
                                                      "--python-expr", expression], 60)
        if rc:
            raise AssertionError("live fixture failed: " + log)
        cls.original = cls.raw.read_bytes()
        if inspect_glb(cls.raw)["faces"] != 1280:
            raise AssertionError("fixture triangle count drift")

    def run_live(self, name, **options):
        kwargs = dict(method="decimate", target_faces=1200, seed=42, policy=POLICY,
                      expected_source_sha256=digest(self.original), timeout_s=90,
                      comparator=measured_compare, allow_test_comparator=True)
        kwargs.update(options)
        return top.run(self.raw, self.base / name, **kwargs)

    def test_live_identity_byte_custody(self):
        rc, receipt = self.run_live("identity", method="identity", target_faces=1280)
        self.assertEqual(rc, 0, receipt)
        self.assertEqual((self.base / "identity" / "raw.glb").read_bytes(), self.original)
        self.assertEqual(receipt["checks"][0]["metrics"]["hausdorff_forward_um"], 0)
        self.assertEqual(receipt["checks"][0]["metrics"]["silhouette_iou_ppm"], 1000000)

    def test_live_decimate_and_ladder_measured(self):
        rc, receipt = self.run_live("ladder", lod_targets=[1100, 1000])
        self.assertEqual(rc, 0, receipt)
        self.assertEqual(len(receipt["checks"]), 3)
        for artifact in receipt["artifacts"]:
            self.assertLessEqual(artifact["achieved_faces"], artifact["requested_faces"])
        self.assertEqual((self.base / "ladder" / "LOD0.glb").read_bytes(),
                         (self.base / "ladder" / "master.glb").read_bytes())
        self.assertEqual(self.raw.read_bytes(), self.original)
        print("LIVE decimate_ladder", json.dumps(receipt["checks"], sort_keys=True))

    def test_live_quadriflow_measured(self):
        rc, receipt = self.run_live("quadriflow", method="quadriflow", quadriflow_target_quads=500)
        self.assertEqual(rc, 0, receipt)
        self.assertLess(inspect_glb(self.base / "quadriflow" / "master.glb")["faces"], 1280)
        print("LIVE quadriflow", json.dumps(receipt["checks"], sort_keys=True))

    def test_live_quadriflow_overshoot_is_refused(self):
        rc, receipt = self.run_live("quad_overshoot", method="quadriflow")
        self.assertEqual(rc, 5, receipt)
        self.assertIn("face_budget_unmet_or_fake_lod", receipt["reason_codes"])
        self.assertGreater(receipt["checks"][0]["achieved_faces"], 1200)
        self.assertFalse((self.base / "quad_overshoot").exists())

    def test_live_repeatability(self):
        rc, first = self.run_live("repeat_a")
        self.assertEqual(rc, 0, first)
        rc, second = self.run_live("repeat_b")
        self.assertEqual(rc, 0, second)
        self.assertEqual(first["candidate_sha256"], second["candidate_sha256"])
        self.assertEqual(first["checks"], second["checks"])

    def test_live_collapsed_master_is_rejected_with_explicit_target(self):
        rc, receipt = self.run_live("collapsed", target_faces=8)
        self.assertEqual(rc, 5, receipt)
        self.assertTrue(set(receipt["reason_codes"]) & {"silhouette_regression", "surface_regression", "regression_refused"})
        self.assertIn("silhouette_regression", receipt["reason_codes"])
        self.assertEqual(receipt["checks"][0]["rc"], 0, "mesh gate must pass before fidelity refuses")
        self.assertFalse((self.base / "collapsed").exists())
        self.assertEqual(self.raw.read_bytes(), self.original)
        print("LIVE collapsed RED", receipt["reason_codes"])

    def test_live_textured_decimate_custody(self):
        textured = self.base / "textured.glb"
        expression = ("import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False); "
                      "bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1); obj=bpy.context.object; "
                      "bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT'); "
                      "bpy.ops.uv.sphere_project(); bpy.ops.object.mode_set(mode='OBJECT'); "
                      "mat=bpy.data.materials.new('paint'); mat.use_nodes=True; "
                      "image=bpy.data.images.new('test-pixels', width=2, height=2); "
                      "image.pixels=[1,0,0,1, 0,1,0,1, 0,0,1,1, 1,1,1,1]; image.pack(); "
                      "tex=mat.node_tree.nodes.new('ShaderNodeTexImage'); tex.image=image; "
                      "bsdf=mat.node_tree.nodes.get('Principled BSDF'); "
                      "mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color']); "
                      "obj.data.materials.append(mat); bpy.ops.export_scene.gltf(filepath=" + repr(str(textured)) +
                      ", export_format='GLB', use_selection=True)")
        rc, log = top.default_runner(top.SCOPE_ARGV + [shutil.which("blender"), "-b", "--factory-startup",
                                    "--threads", "1", "--python-exit-code", "2", "--python-expr", expression], 60)
        self.assertEqual(rc, 0, log)
        before = inspect_glb(textured)
        self.assertTrue(before["image_sha256"])
        self.assertEqual(before["uv_primitives"], before["primitives"])
        rc, receipt = top.run(textured, self.base / "textured-out", method="decimate", target_faces=1200,
                              seed=42, policy=POLICY, expected_source_sha256=before["sha256"],
                              comparator=measured_compare, allow_test_comparator=True)
        self.assertEqual(rc, 0, receipt)
        after = inspect_glb(self.base / "textured-out" / "master.glb")
        self.assertEqual(before["materials_sha256"], after["materials_sha256"])
        self.assertEqual(before["image_sha256"], after["image_sha256"])
        self.assertEqual(before["texture_state_sha256"], after["texture_state_sha256"])
        self.assertTrue(any(any(span) for span in after["uv_spans"]))
        print("LIVE textured custody", before, after)

    def test_live_fidelity_assertion_rejects_upstream_only_failure(self):
        compare = measured_compare
        def upstream_only(raw, candidate, *, policy):
            _, receipt = compare(raw, candidate, policy=policy)
            receipt.update(rc=5, status="FAIL")
            return 5, receipt
        with mock.patch.object(sys.modules[__name__], "measured_compare", upstream_only):
            result = unittest.TestResult()
            BlenderWitnesses("test_live_collapsed_master_is_rejected_with_explicit_target").run(result)
        self.assertTrue(result.failures, "a mesh-only refusal must not satisfy the fidelity witness")
        self.assertIn("silhouette_regression", result.failures[0][1])
        print("REPAIR MUTANT RED live-upstream-only-fidelity")


if __name__ == "__main__":
    unittest.main()
