"""WIRE-C controls for the native pipeline topology adapter."""
import copy
import json
import shutil
import time
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from asset_factory import core, domains
from tools.blender import topology
from tools.blender.glb_io import decode_glb
from tools.blender.topology_glb import digest, inspect_glb
from tools.input.evidence import bytes_digest
from tools.mesh import raw_admission
from tools.pipeline import engine, stages, topology_bridge
from tools.pipeline.engine import Refusal
from test_e2e_blender import options
from test_f2u1_mesh import glb, policy
from test_n18_int_geometry import fixture_scope
import test_d18_geo_certificate as geo_fixture
import test_f2u2_blender as live_fixture


class SolverPolicy(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.opts = options(self.root)
        self.opts.update(method="quadriflow", target_faces=3)
        self.raw = self.root / "raw.glb"
        self.raw.write_bytes(glb())
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        cage = self.root / "registered.cage.json"
        cage.write_bytes(b"fixture")
        self.cage = engine.file_ref(cage)
        cage_input = patch.object(stages.NativeStages, "_topology_cage_input", return_value=self.cage)
        cage_input.start()
        self.addCleanup(cage_input.stop)
        self.solver = {"quadriflow_target_quads": 1, "production_evidence": False,
                       "quad_cage_digest": "sha256:" + self.cage["sha256"]}

    def configured(self, **solver_overrides):
        solver = dict(self.solver, **solver_overrides)
        core.atomic_write_json(self.opts["mesh_policy"], policy(geometry_scope={"quadriflow_solver": solver}))
        return self.backend()

    def backend(self):
        return stages.NativeStages(source=self.raw, reference=self.reference, provider="byo",
                                   options=dict(self.opts))

    def assert_refused_before_launch(self, backend, rc, reason):
        with (patch.object(topology, "default_runner", side_effect=AssertionError("unexpected Blender launch")) as launch,
              self.assertRaises(Refusal) as caught):
            backend.stage_topology(self.root, self.raw)
        launch.assert_not_called()
        self.assertEqual((caught.exception.rc, caught.exception.reason), (rc, reason))
        self.assertFalse((self.root / "topology").exists())

    def test_missing_solver_policy_refuses(self):
        self.assert_refused_before_launch(self.backend(), 3, "quadriflow_solver_policy_missing")

    def test_solver_requires_mesh_policy_at_identity(self):
        del self.opts["mesh_policy"]
        with self.assertRaises(Refusal) as caught:
            self.backend().identity("topology", self.raw)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "mesh_policy_absent"))

    def test_caller_solver_target_refuses_even_with_policy(self):
        self.opts["quadriflow_target_quads"] = 1
        self.assert_refused_before_launch(self.configured(), 2, "quadriflow_solver_option_refused")

    def test_solver_policy_must_use_pinned_bytes(self):
        first = self.configured()
        self.configured(quadriflow_target_quads=2)
        with self.assertRaises(Refusal) as caught:
            first.identity("topology", self.raw)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "bytes_stale"))
        self.assert_refused_before_launch(first, 4, "bytes_stale")

    def test_solver_policy_refuses_malformed_geometry_scope(self):
        core.atomic_write_json(self.opts["mesh_policy"], policy(geometry_scope=[]))
        self.assert_refused_before_launch(self.backend(), 2, "geometry_scope_invalid")

    def test_solver_policy_is_closed_and_fixture_only(self):
        for invalid in ([], dict(self.solver, unknown=1), dict(self.solver, production_evidence=True),
                        dict(self.solver, quadriflow_target_quads=True),
                        dict(self.solver, quadriflow_target_quads=0),
                        dict(self.solver, quad_cage_digest="not-a-digest")):
            with self.subTest(solver=invalid):
                core.atomic_write_json(self.opts["mesh_policy"], policy(geometry_scope={"quadriflow_solver": invalid}))
                self.assert_refused_before_launch(self.backend(), 2, "quadriflow_solver_policy_invalid")

    def test_solver_policy_must_match_registered_cage(self):
        backend = self.configured(quad_cage_digest="sha256:" + "f" * 64)
        self.assert_refused_before_launch(backend, 4, "quadriflow_solver_cage_stale")

    def test_solver_target_exceeding_cap_refuses_before_launch(self):
        self.assert_refused_before_launch(self.configured(quadriflow_target_quads=2), 2,
                                          "quadriflow_solver_target_invalid")

    def test_bridge_requires_explicit_solver_target(self):
        for method, target in (("quadriflow", None), ("decimate", 1)):
            with self.subTest(method=method):
                with (patch.object(topology, "default_runner",
                                   side_effect=AssertionError("unexpected Blender launch")) as launch,
                      self.assertRaises(Refusal) as caught):
                    topology_bridge.run_candidate(self.raw, self.root / "blocked", method=method,
                                                  target_faces=3, seed=42, policy=policy(), timeout_s=20,
                                                  quadriflow_target_quads=target)
                self.assertEqual((caught.exception.rc, caught.exception.reason),
                                 (2, "quadriflow_solver_target_invalid"))
                launch.assert_not_called()
                self.assertFalse((self.root / "blocked").exists())

    def test_solver_target_is_dispatched_and_recorded(self):
        with patch.object(topology_bridge, "run_candidate", return_value={"rc": 0, "checks": []}) as run:
            self.configured().stage_topology(self.root, self.raw)
        self.assertEqual(run.call_args.kwargs["quadriflow_target_quads"], self.solver["quadriflow_target_quads"])
        self.assertEqual(engine.load(self.root / "topology.json")["quadriflow_solver"], self.solver)

    def test_solver_policy_value_changes_topology_identity_and_cache(self):
        self.raw.write_bytes(glb([{"name": "body"}, {"name": "other"}]))
        self.opts["target_faces"] = 5
        native = self.configured()
        refs, parameters, _ = native.identity("topology", self.raw)
        self.assertEqual(parameters["quadriflow_solver"], self.solver)
        topo = self.root / "topology.json"
        core.atomic_write_json(topo, {"quadriflow_solver": self.solver})
        proof = {}
        proof_path = self.root / stages.GEOMETRY_RECEIPT
        core.atomic_write_json(proof_path, proof)
        subject = self.root / "master.glb"
        cage = subject.with_suffix(".cage.json")
        cage.write_bytes(b"cache-fixture")
        record = {"parameters": parameters, "subject": str(subject), "inputs": refs,
                  "evidence": [engine.file_ref(path) for path in (proof_path, cage, topo)]}
        with patch.object(stages.NativeStages, "_geometry_proof", return_value=proof):
            native.verify_cached("topology", self.root, record)
            changed = self.configured(quadriflow_target_quads=2)
            new_refs, new_parameters, _ = changed.identity("topology", self.raw)
            self.assertNotEqual(refs[0]["sha256"], new_refs[0]["sha256"])
            self.assertEqual(new_parameters["quadriflow_solver"]["quadriflow_target_quads"], 2)
            with self.assertRaises(Refusal) as caught:
                changed.verify_cached("topology", self.root, dict(record, inputs=new_refs))
            self.assertEqual((caught.exception.rc, caught.exception.reason),
                             (4, "quadriflow_solver_cache_stale"))

            stale_parameters = dict(parameters, quadriflow_solver=new_parameters["quadriflow_solver"])
            self.configured()
            with self.assertRaises(Refusal) as caught:
                native.verify_cached("topology", self.root, dict(record, parameters=stale_parameters))
            self.assertEqual((caught.exception.rc, caught.exception.reason),
                             (4, "quadriflow_solver_cache_stale"))

            core.atomic_write_json(topo, {"quadriflow_solver": new_parameters["quadriflow_solver"]})
            record["evidence"][-1] = engine.file_ref(topo)
            with self.assertRaises(Refusal) as caught:
                native.verify_cached("topology", self.root, record)
            self.assertEqual((caught.exception.rc, caught.exception.reason),
                             (4, "quadriflow_solver_cache_stale"))

            core.atomic_write_json(topo, {"quadriflow_solver": dict(self.solver, production_evidence=0)})
            record["evidence"][-1] = engine.file_ref(topo)
            with self.assertRaises(Refusal) as caught:
                native.verify_cached("topology", self.root, record)
            self.assertEqual((caught.exception.rc, caught.exception.reason),
                             (4, "quadriflow_solver_cache_stale"))


