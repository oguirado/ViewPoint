"""Custody, fail-closed controls and one real optional-engine integration."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from test_f2u1_mesh import glb, policy, VERTS
from tools.mesh import contracts, remesh


def audit(raw_hash, candidate_hash):
    return {"schema": "af.remesh-distance-audit/v1", "rc": 0,
            "input_sha256": raw_hash, "candidate_sha256": candidate_hash,
            "raw_diag_um": 1732050, "forward_um": 1, "backward_um": 1,
            "forward_cover_estimate_um": 2, "backward_cover_estimate_um": 2,
            "forward_samples": 100, "backward_samples": 100,
            "distance_is_exact": False, "estimator": "all_vertices_and_triangle_centres_BVH",
            "coverage_limit": "floating_BVH_and_centre_radius_cover_are_numerical_estimates"}


class RemeshStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw.glb"
        self.original = glb()
        self.raw.write_bytes(self.original)
        self.dest = self.root / "accepted"
        self.candidate = self.original
        self.modify_audit = lambda x: None
        self.after_audit = lambda: None
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(remesh, "_engine_identity", return_value=({}, [])).start()
        mock.patch.object(remesh, "_tool_identity", return_value="a" * 64).start()
        mock.patch.object(remesh.shutil, "which", return_value="/usr/bin/blender").start()
        self.runner = mock.patch.object(remesh, "default_runner", side_effect=self.fake_runner).start()

    def fake_runner(self, argv, timeout):
        plan = json.loads(Path(argv[-1]).read_text())
        if "audit" not in plan:
            Path(plan["candidate"]).write_bytes(self.candidate)
        else:
            record = audit(plan["source_sha256"], plan["candidate_sha256"])
            self.modify_audit(record)
            Path(plan["audit"]).write_text(json.dumps(record, sort_keys=True) + "\n")
            self.after_audit()
        return 0, "AF_REMESH_RC 0\n"

    def run_stage(self, **kwargs):
        options = dict(expected_source_sha256=hashlib.sha256(self.raw.read_bytes()).hexdigest(),
                       policy=policy(raster_resolution=96), method="voxel_grid", voxel_size_um=3000)
        options.update(kwargs)
        return remesh.run(self.raw, self.dest, **options)

    def test_atomic_positive_bundle_binds_original_and_measured_bytes(self):
        rc, record = self.run_stage()
        self.assertEqual(rc, 0, record)
        self.assertEqual(record["verdict"], "PASS")
        self.assertEqual((self.dest / "raw.glb").read_bytes(), self.original)
        self.assertEqual((self.dest / "remesh.glb").read_bytes(), self.original)
        self.assertEqual(self.raw.read_bytes(), self.original)
        self.assertEqual((self.dest / "receipt.json").read_text(), remesh.core.canonical_json(record))
        for artifact in record["artifacts"]:
            self.assertEqual(artifact["sha256"], remesh.core.sha256_file(self.dest / artifact["path"]))
        self.assertIs(record["mesh_receipt"]["metrics"]["distance_is_exact"], False)
        self.assertIsNone(record["peak_rss_bytes"])

    def test_shortened_limb_red_then_unchanged_geometry_green(self):
        self.candidate = glb([{"name": "body", "verts": [*VERTS[:3], (0, 0, 400000)]}])
        rc, record = self.run_stage()
        self.assertEqual(rc, 5, record)
        self.assertIn("hausdorff_relative", record["mesh_receipt"]["failed"])
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.raw.read_bytes(), self.original)
        self.candidate = self.original
        self.assertEqual(self.run_stage()[0], 0)

    def test_bypassing_mesh_refusal_makes_negative_oracle_red_then_restored_green(self):
        source = Path(remesh.__file__).read_text()
        anchor = '_require(rc == 0, rc, "mesh_gate_refused")'
        self.assertEqual(source.count(anchor), 1)
        namespace = dict(remesh.__dict__)
        exec(compile(source.replace(anchor, "pass  # planted refusal bypass"),
                     remesh.__file__, "exec"), namespace)
        namespace.update(default_runner=self.fake_runner, _engine_identity=lambda _: ({}, []),
                         _tool_identity=lambda *_: "a" * 64)
        self.candidate = glb([{"name": "body", "verts": [*VERTS[:3], (0, 0, 400000)]}])
        with self.assertRaises(AssertionError):
            rc, record = namespace["run"](
                self.raw, self.dest, expected_source_sha256=hashlib.sha256(self.original).hexdigest(),
                policy=policy(raster_resolution=96), method="voxel_grid", voxel_size_um=3000)
            self.assertEqual(rc, 5, record)  # this assertion MUST go red for the mutant
        self.dest = self.root / "restored-gate"
        self.assertEqual(self.run_stage()[0], 5)
        self.assertFalse(self.dest.exists())

    def test_existing_destination_and_symlink_preserve_bytes(self):
        self.dest.mkdir()
        (self.dest / "keep").write_bytes(b"caller-owned")
        self.assertEqual(self.run_stage()[0], 2)
        self.assertEqual((self.dest / "keep").read_bytes(), b"caller-owned")
        (self.dest / "keep").unlink()
        self.dest.rmdir()
        self.dest.symlink_to(self.raw)
        self.assertEqual(self.run_stage()[0], 2)
        self.assertEqual(self.raw.read_bytes(), self.original)
        self.runner.assert_not_called()

    def test_stale_source_pin_is_exact_rc_four(self):
        self.assertEqual(self.run_stage(expected_source_sha256="b" * 64)[0], 4)
        self.runner.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_open_policy_and_boolean_or_zero_parameters_refused(self):
        for changes in ({"policy": policy(allow_open=True, raster_resolution=96)},
                        {"voxel_size_um": True}, {"closing_iterations": 0},
                        {"max_grid_cells": 0}, {"timeout_s": 0},
                        {"policy": policy(raster_resolution=8)}):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_stage(**changes)[0], 2)
                self.assertFalse(self.dest.exists())
        self.runner.assert_not_called()

    def test_texture_attributes_are_not_silently_stripped(self):
        self.raw.write_bytes(glb([{"name": "body", "uv": True}]))
        self.assertEqual(self.run_stage()[0], 3)
        self.runner.assert_not_called()

    def test_worker_success_exit_without_completion_marker_cannot_see(self):
        self.runner.side_effect = None
        self.runner.return_value = (0, "native solver exited without completing")
        self.assertEqual(self.run_stage()[0], 3)
        self.assertFalse(self.dest.exists())

    def test_worker_rc_one_is_never_propagated_as_stage_one(self):
        self.runner.side_effect = None
        self.runner.return_value = (1, "AF_REMESH_RC 1\n")
        self.assertEqual(self.run_stage()[0], 3)

    def test_timeout_has_exact_rc_six_and_no_publication(self):
        self.runner.side_effect = subprocess.TimeoutExpired("owned worker", 1)
        self.assertEqual(self.run_stage()[0], 6)
        self.assertFalse(self.dest.exists())

    def test_fake_pass_with_no_mesh_checks_is_error(self):
        fake = contracts.receipt()
        fake.update(rc=0, status="OK", artefact_sha256="a" * 64)
        with mock.patch.object(remesh.api, "compare_meshes", return_value=(0, fake)):
            self.assertEqual(self.run_stage()[0], 2)
        self.assertFalse(self.dest.exists())

    def test_dense_audit_missing_samples_and_false_pass_refused(self):
        self.modify_audit = lambda r: r.update(forward_samples=0)
        self.assertEqual(self.run_stage()[0], 3)
        self.modify_audit = lambda r: r.update(forward_cover_estimate_um=900000)
        self.assertEqual(self.run_stage()[0], 2)  # false PASS disagrees with its numbers
        self.modify_audit = lambda r: r.update(rc=5, forward_cover_estimate_um=900000)
        self.assertEqual(self.run_stage()[0], 5)
        self.modify_audit = lambda r: None
        self.assertEqual(self.run_stage()[0], 0)

    def test_dense_audit_hash_swap_is_error(self):
        self.modify_audit = lambda r: r.update(candidate_sha256="b" * 64)
        self.assertEqual(self.run_stage()[0], 2)
        self.assertFalse(self.dest.exists())

    def test_external_source_drift_after_gate_is_stale(self):
        self.after_audit = lambda: self.raw.write_bytes(b"changed externally")
        self.assertEqual(self.run_stage()[0], 4)
        self.assertFalse(self.dest.exists())

    def test_publish_race_preserves_competing_destination(self):
        def race():
            self.dest.mkdir()
            (self.dest / "keep").write_bytes(b"concurrent-owner")
        self.after_audit = race
        self.assertEqual(self.run_stage()[0], 2)
        self.assertEqual((self.dest / "keep").read_bytes(), b"concurrent-owner")


class RemeshRealIntegrationTests(unittest.TestCase):
    def test_open_generated_sphere_is_remeshed_and_independently_gated(self):
        # Algorithm-generated synthetic fixture, never a model output or a
        # hand-authored production asset. Libraries remain optional at runtime.
        if any(importlib_missing(n) for n in ("trimesh", "scipy", "skimage")):
            with self.assertRaises(contracts.MeshError) as caught:
                remesh._engine_identity("voxel_grid")
            self.assertEqual(caught.exception.rc, 3)
            return
        import trimesh
        mesh = trimesh.creation.icosphere(subdivisions=4)
        mesh.update_faces(list(range(1, len(mesh.faces))))
        scene = trimesh.Scene()
        scene.add_geometry(mesh, node_name="body", geom_name="body")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "open.glb"
            source.write_bytes(scene.export(file_type="glb"))
            rc, record = remesh.run(source, root / "result", method="voxel_grid", voxel_size_um=12000,
                                    expected_source_sha256=remesh.core.sha256_file(source),
                                    policy=policy(sample_budget=2048, raster_resolution=96, timeout_s=120),
                                    timeout_s=360, fill_cross_sections=True)
            self.assertEqual(rc, 0, record)
            self.assertEqual(record["mesh_receipt"]["metrics"]["component_inventories"][0]["boundary_edges"], 0)
            self.assertGreater(record["distance_audit"]["forward_samples"], 2048)
            self.assertNotEqual(record["raw_sha256"], record["candidate_sha256"])
            self.assertEqual(source.read_bytes(), (root / "result/raw.glb").read_bytes())


def importlib_missing(name):
    import importlib.util
    return importlib.util.find_spec(name) is None
