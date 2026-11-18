"""Pipeline-boundary adversarial controls; synthetic review, real mesh metrics."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from licence.check import check_provider
from tools.mesh import api as mesh
from tools.pipeline import engine, stages
import test_e2e_service as service_fixtures
import test_f1u5_appearance as appearance_fixtures
from test_f2u1_mesh import glb, policy
from test_n18_int_geometry import scope_for_fixture


class AdmissionRepair(unittest.TestCase):
    def setUp(self):
        self.service = service_fixtures.ServiceBoundary()
        self.service.setUp()
        self.addCleanup(self.service.doCleanups)
        self.appearance = appearance_fixtures.AppearanceTests()
        self.appearance.setUp()
        self.addCleanup(self.appearance.doCleanups)
        self.root = self.appearance.root
        self.factory = engine.load(self.service.opts["factory_config"])
        self.provider = copy.deepcopy(self.service.provider)
        self.territory = engine.load(self.service.opts["territory_policy"])
        self.source = self.root / "source.glb"
        self.source.write_bytes(glb())
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-reference")

    def prepare(self, *, weak=False, advisory=None, context_factory=None):
        a = self.appearance
        a.request["asset"] = a.file("synthetic.asset", self.source.read_bytes())
        a.request["reference"] = a.file("synthetic.reference", self.reference.read_bytes())
        a.upstream["source_sha256"] = core.sha256_file(self.source)
        a.manifest.update(asset_sha256=a.request["asset"]["sha256"],
                          reference_sha256=a.request["reference"]["sha256"])
        config = context_factory or self.factory
        a.licence = check_provider(config, self.provider, operation="generate", policy=self.territory)
        if advisory:
            a.licence.update(advisory)
        a.request["provider"] = {"id": self.provider["id"], "revision": self.provider["revision"]}
        a.request["licence_decision"] = a.licence
        a.request["licence_context"] = {"configuration": config, "provider_manifest": self.provider,
                                        "territory_policy": self.territory, "operation": "generate"}
        if weak:
            a.policy.update(floor=0, target=0, likeness_floor=0, likeness_target=0)
        a.rebind()
        if weak:
            a.review["scores"] = {key: None if key == "face_appeal" else 0 for key in a.review["scores"]}
            a.review["likeness_to_reference"] = 0
        opts = {"timeout_s": 20}
        for key, value in (("appearance_request", a.request), ("appearance_review", a.review),
                           ("factory_config", self.factory), ("provider_manifest", self.provider),
                           ("territory_policy", self.territory)):
            path = self.root / (key + ".json")
            core.atomic_write_json(path, value)
            opts[key] = path
        return stages.NativeStages(source=self.source, reference=self.reference, provider="byo", options=opts)

    def invoke(self, backend):
        directory = self.root / "attempt"
        directory.mkdir(exist_ok=True)
        with mock.patch.object(backend, "_bridge", return_value=self.appearance.upstream):
            return backend.stage_appearance(directory, self.source)

    def test_current_allowed_decision_and_frozen_policy_pass(self):
        self.assertEqual(self.invoke(self.prepare())["rc"], 0)

    def test_f1_noncommercial_declaration_and_legacy_negative_are_informational(self):
        self.factory["commercial_use"] = True
        self.provider["commercial"] = False
        self.assertEqual(self.invoke(self.prepare())["rc"], 0)
        decision = engine.load(self.root / "attempt/licence.json")
        self.assertFalse(decision["monetisable"])
        self.assertEqual(decision["rc"], 0)
        legacy = {"rc": 5, "verdict": "FAIL", "allowed": False, "monetisable": False,
                  "reason_codes": ["commercial_provider_required"]}
        self.assertEqual(self.invoke(self.prepare(advisory=legacy))["rc"], 0)

    def test_f1_advisory_record_own_digest_is_checked_at_admission(self):
        backend = self.prepare()
        request = copy.deepcopy(self.appearance.request)
        request["licence_decision"]["reason_codes"] = ["changed_advisory"]
        with self.assertRaisesRegex(engine.Refusal, "^licence_decision_stale$") as error:
            backend._appearance_admission(request)
        self.assertEqual(error.exception.rc, 4)

    def test_f1_resealed_advisory_identity_drift_refuses_at_admission(self):
        backend = self.prepare()
        for field, changed in (("provider_sha256", "0" * 64), ("configuration_sha256", "0" * 64),
                               ("policy_sha256", "0" * 64), ("provider_id", "different-provider"),
                               ("operation", "refine")):
            with self.subTest(field=field):
                request = copy.deepcopy(self.appearance.request)
                request["licence_decision"][field] = changed
                request["licence_decision_sha256"] = core.sha256_json(request["licence_decision"])
                with self.assertRaisesRegex(engine.Refusal, "^licence_decision_stale$") as error:
                    backend._appearance_admission(request)
                self.assertEqual(error.exception.rc, 4)

    def test_f1_advisory_shape_is_checked_at_admission(self):
        backend = self.prepare()
        request = copy.deepcopy(self.appearance.request)
        request["licence_decision"] = []
        request["licence_decision_sha256"] = core.sha256_json([])
        with self.assertRaisesRegex(engine.Refusal, "^licence_decision_invalid$") as error:
            backend._appearance_admission(request)
        self.assertEqual(error.exception.rc, 2)

    def test_generation_without_declarations_still_binds_qualified_provider(self):
        for key in ("jurisdiction", "commercial_use"):
            del self.factory[key]
        core.atomic_write_json(self.service.opts["factory_config"], self.factory)
        service, lifecycle = service_fixtures.FakeService(), service_fixtures.Lifecycle()
        service.provider = self.service.provider
        directory = self.service.root / "success"
        directory.mkdir()
        backend = self.service.backend(service, lifecycle)
        with mock.patch.object(stages.contracts, "validate_slot", wraps=stages.contracts.validate_slot) as validate:
            self.assertEqual(backend.stage_provider(directory, None)["rc"], 0)
        request, = [c.args[0] for c in validate.call_args_list if c.args[1:] == ("generate", "request")]
        self.assertEqual(set(request["context"]), {"policy_sha256", "toolchain_sha256", "timeout_s"})
        self.assertTrue(lifecycle.stopped)

        other = {**self.service.provider, "revision": "f" * 40}
        core.atomic_write_json(self.service.opts["provider_manifest"], other)
        backend = self.service.backend(service, service_fixtures.Lifecycle())
        with self.assertRaisesRegex(engine.Refusal, "^qualification_provider_stale$") as error:
            backend.stage_provider(directory, None)
        self.assertEqual(error.exception.rc, 4)
        self.assertEqual(service.calls, 1)

    def test_f1_embedded_inputs_must_match_current_files(self):
        embedded = copy.deepcopy(self.factory)
        self.factory["models_path"] = "changed-current-configuration"
        backend = self.prepare(context_factory=embedded)
        with self.assertRaises(engine.Refusal) as error:
            self.invoke(backend)
        self.assertEqual(error.exception.rc, 4)

    def test_f1_current_inputs_are_required_even_with_embedded_context(self):
        backend = self.prepare()
        del backend.policies["factory_config"]
        with self.assertRaises(engine.Refusal) as error:
            self.invoke(backend)
        self.assertEqual(error.exception.rc, 3)

    def test_f1_current_licence_implementation_pins_appearance_cache(self):
        backend = self.prepare()
        before = backend._pin("appearance")
        original = core.sha256_file
        def changed(path):
            return "e" * 64 if Path(path) == stages.ROOT / "licence/check.py" else original(path)
        with mock.patch.object(core, "sha256_file", changed):
            self.assertNotEqual(backend._pin("appearance"), before)

    def test_f1_cached_appearance_rechecks_technical_provider_availability(self):
        prepared = self.prepare()
        mesh_policy = self.root / "mesh-policy.json"
        raw = self.source.read_bytes()
        scope = scope_for_fixture(self.root / "authority", raw,
                                  dict.fromkeys(("mesh", "topology", "uv_bake_orm", "export"), raw))
        core.atomic_write_json(mesh_policy, policy(sample_budget=512, raster_resolution=96, geometry_scope=scope))
        class IdentityGeometry(stages.NativeStages):
            scope = "synthetic-geometry-real-admission-cache-test"
            def unchanged(self, directory, subject):
                (directory / "asset.glb").write_bytes(subject.read_bytes())
                core.atomic_write_json(directory / "identity.json", {"synthetic_geometry_only": True})
                return stages.success("asset.glb", ["identity.json"])
            stage_mesh = stage_topology = stage_uv_bake_orm = stage_export = unchanged

            def _verify_bake_sources(self, directory, high_sha256, low_sha256):
                # Synthetic geometry: no bake snapshots exist to replay; this test
                # targets provider availability on a cache hit (WIRE-A6).
                if not (directory.parent / "identity.json").is_file():
                    super()._verify_bake_sources(directory, high_sha256, low_sha256)
        backend = IdentityGeometry(source=self.source, reference=self.reference, provider="byo",
                                   options={**prepared.options, "mesh_policy": mesh_policy})
        pipeline = engine.Pipeline(self.root / "pipeline", backend)
        with mock.patch.object(backend, "_bridge", return_value=self.appearance.upstream):
            backend.stage_vqa3d = backend.unchanged
            first = pipeline.run()
            self.assertEqual(first["rc"], 0, first)
            cached = pipeline.run()
            self.assertEqual(cached["rc"], 0, cached)
            self.assertTrue(all(row["cache_hit"] for row in cached["stages"]))
            self.assertEqual(backend.raw_ref, engine.file_ref(
                engine.load(cached["stages"][1]["receipt"]["path"])["subject"]))
            blocked = {**self.provider, "runnable": False, "blocked_reason": "research_only_unverified_runtime"}
            refused = check_provider(self.factory, blocked, operation="generate", policy=self.territory)
            self.assertEqual(refused["reason_codes"], ["provider_blocked"])
            with mock.patch("licence.check.check_provider", return_value=refused):
                result = pipeline.run()
            self.assertEqual(result["stages"][6]["rc"], 5, result)
            self.assertTrue(all(row["cache_hit"] for row in result["stages"][:6]))
            self.assertIsNone(result["artifact"])

    def test_f2_resealed_zero_scores_and_thresholds_cannot_pass(self):
        backend = self.prepare(weak=True)
        with self.assertRaises(engine.Refusal) as error:
            self.invoke(backend)
        self.assertEqual(error.exception.rc, 2)
        self.assertFalse((self.root / "attempt/appearance.json").exists())

    def test_f2_each_quality_and_likeness_bound_cannot_be_weakened(self):
        for name in ("floor", "target", "likeness_floor", "likeness_target"):
            with self.subTest(name=name):
                self.appearance.policy.update(floor=4, target=7, likeness_floor=4, likeness_target=7)
                self.appearance.policy[name] -= 1
                backend = self.prepare()
                with self.assertRaises(engine.Refusal) as error:
                    self.invoke(backend)
                self.assertEqual(error.exception.rc, 2)

    def test_f2_stricter_request_keeps_the_configured_minimum(self):
        self.appearance.policy.update(floor=5, target=8, likeness_floor=5, likeness_target=8)
        self.assertEqual(self.invoke(self.prepare())["rc"], 0)


class MeshRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw.glb"
        self.raw.write_bytes(glb())
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-reference")
        self.policy = policy(sample_budget=512, raster_resolution=96,
                             repair={"max_passes": 1, "max_hole_edges": 0, "cull_max_permille": 0})
        self.policy_file = self.root / "policy.json"
        core.atomic_write_json(self.policy_file, self.policy)
        self.backend = stages.NativeStages(source=self.raw, reference=self.reference, provider="byo",
            options={"mesh_policy": self.policy_file, "timeout_s": 20})
        self.out = self.root / "out"
        self.out.mkdir()

    def native_repair(self):
        rc, receipt, data = mesh.repair_mesh(self.raw, policy=self.policy)
        self.assertEqual(rc, 0, receipt)
        return receipt, data

    def test_f3_truncated_native_repair_receipt_is_an_error(self):
        with mock.patch.object(stages.mesh, "repair_mesh", return_value=(0, {"rc": 0}, self.raw.read_bytes())), \
                mock.patch.object(stages.mesh, "compare_meshes", return_value=(0, {"rc": 0})):
            with self.assertRaises(engine.Refusal) as error:
                self.backend.stage_mesh(self.out, self.raw)
        self.assertEqual(error.exception.rc, 2)

    def test_f3_truncated_comparison_receipt_is_an_error(self):
        receipt, data = self.native_repair()
        with mock.patch.object(stages.mesh, "repair_mesh", return_value=(0, receipt, data)), \
                mock.patch.object(stages.mesh, "compare_meshes", return_value=(0, {"rc": 0})):
            with self.assertRaises(engine.Refusal) as error:
                self.backend.stage_mesh(self.out, self.raw)
        self.assertEqual(error.exception.rc, 2)

    def test_f3_wrong_source_policy_toolchain_and_artifact_are_stale(self):
        native, data = self.native_repair()
        for key in ("input_sha256", "policy_sha256", "toolchain_sha256", "candidate_sha256", "artefact_sha256"):
            with self.subTest(key=key):
                bad = copy.deepcopy(native)
                bad[key] = "0" * 64
                if key == "candidate_sha256":
                    bad["artefact_sha256"] = "0" * 64
                if key == "artefact_sha256":
                    bad["candidate_sha256"] = "0" * 64
                with mock.patch.object(stages.mesh, "repair_mesh", return_value=(0, bad, data)):
                    with self.assertRaises(engine.Refusal) as error:
                        self.backend.stage_mesh(self.out, self.raw)
                self.assertEqual(error.exception.rc, 4)

    def test_f3_schema_valid_success_without_coverage_is_an_error(self):
        native, data = self.native_repair()
        native["metrics"]["component_inventories"] = []
        with mock.patch.object(stages.mesh, "repair_mesh", return_value=(0, native, data)):
            with self.assertRaises(engine.Refusal) as error:
                self.backend.stage_mesh(self.out, self.raw)
        self.assertEqual(error.exception.rc, 2)

    def test_f3_real_repair_and_comparison_stay_green(self):
        self.assertEqual(self.backend.stage_mesh(self.out, self.raw)["rc"], 0)

    def test_f3_native_repair_refusal_keeps_its_code_and_evidence(self):
        from tools.mesh import contracts as mesh_contracts
        for rc in (2, 3, 4, 5, 6):
            with self.subTest(rc=rc):
                rec = mesh_contracts.receipt(raw_hash=core.sha256_file(self.raw),
                    policy_hash=core.sha256_json(self.policy), tool_hash=mesh.toolchain_identity())
                _, rec = mesh_contracts.finish(rec, rc, "planted_native_refusal")
                rec["candidate_sha256"] = "a" * 64
                with mock.patch.object(stages.mesh, "repair_mesh", return_value=(rc, rec, None)):
                    with self.assertRaises(engine.Refusal) as error:
                        self.backend.stage_mesh(self.out, self.raw)
                self.assertEqual(error.exception.rc, rc)
                self.assertEqual(error.exception.evidence_record, rec)

    def test_f4_locally_passing_composition_is_refused_against_retained_raw(self):
        first = self.root / "first.glb"
        final = self.root / "final.glb"
        first.write_bytes(glb([{"name": "body", "scale": [0.985] * 3}]))
        final.write_bytes(glb([{"name": "body", "scale": [0.985 ** 2] * 3}]))
        for left, right, expected in ((self.raw, first, 0), (first, final, 0), (self.raw, final, 5)):
            rc, record = mesh.compare_meshes(left, right, policy=self.policy)
            self.assertEqual(rc, expected, record)
        def local_step(directory, subject):
            rc, record = mesh.compare_meshes(subject, final, policy=self.policy)
            self.assertEqual(rc, 0, record)
            (directory / "candidate.glb").write_bytes(final.read_bytes())
            core.atomic_write_json(directory / "local.json", record)
            return stages.success("candidate.glb", ["local.json"])
        scope = scope_for_fixture(self.root / "authority", self.raw.read_bytes(),
                                  {"topology": self.raw.read_bytes()})
        core.atomic_write_json(self.policy_file, dict(self.policy, geometry_scope=scope))
        self.backend = stages.NativeStages(source=self.raw, reference=self.reference, provider="byo",
                                          options={"mesh_policy": self.policy_file, "timeout_s": 20})
        self.backend.bind_output(self.out)
        with mock.patch.object(self.backend, "stage_topology", local_step):
            with self.assertRaises(engine.Refusal) as error:
                self.backend.execute("topology", self.out, first)
        self.assertEqual(error.exception.rc, 5)
        self.assertEqual(error.exception.evidence_record["input_sha256"], core.sha256_file(self.raw))
        self.assertEqual(error.exception.evidence_record["candidate_sha256"], core.sha256_file(final))
        self.assertEqual(self.raw.read_bytes(), glb())


if __name__ == "__main__":
    unittest.main()