class CageRefusals(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.raw = Path(scratch.name) / "raw.glb"
        self.candidate = self.raw.with_name("master.glb")
        self.receipt = {"rc": 0, "method": "quadriflow", "checks": [{"role": "master"}]}

    def test_geometry_proof_is_required(self):
        with self.assertRaisesRegex(TypeError, "geometry_proof"):
            topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt, target_faces=12)

    def test_repinned_structural_negatives(self):
        cage = {"schema": "af.quad-cage/v1", "frame": domains.FRAME,
                "vertices_um": [[x, y, 0] for x, y in
                                ((0, 0), (1, 0), (1, 1), (0, 1), (2, 0), (2, 1))],
                "quads": [[0, 1, 2, 3], [1, 4, 5, 2]],
                "triangles": [[0, 1, 2], [0, 2, 3], [1, 4, 5], [1, 5, 2]],
                "triangle_quads": [0, 0, 1, 1]}
        path = self.candidate.with_suffix(".cage.json")
        path.write_bytes(topology.canonical(cage).encode())
        self.assertEqual(topology.read_quad_cage(self.candidate), (digest(path.read_bytes()), 2))
        blob = geo_fixture.glb(geo_fixture.TET, geo_fixture.FACES)
        self.raw.write_bytes(blob)
        self.candidate.write_bytes(blob)
        candidate_sha = digest(blob)
        self.receipt.update(raw_sha256=candidate_sha, candidate_sha256=candidate_sha,
                            toolchain_sha256=topology_bridge._quad_toolchain(shutil.which("blender")))
        self.receipt["checks"][0].update(candidate_sha256=candidate_sha, requested_faces=12, achieved_faces=4)
        for name, changes, expected in (
            ("quad_oob", [("quads", 0, [6, 1, 2, 3])], (2, "quad_cage_quad_index_invalid")),
            ("tri_oob", [("triangles", 0, [6, 1, 2])], (2, "quad_cage_triangle_index_invalid")),
            ("coverage_gap", [("triangle_quads", 0, 1)], (4, "cage_export_coverage")),
            ("dup_tri", [("triangles", 1, [2, 1, 0])], (2, "quad_cage_duplicate_triangle")),
            ("dup_owner", [("triangles", 2, [0, 1, 2]), ("triangle_quads", 2, 0)],
             (2, "quad_cage_duplicate_triangle")),
            ("owner_oob", [("triangle_quads", 0, 2)], (2, "quad_cage_owner_invalid")),
            ("degenerate_quad", [("quads", 0, [0, 0, 2, 3])], (2, "quad_cage_degenerate_quad")),
            ("degenerate_triangle", [("triangles", 0, [0, 0, 2])], (2, "quad_cage_degenerate_triangle")),
            ("wrong_owner_pair", [("triangle_quads", 0, 1), ("triangle_quads", 2, 0)],
             (2, "quad_cage_owner_mismatch")),
        ):
            with self.subTest(name=name):
                mutated = copy.deepcopy(cage)
                for field, index, value in changes:
                    mutated[field][index] = value
                data = topology.canonical(mutated).encode()
                path.write_bytes(data)
                pin = digest(data)
                self.receipt["checks"][0].update(quad_cage_path=path.name, quad_cage_sha256=pin)
                proof = {"schema": "af.mesh-regression/v2", "rc": 0, "measurement_rc": 0, "verdict": "PASS",
                         "subjects": ["sha256:" + candidate_sha] * 2,
                         "levels": [{}, {"quad_cage": {"cage_digest": "sha256:" + pin, "quads": 2}}]}
                with self.assertRaises(Refusal) as caught:
                    topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt,
                                                     target_faces=12, geometry_proof=proof)
                self.assertEqual((caught.exception.rc, caught.exception.reason), expected)

    def test_triangle_only_refuses_missing_cage(self):
        self.candidate.write_bytes(geo_fixture.glb(geo_fixture.TET, geo_fixture.FACES))
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt,
                                             target_faces=12, geometry_proof=None)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "required_quad_cage_missing"))


