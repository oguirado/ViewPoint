"""D18 INPUT receipt wiring at the native pipeline entry point."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gates.input_d18 import inspect_matte, prepare_matte
from tools.input.evidence import bytes_digest, canonical_bytes
from tools.pipeline import engine
import test_d18_input_contract as input_fixture
from test_d18_input_contract import png
from test_f1u3_input_gate import calibration, policy as legacy_policy
import test_n18_sat_replay as replay_fixture
from test_n18_sat_replay import sha, stage_record, write


class IntakeMatteTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fixture = input_fixture.Mattes()
        self.fixture.setUp()
        self.original = self.root / "original.png"
        self.original.write_bytes(self.fixture.original)
        self.derivative = self.root / "submitted.png"
        self.matte = self.root / "matte.json"
        self.out = self.root / "pipeline"

    def pin(self, role, data):
        digest = bytes_digest(data)
        path = self.root / role / digest[7:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path

    def prepare(self, *, alpha=None):
        f = self.fixture
        if alpha is None:
            alpha = f.alpha
        master = png("RGBA", f.size, [v for i, a in enumerate(alpha)
                                    for v in (*f.rgb[i * 3:i * 3 + 3], a)])
        intent = f.store.add("matte-intent", f.intent)
        self.approve_intent(intent, f.store.read("matte-intent", intent))
        for (role, _pin), raw in f.store.items():
            if role in ("matte-intent", "input-selection", "selected-frame-rgb"):
                self.pin(role, raw)
        self.pin("original-rgb", f.original)
        self.pin("straight-alpha", master)
        self.pin("approved-mask", f.reference)
        return intent, master, prepare_matte(
            f.original, master, f.reference, intent_digest=intent, read_checked=f.store.read,
            matte_source=f.matte_source,
        )

    def approve_intent(self, pin, raw):
        from test_n18_int_intent import approval_store

        self.approval = approval_store(self.root / "operator", self.fixture.intent, pin, raw)

    def factory_fixture(self, path=None):
        if path is None:
            path = self.root / "factory-intake.json"
            config = {"schema": "af.factory-config/v1"}
        else:
            config = engine.load(path)
        return write(path, dict(config, matte_approval_store=self.approval))

    def invoke(self, expected_rc, reason=None):
        config = self.factory_fixture()
        with patch.dict(os.environ, AF_FACTORY_CONFIG_SHA256=sha(config)):
            result, _ = replay_fixture.Replay().invoke_pipeline(
                "image", self.derivative, self.original, self.out, "--matte-receipt", self.matte,
                "--factory-config", config,
            )
        row = stage_record(result, "input")
        stage = engine.load(row["receipt"]["path"])
        self.assertEqual(row["rc"], expected_rc, stage)
        if reason:
            self.assertEqual(stage["reason_codes"], [reason], stage)
        provider = engine.load(stage_record(result, "provider")["receipt"]["path"])
        self.assertEqual(provider["reason_codes"], [
            "upstream_blocked" if expected_rc else "trellis2_service_not_built_or_transport_absent"
        ])
        return stage

    def test_sat_replay_derivative(self):
        intent, _, (submitted, matte) = self.prepare()
        self.assertEqual(matte["source_rc"], 0, matte)
        self.derivative.write_bytes(submitted)
        write(self.matte, matte)
        stage = self.invoke(0)
        intake = next(ref for ref in stage["evidence"] if Path(ref["path"]).name == "intake.json")
        receipt = engine.load(intake["path"], expected_sha256=intake["sha256"])
        self.assertEqual(receipt["matte_receipt_sha256"], sha(self.matte))
        self.assertEqual(receipt["derivative_sha256"], sha(self.derivative))
        self.assertEqual(receipt["intent_digest"], intent)
        self.assertIs(receipt["required_preprocess_image"], False)
        self.assertEqual(self.invoke(0), stage)

    def test_unwired_legacy_control(self):
        _, _, (submitted, _) = self.prepare()
        self.derivative.write_bytes(submitted)
        policy = legacy_policy()
        (self.root / "calibration.txt").write_bytes(calibration(policy))
        policy_path = write(self.root / "input-policy.json", policy)
        result, _ = replay_fixture.Replay().invoke_pipeline(
            "image", self.derivative, self.original, self.out, "--input-policy", policy_path
        )
        stage = stage_record(result, "input")
        self.assertEqual(stage["rc"], 5, stage)
        self.assertEqual(engine.load(stage["receipt"]["path"])["reason_codes"], ["input_gate_refused"])

    def test_derivative_byte_drift(self):
        _, _, (submitted, matte) = self.prepare()
        write(self.matte, matte)
        self.derivative.write_bytes(submitted + b"\n")
        self.invoke(4, "matte_derivative_stale")

    def test_missing_intent_provenance(self):
        intent, _, (submitted, matte) = self.prepare()
        self.derivative.write_bytes(submitted)
        write(self.matte, matte)
        (self.root / "operator" / "matte-intent" / intent[7:]).unlink()
        self.invoke(3, "missing_matte-intent")

    def test_matte_substitution_refreshes_intake_cache_key(self):
        _, _, (submitted, matte) = self.prepare()
        self.derivative.write_bytes(submitted)
        write(self.matte, matte)
        self.invoke(0)
        matte["domain_digest"] = bytes_digest(b"different intent")
        # Fixture approval selects a different digest whose authority bytes are absent.
        self.approve_intent(matte["domain_digest"], None)
        write(self.matte, matte)
        self.invoke(3, "missing_matte-intent")

    def test_replays_fine_structure_even_with_a_forged_pass(self):
        self.fixture.intent["regions"]["fine_structure"] = [[3, 2, 4, 3]]
        self.fixture.intent["fine_structure_alpha"] = "fractional"
        _, _, (submitted, valid) = self.prepare()
        self.assertEqual(valid["source_rc"], 0, valid)
        binary = [255 if a else 0 for a in self.fixture.alpha]
        intent, master, (_, refused) = self.prepare(alpha=binary)
        self.assertEqual(refused["reason_codes"], ["protected_fine_structure_alpha_binarized"])
        self.derivative.write_bytes(submitted)
        f = self.fixture
        refused = inspect_matte(f.original, master, submitted, f.reference,
                                intent_digest=intent, read_checked=f.store.read,
                                matte_source=f.matte_source)
        write(self.matte, refused)
        self.invoke(5, "protected_fine_structure_alpha_binarized")

        valid["input_refs"][1] = bytes_digest(master)
        write(self.matte, valid)
        self.invoke(5, "protected_fine_structure_alpha_binarized")

    def test_modified_success_receipt_is_stale(self):
        _, _, (submitted, matte) = self.prepare()
        self.derivative.write_bytes(submitted)
        matte["generator"]["required_preprocess_image"] = True
        self.matte.write_bytes(canonical_bytes(matte))
        self.invoke(4, "input_receipt_drift")
