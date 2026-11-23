"""Record and reader regressions from the independent BASE review."""
import copy
import json
import unittest
from unittest.mock import patch

import jsonschema

from asset_factory import math_acceptance as math
import test_d18_base_evidence as fixtures


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceTests()
        self.fixture.setUp()

    def test_nonpassing_attempts_require_their_own_receipts(self):
        f = self.fixture
        obs = f.receipt["observations"][0]
        for endpoint, rc in (([1, 1], 5), ([0, 1], 3)):
            obs.update(lower=endpoint, upper=[1, 1])
            gate = f.assess()["gate"]
            self.assertEqual(gate["rc"], rc)
            for kind in ("asset", "part"):
                record = f.make_record(kind, copy.deepcopy(gate))
                with self.subTest(kind=kind, rc=rc):
                    raw = fixtures.encoded(record)
                    result = f.verify(raw)
                    self.assertEqual(result["rc"], 3)
                    self.assertEqual(result["reasons"], ["missing_required_gate"])
                    self.assertIsNone(result["record_digest"])
                    entry = record["gates"][0]
                    for edit, reason in (({"receipt_digest": fixtures.WITNESS}, "missing:stage"),
                                         ({"rc": 3 if rc == 5 else 5,
                                           "verdict": "CANNOT_SEE" if rc == 5 else "FAIL"},
                                          "record_gate_mismatch")):
                        entry.update(edit)
                        result = f.verify(fixtures.encoded(record))
                        self.assertIsNone(result["record_digest"])
                        self.assertIn(reason, result["reasons"])
                        entry.update(copy.deepcopy(gate))
                    entry["metrics"][0]["value"] += 1
                    self.assertIn("record_gate_mismatch", f.verify(fixtures.encoded(record))["reasons"])

    def test_older_failure_and_approved_pass_can_coexist(self):
        f = self.fixture
        passed = f.assess()["gate"]
        f.receipt["observations"][0].update(lower=[1, 1], upper=[1, 1])
        failed = f.assess()["gate"]
        record = f.make_record("asset", passed)
        record["gates"] = sorted([failed, passed], key=lambda g: g["receipt_digest"])
        raw = fixtures.encoded(record)
        self.assertEqual(f.verify(raw)["record_digest"], fixtures.digest(raw))
        failed["metrics"][0]["value"] = 0
        self.assertIsNone(f.verify(fixtures.encoded(record))["record_digest"])

    def test_foreign_failure_cannot_be_archived_with_an_approved_pass(self):
        f = self.fixture
        passed = f.assess()["gate"]
        foreign = fixtures.digest(b"other-asset")
        f.receipt["subject_digest"] = foreign
        f.receipt["observations"][0].update(lower=[1, 1000], upper=[1, 1000])
        for observation in f.receipt["observations"]:
            certificate = json.loads(f.store["certificate", observation["certificate_ref"]])
            certificate.update(subject_digest=foreign, lower=observation["lower"], upper=observation["upper"])
            observation["certificate_ref"] = f.store_object("certificate", certificate)
        decision = f.assess(refresh_certificates=False, subject_digest=foreign)
        self.assertEqual(decision["rc"], 5, decision)
        record = f.make_record("asset", passed)
        record["gates"] = sorted([passed, decision["gate"]], key=lambda g: g["receipt_digest"])
        result = f.verify(fixtures.encoded(record))
        self.assertEqual((result["rc"], result["reasons"], result["record_digest"]),
                         (4, ["foreign_record_subject"], None))

    def test_failure_under_an_unapproved_profile_cannot_be_archived(self):
        f = self.fixture
        f.receipt["observations"][0].update(lower=[1, 1000], upper=[1, 1000])
        decision = f.assess()
        self.assertEqual(decision["rc"], 5, decision)
        record = f.make_record("asset", decision["gate"])
        result = f.verify(fixtures.encoded(record), {})
        self.assertEqual((result["rc"], result["reasons"], result["record_digest"]),
                         (4, ["unapproved_record_scope"], None))

    def test_qualification_scope_for_both_bases(self):
        f = self.fixture
        for key, value in (("gate_id", "texture"), ("operation", "generate"), ("role", "prop"),
                           ("reference_refs", [fixtures.POLICY]), ("input_refs", [fixtures.DOMAIN])):
            with self.subTest(key=key):
                f.setUp()
                f.profile[key] = value
                if key in f.receipt:
                    f.receipt[key] = value
                if key == "gate_id":
                    control = json.loads(f.store["negative-control", f.control])
                    control["gate_id"] = value
                    pin = f.store_object("negative-control", control)
                    f.receipt["negative_control_receipt_refs"] = [pin]
                    f.profile["negative_control_receipt_refs"] = [pin]
                result = f.assess()
                self.assertEqual(result["rc"], 4)
                self.assertEqual(result["reasons"], ["links:qualification_scope_drift",
                                                     "surface:qualification_scope_drift"])

    def test_invalid_scope_maps_and_reader(self):
        f = self.fixture
        raw = fixtures.encoded(f.make_record("asset", f.assess()["gate"]))
        pin = f.receipt["profile_digest"]
        key = ("mesh", fixtures.SUBJECT)
        for scopes in (None, [], {"mesh": {pin}}, {("mesh", "bad"): {pin}},
                       {key: pin}, {key: [pin]}, {key: {"bad"}}):
            with self.subTest(scopes=scopes):
                self.assertEqual(math.verify_record(raw, approved_scopes=scopes,
                                 read_checked=lambda r, p: f.store.get((r, p)))["rc"], 2)
        self.assertEqual(f.verify(raw, {key: frozenset({pin})})["rc"], 0)
        self.assertEqual(math.verify_record(raw, approved_scopes={key: {pin}},
                                            read_checked=None)["rc"], 2)
        self.assertEqual(f.assess(read_checked=None)["rc"], 2)

    def test_reader_errors_at_each_role(self):
        f = self.fixture
        raw = fixtures.encoded(f.make_record("asset", f.assess()["gate"]))
        scopes = {("mesh", fixtures.SUBJECT): {f.receipt["profile_digest"]}}
        for role in ("profile", "stage", "certificate", "negative-control", "proof",
                     "calibration", "derivation", "held-out-calibration"):
            for bad, rc, reason in ((RuntimeError(), 3, "reader_error:"), ("wrong", 2, "expected_bytes:")):
                with self.subTest(role=role, rc=rc):
                    def reader(requested, pin):
                        if requested != role:
                            return f.store.get((requested, pin))
                        if isinstance(bad, Exception):
                            raise bad
                        return bad

                    for result in (f.assess(read_checked=reader),
                                   math.verify_record(raw, approved_scopes=scopes, read_checked=reader)):
                        self.assertEqual((result["rc"], result["reasons"]), (rc, [reason + role]))

    def test_negative_control_exact_fields(self):
        f = self.fixture
        control = json.loads(f.store["negative-control", f.control])
        for invalid in (dict(control, extra=True), {k: v for k, v in control.items() if k != "gate_id"}):
            pin = f.store_object("negative-control", invalid)
            f.profile["negative_control_receipt_refs"] = f.receipt["negative_control_receipt_refs"] = [pin]
            self.assertEqual(f.assess()["rc"], 2)

    def test_validator_requires_distribution_module(self):
        math._validator.cache_clear()
        self.addCleanup(math._validator.cache_clear)
        with patch.object(jsonschema, "__file__", __file__):
            self.assertEqual(self.fixture.assess()["rc"], 3)

    def test_actual_factory_schema_negative_at_startup(self):
        cls = jsonschema.Draft202012Validator
        real = cls.iter_errors

        def selective(validator, instance, *args, **kwargs):
            if validator.schema.get("$id", "").startswith("urn:af:"):
                return iter(())
            return real(validator, instance, *args, **kwargs)

        math._validator.cache_clear()
        self.addCleanup(math._validator.cache_clear)
        with patch.object(cls, "iter_errors", selective):
            self.assertEqual(self.fixture.assess()["rc"], 3)

    def test_validator_schema_error_is_typed(self):
        math._validator.cache_clear()
        self.addCleanup(math._validator.cache_clear)
        with patch.object(jsonschema.Draft202012Validator, "check_schema",
                          side_effect=jsonschema.exceptions.SchemaError("fixture")):
            self.assertEqual(self.fixture.assess()["rc"], 3)

    def test_canonical_protocol_bytes(self):
        f = self.fixture
        for role in ("profile", "stage", "certificate", "negative-control"):
            with self.subTest(role=role):
                f.setUp()
                original = f.store_object

                def store(kind, obj):
                    if kind != role:
                        return original(kind, obj)
                    raw = fixtures.encoded(obj).rstrip(b"\n")
                    pin = fixtures.digest(raw)
                    f.store[kind, pin] = raw
                    return pin

                # Pin the actual noncanonical bytes, including all dependent envelopes.
                with patch.object(f, "store_object", store):
                    if role == "negative-control":
                        pin = store(role, json.loads(f.store[role, f.control]))
                        f.profile["negative_control_receipt_refs"] = [pin]
                        f.receipt["negative_control_receipt_refs"] = [pin]
                    result = f.assess()
                    self.assertEqual(result["rc"], 2)
                    self.assertTrue(any(r.endswith("noncanonical_" + role) for r in result["reasons"]))