class SphereRegistration:
    """Fixture-only registration against the authored sphere and its radial normals."""

    def registration(self, blob, domain, patches):
        doc, binary = decode_glb(blob)
        prim = doc["meshes"][0]["primitives"][0]
        positions = raw_admission._accessor(doc, binary, prim["attributes"]["POSITION"], "fff")
        representatives = {}
        welds = [representatives.setdefault(point, index) for index, point in enumerate(positions)]
        # Frozen triangle ordinals on the two exact sphere exports. The three
        # GEO region labels select surface patches for this procedural fixture.
        ordinals = (556, 552, 593) if domain is None else (1036, 586, 446)
        regions = {name: ["0:0", [ordinal]]
                   for name, ordinal in zip(("face", "left_hand", "right_hand"), ordinals)}
        pin = super().registration(blob, domain, regions)
        record = json.loads(self.store.read("geo-registration", pin))
        record["welded_index_maps"] = {"0:0": welds}
        if domain is not None:
            record.update(quad_cage=self.cage_pin, quad_normals=self.store.add("geo-quad-normals", {
                "schema": "af.quad-normals/v1", "domain": domain,
                "subject": bytes_digest(blob), "cage": self.cage_pin, "normals": self.normals}))
        return self.store.add("geo-registration", record)


class LiveCage(unittest.TestCase):
    @staticmethod
    def _observe_blender(runner, argv, timeout, **kwargs):
        script = argv.index("--python")
        expression = ("import os; print('WIRE_C_BLENDER_CPU', os.nice(0), "
                      "','.join(map(str, sorted(os.sched_getaffinity(0)))))")
        argv = [*argv[:script], "--python-expr", expression, *argv[script:]]
        rc, output = runner(argv, timeout, **kwargs)
        expected = "WIRE_C_BLENDER_CPU 15 " + ",".join(map(str, range(16)))
        observed, = [line for line in output.splitlines() if line.startswith("WIRE_C_BLENDER_CPU ")]
        if observed != expected:
            raise AssertionError(observed)
        print(observed, flush=True)
        return rc, output

    @classmethod
    def setUpClass(cls):
        live_fixture.BlenderWitnesses.setUpClass.__func__(cls)
        cls.output = cls.base / "quadriflow"
        cls.candidate = cls.output / "master.glb"
        start = time.monotonic()
        observe = partial(cls._observe_blender, topology.default_runner)
        with patch.object(topology, "default_runner", observe):
            cls.receipt = topology_bridge.run_candidate(
                cls.raw, cls.output, method="quadriflow", target_faces=1200, seed=42,
                policy=policy(sample_budget=6000, raster_resolution=96, timeout_s=90,
                              component_allowlist={"Icosphere": 1}),
                timeout_s=120, quadriflow_target_quads=500)
        print(f"BRIDGE wall_s={time.monotonic() - start:.3f} {json.dumps(cls.receipt, sort_keys=True)}", flush=True)

    def test_live_cage(self):
        pin, _, _ = topology_bridge._check_quad_cage_custody(self.raw, self.candidate, self.receipt, target_faces=1200)
        cage = self.candidate.with_suffix(".cage.json").read_bytes()
        self.assertEqual(pin, digest(cage))
        self.assertEqual(inspect_glb(self.candidate)["faces"], 2 * len(json.loads(cage)["quads"]))
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt,
                                             target_faces=1200, geometry_proof=None)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "quad_cage_geometry_unqualified"))

    def test_emitted_faces_still_enforce_cap(self):
        output = self.base / "overshoot"
        with self.assertRaises(topology.TopologyError) as caught:
            topology_bridge.run_candidate(
                self.raw, output, method="quadriflow", target_faces=1200, seed=42,
                policy=policy(sample_budget=6000, raster_resolution=96, timeout_s=90,
                              component_allowlist={"Icosphere": 1}),
                timeout_s=120, quadriflow_target_quads=600)
        self.assertEqual((caught.exception.rc, caught.exception.reason),
                         (5, "face_budget_unmet_or_fake_lod"))
        plan = engine.load(output / "operation.json")
        self.assertEqual(plan["target_faces"], 600)
        self.assertGreater(inspect_glb(output / "master.glb")["faces"], 1200)

    def test_unavailable_nice_refuses_before_worker(self):
        with (patch.object(topology_bridge.os, "nice", return_value=19),
              patch.object(topology, "default_runner") as launch,
              self.assertRaises(Refusal) as caught):
            topology_bridge.run_candidate(
                self.raw, self.base / "wrong-nice", method="quadriflow", target_faces=1200, seed=42,
                policy=policy(sample_budget=6000, raster_resolution=96, timeout_s=90,
                              component_allowlist={"Icosphere": 1}),
                timeout_s=120, quadriflow_target_quads=500)
        launch.assert_not_called()
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "topology_nice_unavailable"))

    def test_live_geometry_certificate(self):
        class SphereAuthority(SphereRegistration, geo_fixture.Gates):
            pass

        cage = json.loads(self.candidate.with_suffix(".cage.json").read_bytes())
        geometry = SphereAuthority()
        geometry.setUp()
        self.assertEqual(self.receipt["raw_sha256"],
                         "02411124a9f4b2f2921130b0fc0d01e180958f9f3e10e51598e55c6e14758fda")
        self.assertEqual(self.receipt["candidate_sha256"],
                         "e1d01ce8887d80a27b3d78ec5fe63cb68aaa226e87e2c4f9f09c8f24ef9d001b")
        geometry.raw = self.original
        geometry.cage_pin = geometry.store.add("geo-quad-cage", cage)
        points = [list(map(domains.rational, vertex)) for vertex in cage["vertices_um"]]
        geometry.normals = [[round(sum(points[index][axis] for index in quad) / 4) for axis in range(3)]
                            for quad in cage["quads"]]
        geometry.intent.update(forbidden_boxes_um=[[[-2200000, -100, -100], [-2100000, 100, 100]]],
                               extent_ranges_um=[[1_980_000, 2_030_000]] * 3,
                               max_glb_bytes=1_000_000, max_faces=1500, max_box_checks=1500)
        geometry.policy.update(source=bytes_digest(self.original), reference_length_um=3_420_000,
                               intent=geometry.store.add("raw-intent", geometry.intent),
                               regions_um={name: 68_399 for name in geometry.patches},
                               normal_cos2_min_ppm=800_000, projected_max_millipx=2000,
                               raster_support_max_millipx=2000, refinements=3, max_work=60_000_000,
                               face_budgets=[1200],
                               quad_levels={"1": {"jacobian2_min_ppm": 1, "split_cos2_min_ppm": 1}})
        for view, intrinsics in (
            ("face", [[48, 1], [48, 1], [70, 3], [331, 14]]),
            ("hands", [[32, 1], [32, 1], [283, 12], [331, 14]]),
        ):
            geometry.policy["views"][view]["camera"]["intrinsics"] = intrinsics
        geometry.policy["selection"] = geometry.store.add("geo-input-selection", geometry.selection)
        start = time.monotonic()
        pins = fixture_scope(geometry, self.base / "authority", self.candidate.read_bytes())
        authority_wall = time.monotonic() - start
        opts = options(self.base)
        opts.update(method="quadriflow", target_faces=1200, timeout_s=120)
        mesh_policy = policy(sample_budget=6000, raster_resolution=96, timeout_s=90,
                             component_allowlist={"Icosphere": 1})
        authority = self.base / "authority"
        solver = {"quadriflow_target_quads": 500, "production_evidence": False,
                  "quad_cage_digest": geometry.cage_pin}
        core.atomic_write_json(opts["mesh_policy"], dict(mesh_policy, geometry_scope={
            "authority": str(authority), "topology": pins, "quadriflow_solver": solver}))
        reference = self.base / "reference.png"
        reference.write_bytes(b"\x89PNG\r\n\x1a\n")
        backend = stages.NativeStages(source=self.raw, reference=reference, provider="byo", options=opts)
        pipeline = engine.Pipeline(self.base / "pipeline", backend)
        pipeline.stages = ("input", "provider", "topology")
        start = time.monotonic()
        row = pipeline.run()["stages"][-1]
        stage_wall = time.monotonic() - start
        record = engine.load(row["receipt"]["path"])
        self.assertEqual((row["stage"], row["rc"], row["cache_hit"]), ("topology", 0, False), record)
        directory = Path(row["receipt"]["path"]).parent
        self.assertEqual(record["parameters"]["quadriflow_solver"], solver)
        self.assertEqual(engine.load(directory / "topology.json")["quadriflow_solver"], solver)
        proof = engine.load(directory / stages.GEOMETRY_RECEIPT)
        self.assertEqual((proof["rc"], proof["verdict"]), (0, "PASS"), proof["reason_codes"])
        pin = topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt,
                                               target_faces=1200, geometry_proof=proof)
        self.assertEqual("sha256:" + pin, proof["levels"][-1]["quad_cage"]["cage_digest"])
        staged_cage = Path(record["subject"]).with_suffix(".cage.json")
        authority_cage = authority / "geo-quad-cage" / pin
        self.assertIn(engine.file_ref(staged_cage), record["evidence"])
        self.assertIn(engine.file_ref(authority_cage), record["inputs"])
        missing_receipt = copy.deepcopy(record)
        missing_receipt["evidence"].remove(engine.file_ref(directory / "topology.json"))
        with self.assertRaises(Refusal) as caught:
            backend.verify_cached("topology", directory, missing_receipt)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "topology_receipt_missing"))
        # Only the stage hook re-verifies the cage bindings inside topology.json:
        # a re-pinned receipt with a different toolchain must still be refused.
        topology_receipt = directory / "topology.json"
        receipt_bytes = topology_receipt.read_bytes()
        forged = json.loads(receipt_bytes)
        forged["toolchain_sha256"] = "0" * 64
        core.atomic_write_json(topology_receipt, forged)
        repinned = copy.deepcopy(record)
        repinned["evidence"] = [engine.file_ref(topology_receipt) if Path(ref["path"]) == topology_receipt
                                else ref for ref in record["evidence"]]
        try:
            with self.assertRaises(Refusal) as caught:
                backend.verify_cached("topology", directory, repinned)
            self.assertEqual(caught.exception.rc, 4)
            self.assertIn(caught.exception.reason, ("quad_cage_toolchain_stale",))
        finally:
            topology_receipt.write_bytes(receipt_bytes)
        cached = pipeline.run()["stages"][-1]
        self.assertEqual((cached["rc"], cached["cache_hit"], cached["receipt"]), (0, True, row["receipt"]))
        print(f"GEO authority_wall_s={authority_wall:.3f} stage_wall_s={stage_wall:.3f} "
              f"native={proof['native_digest']} "
              f"stage={pins['stage_digest']} profile={pins['profile_digest']}", flush=True)
        cage_bytes = staged_cage.read_bytes()
        staged_cage.write_bytes(cage_bytes + b"\n")
        drift = pipeline.run()["stages"][-1]
        self.assertEqual((drift["rc"], drift["cache_hit"]), (4, False))
        self.assertEqual(engine.load(drift["receipt"]["path"])["reason_codes"], ["bytes_stale"])
        staged_cage.write_bytes(cage_bytes)
        # A triangle-only replay with no declared cage evidence cannot reach promotion.
        missing = copy.deepcopy(record)
        missing["evidence"].remove(engine.file_ref(staged_cage))
        staged_cage.unlink()
        with self.assertRaises(Refusal) as caught:
            backend.verify_cached("topology", directory, missing)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "required_quad_cage_missing"))
        staged_cage.write_bytes(cage_bytes)
        authority_cage.write_bytes(cage_bytes + b"\n")
        with self.assertRaises(Refusal) as caught:
            backend.verify_sources()
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "bytes_stale"))
        authority_cage.write_bytes(cage_bytes)
        registration = engine.load(authority / "geo-registration" / pins["registration_digests"][-1][7:])
        del registration["quad_cage"]
        new_registration = core.sha256_json(registration)
        core.atomic_write_json(authority / "geo-registration" / new_registration, registration)
        no_cage = dict(pins, registration_digests=[pins["registration_digests"][0], "sha256:" + new_registration])
        no_cage_policy = self.base / "no-cage-policy.json"
        core.atomic_write_json(no_cage_policy, dict(mesh_policy, geometry_scope={
            "authority": str(authority), "topology": no_cage, "quadriflow_solver": solver}))
        no_cage_backend = stages.NativeStages(source=self.raw, reference=reference, provider="byo",
                                               options=dict(opts, mesh_policy=no_cage_policy))
        no_cage_backend.bind_output(pipeline.root)
        with self.assertRaises(Refusal) as caught:
            no_cage_backend.identity("topology", self.raw)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "required_quad_cage_missing"))
        stale = copy.deepcopy(proof)
        stale["levels"][-1]["quad_cage"]["cage_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(self.raw, self.candidate, self.receipt,
                                             target_faces=1200, geometry_proof=stale)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "cage_export_drift"))
        rotated_solver = dict(solver, quadriflow_target_quads=601)
        core.atomic_write_json(opts["mesh_policy"], dict(mesh_policy, geometry_scope={
            "authority": str(authority), "topology": pins, "quadriflow_solver": rotated_solver}))
        rotated = stages.NativeStages(source=self.raw, reference=reference, provider="byo", options=opts)
        rotated_pipeline = engine.Pipeline(pipeline.root, rotated)
        rotated_pipeline.stages = pipeline.stages
        with patch.object(topology, "default_runner") as launch:
            miss = rotated_pipeline.run()["stages"][-1]
        miss_record = engine.load(miss["receipt"]["path"])
        self.assertEqual((miss["stage"], miss["rc"], miss["cache_hit"]), ("topology", 2, False))
        self.assertEqual(miss_record["reason_codes"], ["quadriflow_solver_target_invalid"])
        self.assertNotEqual(miss_record["key"], record["key"])
        launch.assert_not_called()

    def test_digest_mismatch(self):
        output = self.base / "changed-cage"
        shutil.copytree(self.output, output)
        candidate = output / self.candidate.name
        cage = candidate.with_suffix(".cage.json")
        cage.write_bytes(cage.read_bytes() + b"\n")
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(self.raw, candidate, self.receipt, target_faces=1200, geometry_proof=None)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "quad_cage_digest_stale"))

    def test_other_raw_input(self):
        alternate = self.base / "other.glb"
        alternate.write_bytes(self.candidate.read_bytes())
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(alternate, self.candidate, self.receipt,
                                             target_faces=1200, geometry_proof=None)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (4, "quad_cage_raw_stale"))

    def test_changed_bindings(self):
        for key, changed, reason in (
            ("candidate_sha256", "0" * 64, "quad_cage_candidate_stale"),
            ("toolchain_sha256", "0" * 64, "quad_cage_toolchain_stale"),
            ("requested_faces", 1198, "quad_cage_face_counts_stale"),
            ("achieved_faces", 1080, "quad_cage_face_counts_stale"),
        ):
            with self.subTest(key=key):
                receipt = copy.deepcopy(self.receipt)
                record = receipt if key.endswith("sha256") else receipt["checks"][0]
                record[key] = changed
                with self.assertRaises(Refusal) as caught:
                    topology_bridge.verify_quad_cage(self.raw, self.candidate, receipt,
                                                     target_faces=1200, geometry_proof=None)
                self.assertEqual((caught.exception.rc, caught.exception.reason), (4, reason))

    def test_removed_cage_with_old_pin(self):
        output = self.base / "removed-cage"
        shutil.copytree(self.output, output)
        candidate = output / self.candidate.name
        candidate.with_suffix(".cage.json").unlink()
        with self.assertRaises(Refusal) as caught:
            topology_bridge.verify_quad_cage(self.raw, candidate, self.receipt, target_faces=1200, geometry_proof=None)
        self.assertEqual((caught.exception.rc, caught.exception.reason), (3, "required_quad_cage_missing"))
