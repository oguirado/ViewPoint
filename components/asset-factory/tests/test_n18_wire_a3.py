"""The native CLI's recorded TRELLIS transport and producer matte provenance."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from asset_factory import core
from gates import input_d18
from tools.blender import glb_io
from tools.input import matte as raster_matte
from tools.pipeline import engine, run_asset, stages
from tools.qualification.harness import RECEIPT_SCHEMA
from tools.qualification.manifest import Refusal as QualRefusal
import test_d18_input_contract as input_fixture
import test_f1u2_adapter as adapter_fixture
import test_n18_sat_replay as replay_fixture
from test_n18_sat_replay import sha, stage_record, write


class WireA3(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.out = self.root / "pipeline"
        fixture = input_fixture.Mattes()
        fixture.setUp()
        self.fixture = fixture
        fixture.intent["matte_source"] = "authored"
        self.original = self.root / "original.png"
        self.original.write_bytes(fixture.original)
        self.master = input_fixture.png("RGBA", fixture.size, [
            channel for index, alpha in enumerate(fixture.alpha)
            for channel in (*fixture.rgb[index * 3:index * 3 + 3], alpha)
        ])
        self.witnesses = {
            "intent_digest": fixture.store.add("matte-intent", fixture.intent),
            "read_checked": fixture.store.read,
        }
        self.mask = fixture.reference
        submitted, self.matte = raster_matte.prepare_matte(
            fixture.original, self.master, self.mask, **self.witnesses)
        self.assertEqual(self.matte["source_rc"], 0, self.matte)
        self.image = self.root / "submitted.png"
        self.image.write_bytes(submitted)
        self.matte_path = self.root / "matte.json"
        for role, pin in fixture.store:
            path = self.root / role / pin[7:]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(fixture.store.read(role, pin))
        for role, data in (("original-rgb", fixture.original), ("straight-alpha", self.master),
                           ("approved-mask", self.mask)):
            path = self.root / role / sha256(data).hexdigest()
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)

    def set_model_intent(self):
        intent = dict(self.fixture.intent, matte_source="birefnet", birefnet_candidate_sha256="b" * 64)
        pin = self.fixture.store.add("matte-intent", intent)
        self.witnesses["intent_digest"] = pin
        (self.root / "matte-intent" / pin[7:]).write_bytes(self.fixture.store.read("matte-intent", pin))

    def factory_fixture(self, factory=None):
        from test_n18_int_intent import approval_store

        pin = self.witnesses["intent_digest"]
        store = approval_store(self.root / "operator", self.fixture.intent, pin,
                               self.fixture.store.read("matte-intent", pin))
        config = factory or {"schema": "af.factory-config/v1"}
        return write(self.root / "factory.json", dict(config, matte_approval_store=store))

    def record_transport(self, *, candidate_for_previous_revision=False):
        provider_data = SimpleNamespace(addCleanup=self.addCleanup)
        adapter_fixture.AdapterTests.setUp(provider_data)
        provider = provider_data.provider
        manifest = write(self.root / "provider.json", provider)
        config = self.factory_fixture(provider_data.factory)
        self.factory_path = config
        policy = Path(stages.ROOT / "config/territory-groups.v1.json")
        prior = deepcopy(provider)
        prior["revision"] = "f" * 40
        candidate_manifest = (write(self.root / "prior-provider.json", prior)
                              if candidate_for_previous_revision else manifest)
        candidate = write(self.root / "candidate.json", {
            "provider": "trellis2", "code": {"files": [engine.file_ref(candidate_manifest)]},
            "weights": [dict(item, path=str(self.root / item["path"]))
                        for weight in provider["weights"] for item in weight["files"]],
            "conditioners": [],
        })
        qual = write(self.root / "qualification.json", {
            "schema": RECEIPT_SCHEMA, "scope": "fixture", "provider": "trellis2",
            "candidate_sha256": sha(candidate), "rc": 0, "status": "OK",
            "verdict": "PASS", "reason_codes": [],
        })
        derivative = sha(self.image)
        admission = write(self.root / "admission.json", {
            "schema": "af.pipeline-qualification/v1", "scope": "fixture",
            "receipt": engine.file_ref(qual), "candidate_sha256": sha(candidate),
            "input_derivative_sha256": derivative,
        })
        self.raw = self.root / "recorded.glb"
        self.raw.write_bytes(adapter_fixture.glb())
        self.record = {
            "schema": "af.trellis2-replay/v1", "scope": "fixture",
            "request_id": derivative, "provider_revision": provider["revision"],
            "provider_sha256": core.sha256_json(provider), "candidate_sha256": sha(candidate),
            "input_derivative_sha256": derivative, "params": {"seed": 42, "pipeline_type": "512"},
            "output": {"path": self.raw.name, "sha256": sha(self.raw)},
        }
        self.transport = self.root / "transport.json"
        self.flags = ("--provider-manifest", manifest, "--factory-config", config,
                      "--territory-policy", policy, "--qualification", admission, sha(admission))

    def run_pipeline(self, *, matte_source="authored", replay=False, repin=True):
        receipt = dict(self.matte)
        if matte_source is not None:
            receipt["matte_source"] = matte_source
        write(self.matte_path, receipt)
        flags = ("--matte-receipt", self.matte_path)
        if replay:
            if repin:
                write(self.transport, self.record)
                self.transport_pin = sha(self.transport)
            flags += (*self.flags, "--replay-transport", self.transport, self.transport_pin)
        if replay:
            config = self.factory_path
        else:
            config = self.factory_fixture()
            flags += ("--factory-config", config)
        with patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=sha(config)):
            result, _ = replay_fixture.Replay().invoke_pipeline(
                "image", self.image, self.original, self.out, *flags)
        return result

    def assert_refused(self, name, result, rc, reason):
        row = stage_record(result, name)
        receipt = engine.load(row["receipt"]["path"], expected_sha256=row["receipt"]["sha256"])
        self.assertEqual((row["rc"], receipt["reason_codes"]), (rc, [reason]), receipt)
        self.assertIsNone(result["artifact"])
        return receipt

    def test_scoped_replay_cannot_promote_even_when_both_stages_are_green(self):
        self.record_transport()
        with patch.dict(engine.STAGE_PLANS, prop=("input", "provider")):
            result = self.run_pipeline(replay=True)
            cached = self.run_pipeline(replay=True)
        self.assertEqual([row["rc"] for row in result["stages"]], [0, 0])
        self.assertEqual((result["scope"], result["rc"], result["verdict"], result["artifact"]),
                         ("provider-fixture-replay", 3, "CANNOT_SEE", None))
        provider = stage_record(result, "provider")
        stage = engine.load(provider["receipt"]["path"])
        self.assertEqual(Path(stage["subject"]).read_bytes(), self.raw.read_bytes())
        self.assertFalse(provider["cache_hit"])
        self.assertTrue(stage_record(cached, "provider")["cache_hit"])
        self.assertEqual(cached["verdict"], "CANNOT_SEE")
        replay_ref = next(ref for ref in stage["evidence"]
                          if Path(ref["path"]).name == "replay-transport.json")
        self.assertEqual(engine.load(replay_ref["path"]), self.record)

    def test_request_id_mismatch_through_adapter(self):
        self.record_transport()
        self.record["request_id"] = "other-request"
        receipt = self.assert_refused("provider", self.run_pipeline(replay=True), 4, "replay_request_stale")
        diagnostic = engine.load(receipt["evidence"][0]["path"])
        self.assertEqual(diagnostic["absence_reasons"]["outputs"], "replay_request_stale")

    def test_candidate_from_other_provider_revision_through_native_cli(self):
        self.record_transport(candidate_for_previous_revision=True)
        self.assert_refused("provider", self.run_pipeline(replay=True), 4, "qualification_provider_stale")

    def test_recorded_provider_revision_mismatch_through_adapter(self):
        self.record_transport()
        self.record["provider_revision"] = "f" * 40
        self.assert_refused("provider", self.run_pipeline(replay=True), 4, "replay_provider_stale")

    def test_candidate_and_derivative_replay_bindings(self):
        for field, reason in (("candidate_sha256", "replay_candidate_stale"),
                              ("input_derivative_sha256", "replay_derivative_stale")):
            with self.subTest(field=field):
                self.record_transport()
                self.record[field] = "d" * 64
                self.assert_refused("provider", self.run_pipeline(replay=True), 4, reason)

    def test_fixture_record_cannot_assert_production_verdict(self):
        self.record_transport()
        self.record["production_verdict"] = "PASS"
        result = self.run_pipeline(replay=True)
        self.assertEqual((result["rc"], result["reason_codes"]), (5, ["replay_fixture_scope"]))

    def test_replay_output_drift_refuses_existing_cache(self):
        self.record_transport()
        result = self.run_pipeline(replay=True)
        self.assertEqual(stage_record(result, "provider")["rc"], 0)
        self.raw.write_bytes(self.raw.read_bytes() + b"tampered")
        result = self.run_pipeline(replay=True, repin=False)
        self.assertEqual((result["rc"], result["reason_codes"]), (4, ["bytes_stale"]))

    def test_authored_producer_records_source_and_enters_pipeline(self):
        prepared, receipt = input_d18.prepare_matte(
            self.original.read_bytes(), self.master, self.mask, matte_source="authored", **self.witnesses)
        self.assertEqual(receipt.get("matte_source"), "authored")
        self.assertEqual(prepared, self.image.read_bytes())
        self.matte = receipt
        result = self.run_pipeline()
        self.assertEqual(stage_record(result, "input")["rc"], 0)

    def test_replay_parameters_must_match_requested_seed_and_pipeline(self):
        for key, changed in (("seed", 7), ("pipeline_type", "1024_cascade")):
            with self.subTest(key=key):
                self.record_transport()
                self.record["params"][key] = changed
                self.assert_refused("provider", self.run_pipeline(replay=True), 4, "replay_parameters_stale")

    def test_new_valid_recording_invalidates_provider_cache(self):
        self.record_transport()
        with patch.dict(engine.STAGE_PLANS, prop=("input", "provider")):
            first = stage_record(self.run_pipeline(replay=True), "provider")
            self.assertEqual(first["rc"], 0)
            self.assertEqual(sha(engine.load(first["receipt"]["path"])["subject"]), sha(self.raw))
            document, binary = glb_io.decode_glb(self.raw.read_bytes())
            document["asset"]["generator"] = "second-recording"
            other = self.root / "second.glb"
            other.write_bytes(glb_io.encode_glb(document, binary))
            self.assertNotEqual(sha(other), sha(self.raw))
            self.record["output"] = {"path": other.name, "sha256": sha(other)}
            second = stage_record(self.run_pipeline(replay=True), "provider")
        self.assertEqual(second["rc"], 0)
        self.assertFalse(second["cache_hit"])
        self.assertEqual(sha(engine.load(second["receipt"]["path"])["subject"]), sha(other))

    def test_fixture_qualification_refuses_live_service(self):
        self.record_transport()
        service = Mock()
        native = stages.NativeStages
        with patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=sha(self.factory_path)), patch.object(
                run_asset, "NativeStages",
                side_effect=lambda **kw: native(service=service, scheduler_backend=Mock(), **kw)):
            write(self.matte_path, dict(self.matte, matte_source="authored"))
            result, _ = replay_fixture.Replay().invoke_pipeline(
                "image", self.image, self.original, self.out, "--matte-receipt", self.matte_path, *self.flags)
        self.assert_refused("provider", result, 5, "qualification_fixture_scope")
        self.assertEqual(service.mock_calls, [])

    def test_model_intent_refuses_authored_receipt(self):
        self.set_model_intent()
        self.matte = raster_matte.inspect_matte(
            self.original.read_bytes(), self.master, self.image.read_bytes(), self.mask, **self.witnesses)
        self.assertEqual(self.matte["source_rc"], 0)
        self.assert_refused("input", self.run_pipeline(matte_source="authored"), 4, "matte_source_mismatch")

    def test_model_matte_requires_qualification(self):
        self.set_model_intent()
        self.matte = raster_matte.inspect_matte(
            self.original.read_bytes(), self.master, self.image.read_bytes(), self.mask, **self.witnesses)
        self.assertEqual(self.matte["source_rc"], 0)
        self.assert_refused("input", self.run_pipeline(matte_source="birefnet"),
                            5, "birefnet_qualification_required")

    def prepare_model_receipt(self):
        self.set_model_intent()
        proof = {"receipt_sha256": "a" * 64, "candidate_sha256": "b" * 64,
                 "evidence_dir": str(self.root / "birefnet")}
        output_bytes = {"original.png": self.original.read_bytes(), "straight.png": self.master}
        verified = {"provider": "birefnet", "candidate_sha256": proof["candidate_sha256"], "rc": 0,
                    "runs": [{}, {"outputs": {name: {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
                                              for name, raw in output_bytes.items()}}]}
        with patch.object(input_d18, "verify_receipt", return_value=verified) as verifier:
            _, self.matte = input_d18.prepare_matte(
                self.original.read_bytes(), self.master, self.mask, matte_source="birefnet",
                birefnet_qualification=proof, **self.witnesses)
            self.assertEqual(self.matte["source_rc"], 0, self.matte)
            verifier.assert_called_once()
        return verified

    def test_relabelled_model_receipt_refused_after_proof_removed(self):
        self.prepare_model_receipt()
        del self.matte["birefnet_qualification"], self.matte["birefnet_evidence_dir"]
        self.assert_refused("input", self.run_pipeline(matte_source="authored"), 4, "matte_source_mismatch")

    def test_model_matte_qualification_must_still_verify_on_cache_hit(self):
        verified = self.prepare_model_receipt()
        with patch.object(input_d18, "verify_receipt", return_value=verified) as verifier:
            result = self.run_pipeline(matte_source="birefnet")
            self.assertEqual(stage_record(result, "input")["rc"], 0)
            verifier.assert_called_once()
        with patch("tools.qualification.verify_receipt",
                   side_effect=QualRefusal(4, "receipt_output_mismatch")):
            self.assert_refused("input", self.run_pipeline(matte_source="birefnet"),
                                4, "receipt_output_mismatch")

    def test_omitted_matte_source_refused_at_both_entries(self):
        with self.assertRaises(TypeError):
            input_d18.prepare_matte(self.original.read_bytes(), self.master, self.mask, **self.witnesses)
        with self.assertRaises(TypeError):
            input_d18.inspect_matte(self.original.read_bytes(), self.master, self.image.read_bytes(),
                                     self.mask, **self.witnesses)
        self.assert_refused("input", self.run_pipeline(matte_source=None), 3, "matte_source_missing")
