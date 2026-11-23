"""Analytic geometry controls: no installed CoACD or generated-model claim."""
from asset_factory import proc as af_proc
import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asset_factory import core
from providers.coacd import adapter, contracts, geometry, worker


def box(center=(0, 0, 0), extents=(1, 1, 1)):
    _, trimesh = geometry.dependencies()
    mesh = trimesh.creation.box(extents=extents)
    mesh.apply_translation(center)
    return mesh


def gate_policy(**changes):
    policy = {"schema": "af.collision-policy/v1", "unit": "metre", "transform": "gltf-world",
              "max_hulls": 16, "max_faces_per_hull": 256, "max_lost_volume_ppm": 10000,
              "max_extra_volume_ppm": 10000, "sample_count": 4096, "sampling_seed": 17,
              "plane_tolerance_um": 1, "timeout_s": 20,
              "clearance_segments_um": [[[0, -2000000, 0], [0, 2000000, 0]]]}
    policy.update(changes)
    return policy


def gap_parts():
    return [box((-1, 0, 0)), box((1, 0, 0))]


def glb(parts):
    _, trimesh = geometry.dependencies()
    scene = trimesh.Scene()
    for index, part in enumerate(parts):
        scene.add_geometry(part, node_name=f"part_{index}", geom_name=f"part_{index}")
    return scene.export(file_type="glb")


def request(path, digest=None):
    return {"schema": "af.collision/v1", "kind": "request", "slot": "collision",
            "request_id": "synthetic-fixture", "provider_id": "coacd",
            "provider_revision": contracts.REVISION,
            "inputs": [{"role": "visual_glb", "path": str(path), "media_type": "model/gltf-binary",
                        "sha256": digest or core.sha256_file(path)}],
            "params": {"seed": 42, "threshold_ppm": 50000, "resolution": 2000, "max_hulls": 16},
            "context": {"jurisdiction": "ES", "commercial_use": False,
                        "policy_sha256": "a" * 64, "toolchain_sha256": adapter.toolchain_identity(),
                        "timeout_s": 20}}


