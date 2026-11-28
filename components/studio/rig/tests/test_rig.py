# SPDX-License-Identifier: AGPL-3.0-only
"""Offline synthetic rig and pose regression controls."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bindfix import Refusal, recompute_inverse_binds
from diagnostics import report_trials, verify_failed_readback, verify_readback
from glb_utils import add, pack, primitive, unpack
from pre_rig import geometry, normalise, validate_projection
from rig_common import Hold, invoke_blender
from rigid_distance import distance_bounds
from rigid_skin import clipped_polygon
from template_rig import accessor, constrain_chassis_weights, mechanical_top_four
from transfer import closest_on_triangles, top_four

VERTICES = np.array([[0, 0, 0], [1, 0, 0], [0, 3, 0], [0, 0, 1]], dtype=np.float32)
FACES = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])


def fixture(*, seamed=False, open_mesh=False, skinned=False):
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"mesh": 0}], "meshes": [{"primitives": [{"attributes": {}}]}],
           "buffers": [{}], "bufferViews": [], "accessors": []}
    data = bytearray()
    faces = FACES[:-1] if open_mesh else FACES
    xyz = VERTICES[faces.flatten()] if seamed else VERTICES
    indices = np.arange(faces.size) if seamed else faces.flatten()
    part = primitive(doc)
    part["attributes"]["POSITION"] = add(doc, data, xyz, "VEC3", "<f4", 5126, bounds=True)
    part["indices"] = add(doc, data, indices, "SCALAR", "<u4", 5125)
    if skinned:
        doc["nodes"] = [{"children": [1, 2], "translation": [12, 1, -2]},
                        {"mesh": 0, "skin": 0, "translation": [.25, 0, 0]},
                        {"name": "bone_0", "translation": [.5, 0, 0]}]
        bind = add(doc, data, np.eye(4).reshape(1, 16), "MAT4", "<f4", 5126)
        doc["skins"] = [{"joints": [2], "inverseBindMatrices": bind}]
    return pack(doc, data)


def pose_sample(path, *, reject):
    rest = np.vstack((VERTICES, VERTICES + [4, 0, 0]))
    posed = rest @ np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]]) + [.1, .2, .3]
    if reject:
        posed[1, 0] += .04
    np.savez(path, rest=rest, parts=np.repeat([0, 1], len(VERTICES)), move=posed)


class RigTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)

    def launch(self, program, *argv):
        return subprocess.run([sys.executable, "-B", str(ROOT / program), *map(str, argv)],
                              text=True, capture_output=True, timeout=150,
                              env=os.environ | {"TMPDIR": str(self.work)}, check=False)

    def test_weld_preserves_triangle_corners_and_is_idempotent(self):
        source = fixture(seamed=True)
        welded = normalise(source)
        before_xyz, before_faces = geometry(*unpack(source))
        xyz, faces = validate_projection(welded)
        np.testing.assert_array_equal(xyz[faces], before_xyz[before_faces])
        self.assertEqual(len(xyz), len(VERTICES))
        self.assertEqual(normalise(welded), welded)

    def test_weld_refuses_open_surface(self):
        with self.assertRaisesRegex(Hold, "studio_projection_open_or_inconsistent_winding"):
            normalise(fixture(open_mesh=True))

    def test_bindfix_changes_only_bind_and_is_idempotent(self):
        source = fixture(skinned=True)
        result = recompute_inverse_binds(source, timeout_s=10)
        doc, data = unpack(result)
        bind = doc["skins"][0]["inverseBindMatrices"]
        expected = np.eye(4).reshape(1, -1)
        expected[0, 12] = -.25
        np.testing.assert_array_equal(accessor(doc, data, bind), expected)
        view = doc["bufferViews"][doc["accessors"][bind]["bufferView"]]
        start = len(result) - len(data) + view["byteOffset"]
        self.assertEqual(source[:start], result[:start])
        self.assertEqual(source[start + 64:], result[start + 64:])
        self.assertEqual(recompute_inverse_binds(result, timeout_s=10), result)

    def test_bindfix_refuses_shared_view(self):
        doc, data = unpack(fixture(skinned=True))
        doc["accessors"].append(dict(doc["accessors"][-1]))
        with self.assertRaisesRegex(Refusal, "inverse_bind_view_shared"):
            recompute_inverse_binds(pack(doc, data), timeout_s=10)

    def test_rigidity_distinguishes_rigid_motion_from_deformation(self):
        for reject in (False, True):
            path = self.work / "poses.npz"
            pose_sample(path, reject=reject)
            with np.load(path) as sample:
                bounds = distance_bounds(sample["rest"], sample["move"], {"part0": sample["parts"] == 0})
            self.assertEqual(bounds["part0"] > .0005, reject)

    def test_rigid_plane_clip_preserves_area(self):
        corners = np.array([[-1., 0, 0], [1., 0, 0], [0., 1, 0]])
        basis = np.eye(3)
        sides = [clipped_polygon(basis, corners[:, 0], side) @ corners for side in (False, True)]
        self.assertTrue(all(len(side) == 3 for side in sides))
        self.assertAlmostEqual(sum(np.linalg.norm(np.cross(b-a, c-a)) / 2 for a, b, c in sides), 1.0)

    def test_closest_and_top_four_preserve_weight_mass(self):
        tri = np.array([[[0., 0, 0], [2., 0, 0], [0., 2, 0]]])
        points, bary = closest_on_triangles(tri, np.array([[.5, .5, .75]]))
        np.testing.assert_allclose(points, [[.5, .5, 0.]])
        np.testing.assert_allclose(bary, [[.5, .25, .25]])
        slots, weights, dropped = top_four(np.array([[.2, .1, .4, .2, .1]]))
        np.testing.assert_array_equal(slots, [[2, 0, 3, 1]])
        np.testing.assert_allclose(weights.sum(axis=1), [1.])
        np.testing.assert_allclose(dropped, [.1])

    def test_provider_receipt_binds_original_bytes(self):
        original, source, receipt = [self.work / name for name in ("original.glb", "source.glb", "receipt.json")]
        original.write_bytes(fixture())
        source.write_bytes(fixture(skinned=True))
        row = {"schema": "af.unirig-host-inference/v1", "input_sha256": sha256(original.read_bytes()).hexdigest(),
               "rigged_sha256": sha256(source.read_bytes()).hexdigest(), "invocation": "synthetic-fixture",
               "request_sha256": "f" * 64, "device": "gpu", "wall_ms": 4, "voxel_exchanges": [{}],
               "stages": [{"stage": stage, "rc": 0, "wall_ms": 1}
                          for stage in ("extract", "skeleton", "skin", "merge")]}
        receipt.write_text(json.dumps(row))
        self.assertEqual(verify_readback(original, source, receipt)["joint_names"], ["bone_0"])
        original.write_bytes(fixture(seamed=True))
        with self.assertRaisesRegex(Hold, "unirig_provider_receipt_mismatch"):
            verify_readback(original, source, receipt)

    def test_failed_trial_requires_committed_scope_and_bound_input(self):
        input_file, attempt = self.work / "original.glb", self.work / "attempt"
        input_file.write_bytes(fixture())
        attempt.mkdir()
        input_hash = sha256(input_file.read_bytes()).hexdigest()
        launch = {"invocation": "fixture", "input_sha256": input_hash, "generated_rig": {"seed": 7}}
        admission = {"scope_command": ["synthetic"], "committed": True, "variant": "V3",
                     "repeat": 1, "run_key": "fixture-plan"}
        result = {"provider_rc": 124, "status": "PROVIDER_FAILED", "invocation": "fixture",
                  "input_sha256": input_hash, "variant": "V3", "repeat": 1, "seed": 7,
                  "run_key": "fixture-plan"}
        rows = {"host-launch.json": launch, "admission.json": admission, "result.json": result,
                "scope-exit.json": {"rc": 0, "command": admission["scope_command"]}}
        for name, data in rows.items():
            (attempt / name).write_text(json.dumps(data))
        self.assertEqual(verify_failed_readback(input_file, attempt)["reason"], "provider_timed_out")
        admission["committed"] = False
        (attempt / "admission.json").write_text(json.dumps(admission))
        with self.assertRaisesRegex(Hold, "diagnostic_failed_attempt_binding"):
            verify_failed_readback(input_file, attempt)

    def test_diagnostic_control_and_two_repeats(self):
        inputs = [{"id": name, "seed": 1, "input": name, "repeats": repeats}
                  for name, repeats in (("V5", 1), ("V3", 2))]
        def observation(name, repeat, count):
            return {"variant": name, "repeat": repeat, "seed": 1, "input": name, "status": "COMPLETE",
                    "invocation": f"{name}-{repeat}", "joint_count": count}
        rows = [observation("V5", 1, 22), observation("V3", 1, 19), observation("V3", 2, 19)]
        self.assertEqual(report_trials(inputs, rows[:2])["status"], "IN_PROGRESS")
        self.assertEqual(report_trials(inputs, rows)["winner"], "V3")
        rows[0]["joint_count"] = 21
        self.assertEqual(report_trials(inputs, rows)["status"], "HOLD")

    def test_original_trial_preserves_input_file(self):
        source, report = self.work / "original.glb", self.work / "trial.json"
        raw = fixture()
        source.write_bytes(raw)
        run = self.launch("cli.py", "unirig-plan", "--original", source, "--out", report, "--seed", 3)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(json.loads(report.read_text())["input"]["sha256"], sha256(raw).hexdigest())

    def test_mechanical_weight_sharpening_preserves_four_normalized_influences(self):
        slots, weights = mechanical_top_four(np.array([[.4, .1, .2, .2, .1]]))
        np.testing.assert_array_equal(slots, [[0, 2, 3, 1]])
        expected = np.array([.4, .2, .2, .1]) ** 4
        np.testing.assert_allclose(weights, [expected / expected.sum()])
        self.assertGreater(weights[0, 0], .8)

    def test_chassis_constraint_retains_torso_blend_and_limb_weights_outside(self):
        xyz = np.array([[0., .35, 0.], [1., .35, 0.], [0., 1., 0.]])
        dense = np.zeros((len(xyz), 22))
        dense[:, [1, 2, 14]] = [.25, .35, .4]
        self.assertEqual(constrain_chassis_weights(dense, xyz), 1)
        np.testing.assert_allclose(dense[:, 14], [0., .4, .4])
        np.testing.assert_allclose(dense[:, 1:3], [[.25, .35]] * 3)

    def test_background_blender_bone_heat(self):
        # Closed tetrahedron with one active deform bone through its interior.
        np.savez(self.work / "fit.npz", xyz=VERTICES, faces=FACES,
                 heads=[[.1, .2, .1]], tails=[[.1, 1.8, .1]], parents=[-1])
        invoke_blender(self.work, ROOT / "auto_weights.py", [self.work])
        dense = np.load(self.work / "heat.npy")
        self.assertEqual(dense.shape, (4, 1))
        self.assertTrue(np.all(dense > 0), dense)

    def test_diagnostic_subdivision_keeps_original_trial_bytes(self):
        source = self.work / "original.glb"
        selected = self.work / "selected.glb"
        destination = self.work / "diagnostics"
        source.write_bytes(fixture())
        selected.write_bytes(fixture(seamed=True))
        result = self.launch("cli.py", "diagnose-prepare", "--original", source, "--selected", selected,
                             "--control", source, "--out-dir", destination, "--seed", 7,
                             "--simplification-limit", 5, "--v1-min", 6, "--v2-min", 16)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        plan = {row["id"]: row for row in json.loads((destination / "trials.json").read_text())}
        self.assertEqual(plan["V3"]["input"]["sha256"], sha256(source.read_bytes()).hexdigest())
        self.assertEqual(plan["V1"]["geometry"]["triangles"], 6)
        self.assertEqual(plan["V2"]["geometry"]["triangles"], 16)

    def test_oracle_compares_explicit_native_scene_inputs(self):
        invoke_blender(self.work, ROOT / "tests/oracle_controls.py", [self.work, "--scene-only"])
        original = self.work / "scene-original.blend"
        displaced = self.work / "scene-displaced.blend"
        out = self.work / "scene.json"
        for candidate, expected in ((original, 0), (displaced, 1)):
            result = self.launch("pose/oracle", "scene", "--incumbent", original, "--candidate", candidate,
                                 "--incumbent-sha256", sha256(original.read_bytes()).hexdigest(),
                                 "--candidate-sha256", sha256(candidate.read_bytes()).hexdigest(),
                                 "--witness", "Floor plate", "--out", out)
            self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
            receipt = json.loads(out.read_text())
            self.assertEqual(receipt["matched"], expected == 0)
            if expected:
                self.assertNotEqual(receipt["incumbent"]["bindings"]["camera"],
                                    receipt["candidate"]["bindings"]["camera"])

    def test_oracle_synth_reopens_fixture_and_measures_neutral_bind(self):
        contract = self.work / "contract.json"
        contract.write_text(json.dumps({"tolerances": {"seam_band_m": {"value": .003}}}))
        out = self.work / "controls"
        result = self.launch("pose/oracle", "synth", "--out-dir", out, "--contract", contract)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads((out / "rig.synth.json").read_text())
        self.assertLess(report["asset"]["inverse_bind_m"], 1e-6)
        sweep = out / "sweep-synth" / "sweep.json"
        self.assertTrue(sweep.is_file())
        recorded = (out / "rig.synth.json").read_bytes()
        rerun = self.launch("pose/oracle", "synth", "--out-dir", out, "--contract", contract)
        self.assertEqual(rerun.returncode, 0, rerun.stderr or rerun.stdout)
        self.assertEqual((out / "rig.synth.json").read_bytes(), recorded)
        contract.write_text(json.dumps({"tolerances": {"triangle_budget": {"value": 32000}}}))
        measured = self.work / "measure.json"
        refusal = self.launch("pose/oracle", "measure", "--rig", out / "synth_eccentric_pivot.glb",
                              "--map", out / "joints.synth.seated.json", "--contract", contract,
                              "--cameras", out / "cameras.synth.json", "--poses", sweep,
                              "--out", measured, "--jobs", 1)
        self.assertEqual(refusal.returncode, 1, refusal.stderr or refusal.stdout)
        self.assertEqual(json.loads(measured.read_text())["rejected_classes"], ["asset_substituted"])


if __name__ == "__main__":
    unittest.main()
