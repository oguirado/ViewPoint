"""Provider admission and matte intake controls through the native pipeline."""

from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch

from asset_factory import core
from tools.pipeline import engine, run_asset, stages
from tools.qualification import qualify
from tools.qualification.harness import RECEIPT_SCHEMA
import test_d18_qual_harness as qual_fixture
import test_n18_wire_a1 as a1
import test_n18_sat_replay as replay
from test_n18_sat_replay import sha, stage_record, write


class ProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        intake = a1.IntakeMatteTests()
        intake.setUp()
        self.addCleanup(intake.doCleanups)
        self.intake = intake
        self.root = intake.root
        evidence = os.environ.get("WIRE_A2_EVIDENCE")
        self.evidence = Path(evidence) / self._testMethodName if evidence else self.root
        if evidence:
            self.root = intake.root = self.evidence / "work"
            self.root.mkdir(parents=True)
            intake.original = self.root / intake.original.name
            intake.original.write_bytes(intake.fixture.original)
            intake.derivative = self.root / intake.derivative.name
            intake.matte = self.root / intake.matte.name
            intake.out = self.root / intake.out.name
        _, _, (submitted, matte) = intake.prepare()
        self.assertEqual(matte["source_rc"], 0, matte)
        intake.derivative.write_bytes(submitted)
        write(intake.matte, matte)

    def admission(self, receipt_path, *, scope, candidate=None, derivative=None, name="admission.json"):
        receipt_ref = engine.file_ref(receipt_path)
        receipt = engine.load(receipt_path, expected_sha256=receipt_ref["sha256"])
        path = write(self.evidence / name, {
            "schema": "af.pipeline-qualification/v1",
            "scope": scope,
            "receipt": receipt_ref,
            "candidate_sha256": candidate or receipt["candidate_sha256"],
            "input_derivative_sha256": derivative or sha(self.intake.derivative),
        })
        return path, sha(path)

    def fixture_receipt(self):
        return write(self.evidence / "qualification.json", {
            "schema": RECEIPT_SCHEMA, "scope": "fixture", "provider": "trellis2",
            "candidate_sha256": core.sha256_json({"fixture": "wire-a2"}),
            "rc": 0, "status": "OK", "verdict": "PASS", "runs": [], "reason_codes": [],
        })

    def run_pipeline(self, flags=(), *, service=None, backend=stages.NativeStages):
        scheduler = Mock()
        intake = self.intake
        with ExitStack() as stack:
            if service is not None or backend is not stages.NativeStages:
                stack.enter_context(patch.object(run_asset, "NativeStages", side_effect=lambda **kwargs: backend(
                    **kwargs, service=service, scheduler_backend=scheduler)))
            launch = stack.enter_context(patch("tools.qualification.harness.run_offline",
                                               side_effect=AssertionError("provider admission launched QUAL")))
            dispatch = stack.enter_context(patch.object(stages.adapter, "generate",
                                                        side_effect=AssertionError("provider dispatched")))
            result, logs = replay.Replay().invoke_pipeline(
                "image", intake.derivative, intake.original, intake.out,
                "--matte-receipt", intake.matte, *flags,
            )
        launch.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(scheduler.mock_calls, [])
        if service is not None:
            self.assertEqual(service.mock_calls, [])
        self.assertIsNone(result.get("artifact"))
        if self.evidence != self.root:
            write(self.evidence / "pipeline.json", result)
            source_log = intake.out.parent / (intake.out.name + ".jsonl")
            (self.evidence / "pipeline.jsonl").write_bytes(source_log.read_bytes())
            for row in result.get("stages", []):
                if row["stage"] in ("input", "provider"):
                    path = Path(row["receipt"]["path"])
                    (self.evidence / (row["stage"] + "-stage.json")).write_bytes(path.read_bytes())
                    if row["stage"] == "provider":
                        stage = engine.load(path)
                        for ref in stage["evidence"]:
                            (self.evidence / "provider-diagnostic.json").write_bytes(Path(ref["path"]).read_bytes())
        return result, logs

    def assert_stage(self, result, name, rc, reason):
        self.assertEqual(result.get("schema"), "af.pipeline-run/v1", result)
        row = stage_record(result, name)
        stage = engine.load(row["receipt"]["path"], expected_sha256=row["receipt"]["sha256"])
        self.assertEqual((row["rc"], stage["reason_codes"], row["cache_hit"]), (rc, [reason], False), stage)
        return stage

    def test_rights_refusal(self):
        draft = os.environ.get("SAT_TRELLIS_CANDIDATE")
        if draft:
            candidate = engine.load(draft)
        else:
            fixture = qual_fixture.QualificationTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            candidate = fixture.manifest
            candidate["provider"] = "trellis2"
            fixture.bind_rights("CANNOT_SEE")
        with patch("tools.qualification.harness.run_offline",
                   side_effect=AssertionError("rights-refused candidate launched")) as launch:
            rejected = qualify(candidate, self.evidence / "qual", timeout_s=15)
        launch.assert_not_called()
        self.assertEqual((rejected["rc"], rejected["runs"], rejected["reason_codes"]),
                         (5, [], ["commercial_eu_rights_unproven"]))
        pin = self.admission(self.evidence / "qual/qualification.json", scope="production")
        result, _ = self.run_pipeline(("--qualification", *pin))
        stage = self.assert_stage(result, "provider", 5, "commercial_eu_rights_unproven")
        self.assertEqual(stage_record(result, "input")["rc"], 0)
        self.assertEqual(result["rc"], 5)
        self.assertEqual(len(stage["evidence"]), 1)
        ref = stage["evidence"][0]
        self.assertEqual(engine.load(ref["path"], expected_sha256=ref["sha256"]), rejected)

    def test_missing_receipt_stops_before_an_available_service(self):
        result, _ = self.run_pipeline(service=Mock())
        self.assert_stage(result, "provider", 3, "qualification_absent")

    def test_fixture_reaches_service_boundary(self):
        pin = self.admission(self.fixture_receipt(), scope="fixture")
        result, _ = self.run_pipeline(("--qualification", *pin))
        self.assert_stage(result, "provider", 3, "trellis2_service_not_built_or_transport_absent")

    def test_fixture_blocks_an_available_production_service(self):
        pin = self.admission(self.fixture_receipt(), scope="fixture")
        result, _ = self.run_pipeline(("--qualification", *pin), service=Mock())
        self.assert_stage(result, "provider", 5, "qualification_fixture_scope")

    def test_relabelling_fixture_is_refused(self):
        pin = self.admission(self.fixture_receipt(), scope="production")
        result, _ = self.run_pipeline(("--qualification", *pin), service=Mock())
        self.assert_stage(result, "provider", 5, "qualification_fixture_scope")

    def test_other_derivative_is_stale(self):
        other = core.sha256_json({"fixture": "different derivative"})
        pin = self.admission(self.fixture_receipt(), scope="fixture", derivative=other)
        result, _ = self.run_pipeline(("--qualification", *pin), service=Mock())
        self.assert_stage(result, "provider", 4, "qualification_derivative_stale")

    def test_other_candidate_is_stale(self):
        other = core.sha256_json({"fixture": "different candidate"})
        pin = self.admission(self.fixture_receipt(), scope="fixture", candidate=other)
        result, _ = self.run_pipeline(("--qualification", *pin), service=Mock())
        self.assert_stage(result, "provider", 4, "qualification_candidate_stale")

    def test_tampered_receipt_bytes_are_stale(self):
        receipt = self.fixture_receipt()
        pin = self.admission(receipt, scope="fixture")
        receipt.write_bytes(receipt.read_bytes() + b"\n")
        result, _ = self.run_pipeline(("--qualification", *pin), service=Mock())
        self.assert_stage(result, "provider", 4, "bytes_stale")

    def cached_provider_success(self, flags, *, backend=stages.NativeStages):
        def seed(_backend, directory, _subject):
            (directory / "raw.glb").write_bytes(b"synthetic cache subject")
            write(directory / "synthetic.json", {"scope": "cache fixture"})
            return stages.success("raw.glb", ["synthetic.json"])

        with patch.object(stages.NativeStages, "stage_provider", autospec=True, side_effect=seed) as writer:
            result, _ = self.run_pipeline(flags, backend=backend)
        writer.assert_called_once()
        row = stage_record(result, "provider")
        self.assertEqual((row["rc"], row["cache_hit"]), (0, False), row)
        return engine.load(row["receipt"]["path"], expected_sha256=row["receipt"]["sha256"])

    def test_changed_admission_misses_a_successful_provider_cache(self):
        receipt = self.fixture_receipt()
        pin = self.admission(receipt, scope="fixture")
        flags = ("--qualification", *pin)
        cached = self.cached_provider_success(flags)
        unchanged, _ = self.run_pipeline(flags)
        self.assertTrue(stage_record(unchanged, "provider")["cache_hit"])

        other = engine.load(receipt)
        other["candidate_sha256"] = core.sha256_json({"fixture": "another candidate"})
        other_receipt = write(self.evidence / "other-qualification.json", other)
        other_pin = self.admission(other_receipt, scope="fixture", name="other-admission.json")
        changed, _ = self.run_pipeline(("--qualification", *other_pin), service=Mock())
        self.assert_stage(changed, "provider", 5, "qualification_fixture_scope")
        for path in (pin[0], receipt):
            self.assertIn(engine.file_ref(path), cached["inputs"])

    def test_admission_and_receipt_are_rechecked_after_run(self):
        backends = []

        def record(**kwargs):
            backend = stages.NativeStages(**kwargs)
            backends.append(backend)
            return backend

        receipt = self.fixture_receipt()
        pin = self.admission(receipt, scope="fixture")
        self.cached_provider_success(("--qualification", *pin), backend=record)
        self.assertEqual(len(backends), 1)
        for path in (pin[0], receipt):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    with self.assertRaises(engine.Refusal) as raised:
                        backends[0].verify_sources()
                    self.assertEqual((raised.exception.rc, raised.exception.reason), (4, "bytes_stale"))
                finally:
                    path.write_bytes(original)

    def test_qualified_identity_matches_dispatched_revision_and_weights(self):
        provider = engine.load(stages.ROOT / "providers/trellis2/provider.json")
        provider["weights"] = [dict(provider["weights"][0], files=provider["weights"][0]["files"][:1])]
        qualified_manifest = write(self.root / "qualified-provider.json", provider)
        dispatched_manifest = write(self.root / "dispatched-provider.json", provider)
        factory = write(self.root / "factory.json", {
            "schema": "af.factory-config/v1", "jurisdiction": "ES", "commercial_use": False,
            "models_path": "synthetic", "memory": {"max_resident_models": 1,
                "ceiling_gb_default": 8, "min_free_gb_before_start": 2},
            "slots": {s: {"provider": "trellis2" if s == "generate" else "none"} for s in core.SLOTS},
            "gates": {"appearance": {"floor": 4, "target": 7}, "vqa3d_profile": "synthetic"},
        })
        weight = provider["weights"][0]["files"][0]
        candidate = {
            "provider": provider["id"],
            "code": {"revision": "a" * 40, "files": [engine.file_ref(qualified_manifest)]},
            "weights": [dict(weight, path=str(self.root / "weights" / weight["path"]))],
            "conditioners": [],
        }
        output = self.evidence / "output"
        output.mkdir()
        measurement = write(output / "result.json", {
            "input_sha256": sha(self.intake.derivative), "seed": 42,
            "pipeline_type": "512", "preprocess_image": False,
        })
        verified = {"gpu_policy": {}, "runs": [{"outputs": {"result.json": engine.file_ref(measurement)}}]}
        receipt = self.fixture_receipt()
        receipt_record = engine.load(receipt)
        receipt_record.pop("scope")

        def attempt():
            write(self.evidence / "candidate.json", candidate)
            receipt_record["candidate_sha256"] = core.sha256_json(candidate)
            write(receipt, receipt_record)
            pin = self.admission(receipt, scope="production")
            return self.run_pipeline((
                "--qualification", *pin, "--provider-manifest", dispatched_manifest,
                "--factory-config", factory, "--territory-policy",
                stages.ROOT / "config/territory-groups.v1.json",
            ), service=Mock())[0]

        with patch("tools.qualification.verify_receipt", return_value=verified), patch(
            "tools.scheduler.scheduler.build_request", side_effect=engine.Refusal(3, "scheduler_tripwire"),
        ) as build:
            self.assert_stage(attempt(), "provider", 3, "scheduler_tripwire")
            build.assert_called_once()
            build.reset_mock()
            with self.subTest("dispatch revision changed"):
                other_revision = "b" * 40
                self.assertNotEqual(other_revision, provider["revision"])
                write(dispatched_manifest, dict(provider, revision=other_revision))
                self.assert_stage(attempt(), "provider", 4, "qualification_provider_stale")
                build.assert_not_called()

            build.reset_mock()
            with self.subTest("candidate weight digest changed"):
                write(dispatched_manifest, provider)
                candidate["weights"][0]["sha256"] = "c" * 64
                self.assert_stage(attempt(), "provider", 4, "qualification_weights_stale")
                build.assert_not_called()

    def test_input_witness_rejects_invalid_role_before_read(self):
        def inspect(*args, **kwargs):
            kwargs["read_checked"]("witness1", "sha256:" + sha(self.intake.matte))
            self.fail("invalid witness role got past the reader")

        with patch("gates.input_d18.inspect_matte", side_effect=inspect):
            result, _ = self.run_pipeline()
        self.assert_stage(result, "input", 2, "matte_witness_pin_invalid")

    def test_input_toolchain_drift_refuses_cache_hit(self):
        source, scratch = stages.ROOT, self.root / "scratch-product"
        for directory in ("asset_factory", "gates", "slots", "tools/input", "tools/pipeline"):
            shutil.copytree(source / directory, scratch / directory, ignore=shutil.ignore_patterns("__pycache__"))
        module = scratch / "tools/input/evidence.py"

        class ScratchStages(stages.NativeStages):
            edit = False

            def _pin(self, name):
                if name != "input":
                    return super()._pin(name)
                with ExitStack() as stack:
                    stack.enter_context(patch.object(stages, "ROOT", scratch))
                    for item in (stages, stages.core, stages.contracts, stages.input_gate):
                        path = scratch / Path(item.__file__).relative_to(source)
                        stack.enter_context(patch.object(item, "__file__", str(path)))
                    return super()._pin(name)

            def verify_cached(self, name, directory, record):
                super().verify_cached(name, directory, record)
                if name == "input" and self.edit:
                    with module.open("ab") as stream:
                        stream.write(b"\n# changed in scratch\n")
                    self.verify_sources()

        self.run_pipeline(backend=ScratchStages)
        stable, _ = self.run_pipeline(backend=ScratchStages)
        self.assertTrue(stage_record(stable, "input")["cache_hit"])
        ScratchStages.edit = True
        drifted, _ = self.run_pipeline(backend=ScratchStages)
        self.assert_stage(drifted, "input", 4, "upstream_toolchain_stale")