class GateTests(unittest.TestCase):
    def assertRefusal(self, rc, code, function, *args, **kwargs):
        with self.assertRaises(contracts.Refusal) as caught:
            function(*args, **kwargs)
        self.assertEqual((caught.exception.rc, caught.exception.code), (rc, code))

    def test_identity_closed_hulls_pass_sampled_union_and_clearance(self):
        parts = gap_parts()
        metrics, stats, failed = geometry.evaluate(parts, parts, gate_policy())
        self.assertEqual(failed, [])
        self.assertEqual(metrics["lost_volume_ppm"], 0)
        self.assertEqual(metrics["extra_volume_ppm"], 0)
        self.assertEqual(metrics["hull_count"], 2)
        self.assertTrue(all(s["closed"] and s["components"] == 1 for s in stats))
        self.assertIsNone(metrics["volume_uncertainty_ppm"])

    def test_box_bridging_gap_is_red_then_split_hulls_green(self):
        parts = gap_parts()
        self.assertRefusal(5, "clearance_blocked", geometry.evaluate, parts,
                          [box(extents=(3, 1, 1))], gate_policy())
        self.assertEqual(geometry.evaluate(parts, parts, gate_policy())[2], [])

    def test_gate_mutant_ignoring_clearance_goes_red(self):
        parts = gap_parts()
        # The volume sum of a gap-bridging box still covers all source points.
        # Disable excess volume explicitly to isolate the clearance oracle.
        policy = gate_policy(max_extra_volume_ppm=1000000)
        with mock.patch.object(geometry, "segment_hits_hull", return_value=False):
            missed = geometry.evaluate(parts, [box(extents=(3, 1, 1))], policy)
        self.assertEqual(missed[2], [])  # Mutant falsely passes; the nominal rejects.
        self.assertRefusal(5, "clearance_blocked", geometry.evaluate, parts,
                          [box(extents=(3, 1, 1))], policy)

    def test_truncated_hull_fails_lost_volume_then_identity_passes(self):
        source = [box((-1, 0, 0))]
        small = [box((-1, 0, 0), (0.5, 1, 1))]
        metrics, _, failed = geometry.evaluate(source, small, gate_policy())
        self.assertGreater(metrics["lost_volume_ppm"], 400000)
        self.assertEqual(failed, ["lost_volume_exceeded"])
        self.assertEqual(geometry.evaluate(source, source, gate_policy())[2], [])

    def test_equal_volume_displaced_hull_fails_union_gate(self):
        metrics, _, failed = geometry.evaluate([box((-2, 0, 0))], [box((2, 0, 0))], gate_policy())
        self.assertEqual(metrics["lost_volume_ppm"], 1000000)
        self.assertIn("extra_volume_exceeded", failed)

    def test_overgrown_hull_fails_extra_volume(self):
        _, _, failed = geometry.evaluate([box((-2, 0, 0))],
                                        [box((-2, 0, 0), (2, 1, 1))], gate_policy())
        self.assertEqual(failed, ["extra_volume_exceeded"])

    def test_empty_hulls_and_hull_count_cap_refused(self):
        for hulls in ([], gap_parts()):
            self.assertRefusal(5, "hull_count_exceeded", geometry.evaluate,
                              gap_parts(), hulls, gate_policy(max_hulls=1))

    def test_deleted_face_is_red_then_closed_hull_green(self):
        source = box((-1, 0, 0))
        bad = source.copy()
        bad.update_faces(list(range(len(bad.faces) - 1)))
        self.assertRefusal(5, "hull_not_closed_solid", geometry.evaluate,
                          [source], [bad], gate_policy())
        self.assertEqual(geometry.evaluate([source], [source], gate_policy())[2], [])

    def test_inward_winding_hull_refused(self):
        source = box((-1, 0, 0))
        bad = source.copy()
        bad.invert()
        self.assertRefusal(5, "hull_not_closed_solid", geometry.evaluate,
                          [source], [bad], gate_policy())

    def test_nonconvex_closed_hull_refused(self):
        # A library-generated annular solid is closed but concave.
        _, trimesh = geometry.dependencies()
        mesh = trimesh.creation.annulus(r_min=0.25, r_max=1, height=0.5, sections=8)
        mesh.apply_translation((-2, 0, 0))
        self.assertRefusal(5, "hull_nonconvex", geometry.evaluate, [mesh], [mesh], gate_policy())

    def test_face_budget_refused(self):
        parts = [box((-1, 0, 0))]
        self.assertRefusal(5, "hull_face_budget_exceeded", geometry.evaluate,
                          parts, parts, gate_policy(max_faces_per_hull=4))

    def test_open_source_is_cannot_see_never_pass(self):
        bad = box((-1, 0, 0))
        bad.update_faces(list(range(11)))
        self.assertRefusal(3, "source_volume_unavailable", geometry.evaluate,
                          [bad], [box((-1, 0, 0))], gate_policy())

    def test_no_clearance_evidence_is_cannot_see(self):
        parts = [box((-1, 0, 0))]
        self.assertRefusal(3, "clearance_unmeasured", geometry.evaluate,
                          parts, parts, gate_policy(clearance_segments_um=[]))

    def test_clearance_crossing_source_is_invalid_not_success(self):
        parts = [box()]
        self.assertRefusal(2, "probe_crosses_source", geometry.evaluate, parts, parts, gate_policy())

    def test_probe_entirely_inside_source_is_invalid(self):
        policy = gate_policy(clearance_segments_um=[[[0, 0, 0], [1000, 0, 0]]])
        self.assertRefusal(2, "probe_inside_source", geometry.evaluate, [box()], [box()], policy)

    def test_sampling_work_budget_is_enforced(self):
        parts = gap_parts()
        with mock.patch.object(geometry, "MAX_PAIRS", 200):
            self.assertRefusal(6, "volume_budget_exceeded", geometry.evaluate,
                              parts, parts, gate_policy())

    def test_exact_coordinate_weld_preserves_seam_split_closure(self):
        np, trimesh = geometry.dependencies()
        mesh = box((-1, 0, 0))
        seam = trimesh.Trimesh(vertices=mesh.vertices[mesh.faces].reshape(-1, 3),
            faces=np.arange(len(mesh.faces) * 3).reshape(-1, 3), process=False)
        _, stats = geometry.inventory(seam)
        self.assertTrue(stats["closed"])
        self.assertEqual(stats["boundary_edges"], 0)

    def test_nonfinite_geometry_refused(self):
        bad = box()
        bad.vertices[0, 0] = float("nan")
        self.assertRefusal(2, "mesh_invalid", geometry.inventory, bad)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "raw.glb"
        self.source.write_bytes(glb(gap_parts()))
        self.original = self.source.read_bytes()

    def test_draft_request_and_blocked_manifest_validate(self):
        self.assertEqual(contracts.validate_request(request(self.source)), "registered")
        provider = contracts.schema("provider.json")
        core.validate(provider, contracts.schema("provider.schema.json"))
        self.assertFalse(provider["runnable"])
        self.assertEqual(provider["blocked_reason"], "research_only_unverified_runtime")
        self.assertIsNone(provider["runtime"]["ram_peak_gb_measured"])

    def test_unregistered_contract_stays_refused_by_current_validator(self):
        from asset_factory.contracts import validate_provider
        provider = contracts.schema("provider.json")
        validate_provider(provider)
        provider["slot"] = "unregistered_collision"
        with self.assertRaises(core.ContractError) as caught:
            validate_provider(provider)
        self.assertEqual(caught.exception.rc, 2)
        self.assertEqual(caught.exception.code, "schema_mismatch")

    def test_missing_bool_seed_and_unpinned_revision_refused(self):
        for change in (None, True, -1):
            req = request(self.source)
            if change is None:
                del req["params"]["seed"]
            else:
                req["params"]["seed"] = change
            with self.assertRaises(contracts.Refusal):
                contracts.validate_request(req)
        req = request(self.source)
        req["provider_revision"] = "main"
        with self.assertRaises(contracts.Refusal) as caught:
            contracts.validate_request(req)
        self.assertEqual((caught.exception.rc, caught.exception.code), (2, "unpinned_revision"))

    def test_repeated_input_roles_refused(self):
        req = request(self.source)
        req["inputs"] *= 2
        with self.assertRaises(contracts.Refusal):
            contracts.validate_request(req)

    def test_registered_contract_rejects_blank_request_identity(self):
        req = request(self.source)
        req["request_id"] = "   "
        with self.assertRaises(contracts.Refusal) as caught:
            contracts.validate_request(req)
        self.assertEqual((caught.exception.rc, caught.exception.code), (2, "schema_mismatch"))

    def test_policy_requires_integer_numbers_and_bounded_target(self):
        for key, value in (("sample_count", True), ("max_lost_volume_ppm", 10001),
                           ("plane_tolerance_um", -1), ("sampling_seed", 2.5)):
            with self.assertRaises((contracts.Refusal, core.ContractError)):
                contracts.validate_policy(gate_policy(**{key: value}))

    def test_external_uri_in_glb_refused(self):
        import json
        import struct
        doc = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"uri": "secret.bin"}]}).encode()
        doc += b" " * (-len(doc) % 4)
        raw = b"glTF" + struct.pack("<IIII", 2, 20 + len(doc), len(doc), 0x4E4F534A) + doc
        with self.assertRaises(contracts.Refusal) as caught:
            geometry.load_glb(raw)
        self.assertEqual(caught.exception.code, "external_resource_refused")

    def test_tampered_source_hash_is_stale(self):
        rec = adapter.inspect_source(self.source, expected_sha256="a" * 64)
        self.assertEqual(rec["rc"], 4)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_missing_coacd_is_cannot_see(self):
        info = {"coacd_available": False, "coacd_version": None, "code_sha256": None}
        with self.assertRaises(contracts.Refusal) as caught:
            worker.require_runtime(info)
        self.assertEqual((caught.exception.rc, caught.exception.code), (3, "coacd_unavailable"))

    def test_unqualified_and_wrong_version_runtime_refused(self):
        for version, rc, code in (("1.0.14", 3, "runtime_pin_unproven"),
                                  ("0.0.0", 4, "runtime_version_stale")):
            with self.assertRaises(contracts.Refusal) as caught:
                worker.require_runtime({"coacd_available": True, "coacd_version": version,
                                        "code_sha256": "a" * 64})
            self.assertEqual((caught.exception.rc, caught.exception.code), (rc, code))

    def test_native_wrapper_forwards_seed_and_rejects_unknown_api(self):
        import types
        seen = {}
        def run(mesh, *, seed, threshold, resolution, max_convex_hull, preprocess_mode):
            seen.update(seed=seed, threshold=threshold, resolution=resolution,
                        max_convex_hull=max_convex_hull, preprocess_mode=preprocess_mode)
            return [(mesh[0], mesh[1])]
        fake = types.SimpleNamespace(Mesh=lambda v, f: (v, f), run_coacd=run)
        with mock.patch.dict(sys.modules, coacd=fake):
            self.assertEqual(len(worker.native_decompose(gap_parts(), request(self.source)["params"])), 1)
            self.assertEqual(seen["seed"], 42)
            self.assertEqual(seen["threshold"], 0.05)
            self.assertEqual(seen["preprocess_mode"], "off")
            fake.run_coacd = lambda mesh: []
            with self.assertRaises(contracts.Refusal):
                worker.native_decompose(gap_parts(), request(self.source)["params"])

    def test_source_transform_is_applied_before_measurement(self):
        _, trimesh = geometry.dependencies()
        scene = trimesh.Scene()
        transform = trimesh.transformations.translation_matrix([3, 4, 5])
        scene.add_geometry(box(), transform=transform)
        mesh = geometry.load_glb(scene.export(file_type="glb"))[0]
        self.assertEqual(mesh.bounds.tolist(), [[2.5, 3.5, 4.5], [3.5, 4.5, 5.5]])

    def test_worker_gates_real_exported_glb_and_binds_hashes(self):
        policy = gate_policy()
        job = adapter._job("gate", self.original, policy, self.original)
        (self.folder / "source.glb").write_bytes(self.original)
        (self.folder / "collision.glb").write_bytes(self.original)
        rec = worker.measure(job, self.folder)
        self.assertEqual(rec["rc"], 0, rec)
        self.assertEqual(rec["source_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(rec["policy_sha256"], core.sha256_json(policy))
        self.assertFalse(rec["replay_verified"])
        self.assertFalse(rec["thresholds_calibrated"])
        self.assertIsNone(rec["metrics"]["volume_uncertainty_ppm"])

    def test_worker_tampered_candidate_refused(self):
        policy = gate_policy()
        job = adapter._job("gate", self.original, policy, b"wrong")
        (self.folder / "source.glb").write_bytes(self.original)
        (self.folder / "collision.glb").write_bytes(self.original)
        rec = worker.measure(job, self.folder)
        self.assertEqual(rec["rc"], 4)
        self.assertIn("candidate_stale", rec["reason_codes"])

    def test_timeout_kills_and_reaps_owned_group(self):
        child = mock.Mock(pid=87654321, returncode=-9)
        child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), None]
        with mock.patch.object(adapter.af_proc, "popen", return_value=child), \
             mock.patch.object(adapter.af_proc, "signal_group") as kill, \
             mock.patch.object(adapter.af_proc, "wait_exit", side_effect=lambda process, timeout: process.wait(timeout=timeout)):
            # A-SAFE: the fake observer never reaches waitid or a signal syscall.
            with self.assertRaises(contracts.Refusal) as caught:
                adapter._spawn(self.folder, 1)
        self.assertEqual(caught.exception.rc, 6)
        kill.assert_called_once_with(child, adapter.signal.SIGKILL)
        self.assertEqual(child.wait.call_count, 2)

    def test_real_hung_child_is_killed_and_reaped(self):
        import os
        original_popen = af_proc.popen
        children = []
        def hung(_argv, **kwargs):
            child = original_popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(adapter.af_proc, "popen", side_effect=hung):
            with self.assertRaises(contracts.Refusal) as caught:
                adapter._spawn(self.folder, 0.05)
        self.assertEqual(caught.exception.rc, 6)
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            af_proc.signal_pid(children[0], 0)

    def test_native_rc_one_is_never_accepted(self):
        child = mock.Mock(pid=87654321, returncode=1)
        with mock.patch.object(adapter.af_proc, "popen", return_value=child), \
             mock.patch.object(adapter.af_proc, "signal_group"), \
             mock.patch.object(adapter.af_proc, "wait_exit", return_value=1):
            with self.assertRaises(contracts.Refusal) as caught:
                adapter._spawn(self.folder, 1)
        self.assertEqual((caught.exception.rc, caught.exception.code), (2, "backend_contract"))

    def test_no_replace_publication_preserves_existing_directory(self):
        source, target = self.folder / "stage", self.folder / "existing"
        source.mkdir()
        target.mkdir()
        (target / "keep").write_bytes(b"caller bytes")
        with self.assertRaises(contracts.Refusal) as caught:
            adapter._rename_new(source, target)
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual((target / "keep").read_bytes(), b"caller bytes")
        self.assertTrue(source.exists())

    def test_new_directory_publication_is_atomic(self):
        source, target = self.folder / "stage", self.folder / "new"
        source.mkdir()
        (source / "data").write_bytes(b"measured")
        adapter._rename_new(source, target)
        self.assertFalse(source.exists())
        self.assertEqual((target / "data").read_bytes(), b"measured")

    def test_refused_production_never_starts_or_changes_caller_bytes(self):
        target = self.folder / "caller"
        target.mkdir()
        (target / "keep").write_bytes(b"unchanged")
        with mock.patch.object(adapter, "_spawn") as spawn:
            out, rec = adapter.decompose(request(self.source), gate_policy(),
                provider=contracts.schema("provider.json"), factory={}, territory_policy={}, output_dir=target)
        self.assertNotEqual(rec["rc"], 0)
        self.assertEqual(out["outputs"], [])
        spawn.assert_not_called()
        self.assertEqual((target / "keep").read_bytes(), b"unchanged")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_usage_error_is_rc_two(self):
        with mock.patch.object(core, "emit", side_effect=lambda obj: obj["rc"]):
            self.assertEqual(adapter.main(["run"]), 2)

    def simulated_bundle(self):
        """Real gate/export/publication, with an explicitly synthetic backend."""
        from providers.coacd import worker
        target = self.folder / "published"
        def spawn(folder, _timeout):
            job = core.load_json(folder / "job.json", max_bytes=1024 * 1024)
            with mock.patch.object(worker, "require_runtime"), \
                 mock.patch.object(worker, "native_decompose", return_value=gap_parts()):
                rec = worker.measure(job, folder)
            core.atomic_write_json(folder / "worker-receipt.json", rec)
            return rec
        with mock.patch.object(adapter, "_authorise", return_value={"synthetic": True}), \
             mock.patch.object(adapter, "_spawn", side_effect=spawn):
            out, rec = adapter.decompose(request(self.source), gate_policy(),
                provider=contracts.schema("provider.json"), factory={}, territory_policy={}, output_dir=target)
        self.assertEqual(rec["rc"], 0, rec)
        self.assertEqual(out["rc"], 0)
        self.assertEqual({row["role"] for row in out["outputs"]},
                         {"collision_glb", "collision_manifest"})
        self.assertEqual(self.source.read_bytes(), self.original)
        return target

    def test_protocol_publication_positive_with_explicit_fake_backend(self):
        target = self.simulated_bundle()
        manifest = core.load_json(target / "manifest.json", max_bytes=1024 * 1024)
        self.assertEqual(len(manifest["hulls"]), 2)
        self.assertTrue(all(row["source_sha256"] == core.sha256_file(self.source)
                            for row in manifest["hulls"]))
        self.assertEqual((target / "manifest.json").read_text(), core.canonical_json(manifest))
        rec = adapter.verify_bundle(target, expected_source_sha256=core.sha256_file(self.source),
                                    policy=gate_policy())
        self.assertEqual(rec["rc"], 0, rec)

    def test_manifest_source_tamper_is_stale_then_repaired_green(self):
        target = self.simulated_bundle()
        path = target / "manifest.json"
        original = path.read_bytes()
        manifest = core.load_json(path, max_bytes=1024 * 1024)
        manifest["hulls"][0]["source_sha256"] = "b" * 64
        core.atomic_write_json(path, manifest)
        rec = adapter.verify_bundle(target, expected_source_sha256=core.sha256_file(self.source),
                                    policy=gate_policy())
        self.assertEqual((rec["rc"], rec["reason_codes"]), (4, ["hull_source_stale"]))
        path.write_bytes(original)
        self.assertEqual(adapter.verify_bundle(target,
            expected_source_sha256=core.sha256_file(self.source), policy=gate_policy())["rc"], 0)

    def test_obj_byte_tamper_refused_even_when_glb_gate_still_passes(self):
        target = self.simulated_bundle()
        with (target / "hull_0000.obj").open("ab") as stream:
            stream.write(b"\n# changed\n")
        rec = adapter.verify_bundle(target, expected_source_sha256=core.sha256_file(self.source),
                                    policy=gate_policy())
        self.assertEqual((rec["rc"], rec["reason_codes"]), (4, ["hull_bytes_stale"]))

    def test_bundle_path_escape_and_escaping_symlink_refused(self):
        target = self.simulated_bundle()
        path = target / "manifest.json"
        manifest = core.load_json(path, max_bytes=1024 * 1024)
        manifest["hulls"][0]["path"] = "../raw.glb"
        core.atomic_write_json(path, manifest)
        rec = adapter.verify_bundle(target, expected_source_sha256=core.sha256_file(self.source),
                                    policy=gate_policy())
        self.assertEqual((rec["rc"], rec["reason_codes"]), (2, ["bundle_path_invalid"]))
        path.unlink()
        path.symlink_to(self.source)
        rec = adapter.verify_bundle(target, expected_source_sha256=core.sha256_file(self.source),
                                    policy=gate_policy())
        self.assertEqual((rec["rc"], rec["reason_codes"]), (2, ["bundle_path_invalid"]))

    def test_source_drift_during_worker_prevents_publication(self):
        def spawn(folder, timeout):
            job = core.load_json(folder / "job.json", max_bytes=1024 * 1024)
            with mock.patch.object(worker, "require_runtime"), \
                 mock.patch.object(worker, "native_decompose", return_value=gap_parts()):
                rec = worker.measure(job, folder)
            core.atomic_write_json(folder / "worker-receipt.json", rec)
            self.source.write_bytes(b"caller independently changed the source")
            return rec
        target = self.folder / "not-published"
        req = request(self.source)
        with mock.patch.object(adapter, "_authorise", return_value={"synthetic": True}), \
             mock.patch.object(adapter, "_spawn", side_effect=spawn):
            out, rec = adapter.decompose(req, gate_policy(), provider=contracts.schema("provider.json"),
                                         factory={}, territory_policy={}, output_dir=target)
        self.assertEqual(rec["rc"], 4)
        self.assertEqual(out["outputs"], [])
        self.assertFalse(target.exists())

    def test_empty_existing_output_directory_is_never_replaced(self):
        source, target = self.folder / "stage", self.folder / "empty-caller"
        source.mkdir()
        target.mkdir()
        with self.assertRaises(contracts.Refusal):
            adapter._rename_new(source, target)
        self.assertTrue(source.exists())
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
