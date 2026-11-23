"""IU00 boundary controls with an in-memory, independently populated witness store."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from asset_factory import core, math_acceptance as math


def encoded(obj):
    return core.canonical_json(obj).encode("utf-8")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


SUBJECT, POLICY, DOMAIN, EVALUATOR, TOOL, METHOD, WITNESS = [
    digest(tag.encode()) for tag in ("subject", "policy", "domain", "eval", "tool", "method", "evidence")
]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.control = self.store_object("negative-control", {
            "expected_rc": 5, "observed_rc": 5, "gate_id": "mesh", "witness_digest": WITNESS,
            "policy_digest": POLICY, "domain_digest": DOMAIN, "evaluator_digest": EVALUATOR,
            "toolchain_digest": TOOL,
        })
        self.profile = {
            "schema": "af.acceptance-profile/v1", "gate_id": "mesh", "operation": "repair",
            "role": "character", "policy_digest": POLICY, "domain_digest": DOMAIN,
            "evaluator_digest": EVALUATOR, "toolchain_digest": TOOL,
            "input_refs": [SUBJECT], "reference_refs": [WITNESS], "criteria": [],
            "negative_control_receipt_refs": [self.control],
        }
        for name, basis in (("surface", "CALIBRATED"), ("links", "DERIVED")):
            criterion = {
                "id": name, "method_digest": METHOD, "unit": "ppm", "scale": 1000000,
                "operator": "le", "limit": 100, "basis": basis, "uncertainty_kind": "bounded",
                "min_confidence_ppm": None, "target_reason": None,
                "proof_receipt_digest": None, "calibration_receipt_digest": None,
            }
            self.qualify(criterion)
            self.profile["criteria"].append(criterion)
        self.receipt = {
            "schema": "af.math-evidence/v1", "subject_digest": SUBJECT,
            "gate_id": "mesh", "policy_digest": POLICY, "domain_digest": DOMAIN,
            "evaluator_digest": EVALUATOR, "toolchain_digest": TOOL,
            "input_refs": [SUBJECT], "reference_refs": [WITNESS],
            "source_rc": 0, "negative_control_receipt_refs": [self.control], "wall_ns": 15, "peak_bytes": 80,
            "work_counts": {"cells": 1}, "profile_digest": None,
            "observations": [
                {"id": name, "method_digest": METHOD, "unit": "ppm", "lower": [0, 1],
                 "upper": [1, 15000], "confidence_ppm": None, "certificate_ref": None}
                for name in ("links", "surface")
            ],
        }

    def store_object(self, role, obj):
        data = encoded(obj)
        key = digest(data)
        self.store[role, key] = data
        return key

    def qualify(self, criterion):
        fields = ("id", "method_digest", "unit", "scale", "operator", "limit", "basis",
                  "uncertainty_kind", "min_confidence_ppm")
        witness = {key: criterion[key] for key in fields}
        for key in ("policy_digest", "domain_digest", "evaluator_digest", "toolchain_digest",
                    "operation", "gate_id", "role", "reference_refs", "input_refs"):
            witness[key] = self.profile[key]
        underlying = "derivation" if criterion["basis"] == "DERIVED" else "held-out-calibration"
        witness["witness_digest"] = self.store_object(underlying, {"reviewed_fixture": underlying})
        role = "proof" if criterion["basis"] == "DERIVED" else "calibration"
        criterion[role + "_receipt_digest"] = self.store_object(role, witness)

    def assess(self, refresh_certificates=True, **overrides):
        profile_pin = self.store_object("profile", self.profile)
        self.receipt["profile_digest"] = profile_pin
        if refresh_certificates:
            for obs in self.receipt["observations"]:
                cert = {k: v for k, v in obs.items() if k != "certificate_ref"}
                cert.update(subject_digest=SUBJECT, profile_digest=profile_pin, gate_id=self.receipt["gate_id"],
                            domain_digest=DOMAIN, witness_digest=WITNESS)
                obs["certificate_ref"] = self.store_object("certificate", cert)
        args = dict(profile_digest=profile_pin, subject_digest=SUBJECT,
                    receipt_digest=self.store_object("stage", self.receipt),
                    read_checked=lambda role, pin: self.store.get((role, pin)))
        args.update(overrides)
        return math.evaluate_gate(**args)

    def test_both_qualification_paths_and_registry_endpoints(self):
        decision = self.assess()
        self.assertEqual(decision["rc"], 0, decision)
        gate = decision["gate"]
        self.assertEqual(gate["required_check_ids"], ["links", "surface"])
        self.assertEqual([m["value"] for m in gate["metrics"]], [67, 67])
        self.assertEqual(gate["metrics"][0]["criterion"]["basis"], "DERIVED")
        self.assertEqual(gate["metrics"][1]["uncertainty"]["lower"], 0)

    def test_required_coverage(self):
        self.receipt["observations"].pop()
        self.assertEqual(self.assess()["rc"], 3)
        self.receipt["observations"] *= 2
        self.assertEqual(self.assess()["rc"], 2)

    def test_profile_schema_and_duplicate_ids(self):
        self.profile["criteria"][0]["id"] = "links"
        self.assertEqual(self.assess()["rc"], 2)
        self.profile["criteria"] = []
        self.assertEqual(self.assess()["rc"], 2)

    def test_null_bound_and_forged_pass(self):
        self.receipt["observations"][0]["upper"] = None
        self.assertEqual(self.assess()["rc"], 3)
        self.receipt["verdict"] = "PASS"
        self.assertEqual(self.assess()["rc"], 2)

    def test_target_diagnostic(self):
        c = self.profile["criteria"][0]
        c.update(basis="TARGET", calibration_receipt_digest=None, target_reason="pending held-out run")
        result = self.assess()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["gate"]["metrics"][1]["value"], 67)

    def test_qualification_absent_wrong_role_or_stale(self):
        c = self.profile["criteria"][0]
        pin = c["calibration_receipt_digest"]
        witness = self.store.pop(("calibration", pin))
        self.store["proof", pin] = witness
        self.assertEqual(self.assess()["rc"], 3)
        self.store["calibration", pin] = witness + b" "
        self.assertEqual(self.assess()["rc"], 4)
        self.store["calibration", pin] = witness
        c["limit"] += 1
        self.assertEqual(self.assess()["rc"], 4)

    def test_replay_and_counterfeit_method(self):
        self.assertEqual(self.assess(subject_digest=WITNESS)["rc"], 4)
        self.receipt["observations"][0]["method_digest"] = WITNESS
        self.assertEqual(self.assess()["rc"], 4)

    def test_missing_or_unapproved_stage(self):
        self.assertEqual(self.assess(receipt_digest=WITNESS)["rc"], 3)
        self.receipt["negative_control_receipt_refs"] = []
        self.assertNotEqual(self.assess()["rc"], 0)
        self.receipt["negative_control_receipt_refs"] = [self.control]
        self.receipt["work_counts"]["cells"] = 0
        self.assertNotEqual(self.assess()["rc"], 0)

    def test_interval_exact_decision(self):
        obs = self.receipt["observations"][0]
        obs["upper"] = [10000001, 10**11]   # Slightly above the maximum.
        self.assertEqual(self.assess()["rc"], 3)
        obs["lower"] = obs["upper"]
        self.assertEqual(self.assess()["rc"], 5)
        obs["lower"] = [1, 10000]
        obs["upper"] = obs["lower"]
        self.assertEqual(self.assess()["rc"], 0)

    def test_minimum_equality_and_negative_quantities(self):
        rule = self.profile["criteria"][1]
        obs = self.receipt["observations"][0]
        rule.update(operator="ge", limit=-67)
        self.qualify(rule)
        obs.update(lower=[-1, 15000], upper=[0, 1])
        result = self.assess()
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["gate"]["metrics"][0]["value"], -67)
        rule.update(operator="eq", limit=0)
        self.qualify(rule)
        self.assertEqual(self.assess()["rc"], 3)
        obs["lower"] = [0, 1]
        self.assertEqual(self.assess()["rc"], 0)
        obs.update(lower=[1, 10**18], upper=[1, 10**18])
        self.assertEqual(self.assess()["rc"], 5)

    def test_sampled_risk_is_required_and_separate(self):
        rule = self.profile["criteria"][1]
        rule.update(uncertainty_kind="sampled", min_confidence_ppm=990000)
        self.qualify(rule)
        obs = self.receipt["observations"][0]
        for conf, expected in [(None, 3), (989999, 3), (990000, 0), (1000000, 2)]:
            with self.subTest(conf=conf):
                obs["confidence_ppm"] = conf
                self.assertEqual(self.assess()["rc"], expected)

    def test_exactness_requires_a_point(self):
        rule = self.profile["criteria"][1]
        rule.update(uncertainty_kind="exact")
        self.qualify(rule)
        self.assertEqual(self.assess()["rc"], 2)
        self.receipt["observations"][0]["lower"] = [1, 15000]
        self.assertEqual(self.assess()["rc"], 0)

    def test_bad_rationals_and_order(self):
        obs = self.receipt["observations"][0]
        for bound in ([1, 0], [1, -1], [True, 1], ["1", 1], [-1, 1], [1]):
            with self.subTest(bound=bound):
                obs["upper"] = bound
                self.assertEqual(self.assess()["rc"], 2)

    def test_canonical_record_identity(self):
        self.assertEqual(math.canonical_digest({"b": 1, "a": "é"}),
                         digest('{"a":"é","b":1}\n'.encode()))
        with self.assertRaises(core.ContractError):
            math.canonical_digest({"value": 1.5})

    def test_unqualified_validator(self):
        with patch.object(math, "_runtime_versions", return_value={}):
            self.assertEqual(self.assess()["rc"], 3)

    def make_record(self, kind, gate):
        missing = "fixture lacks observation"
        rep = dict.fromkeys(("cad", "visual", "composition", "collision", "physics", "rig", "motion"))
        rep["absence_reasons"] = {}
        record = {
            "schema": "af." + kind + "-record/v1", "record_kind": kind,
            "canonical_profile": "af.integer-json-lf/v1", "display_name": "Fixture",
            "category": "prop", "taxonomy_path": "prop/handle", "annotations": {}, "labels": {},
            "classification": {"taxonomy_digest": POLICY, "method": "fixture",
                               "confidence_ppm": None, "receipt_digest": None, "absence_reason": missing},
            "dimensions": {"xyz_um": None, "evidence_class": "AUSENTE", "method": "fixture",
                           "receipt_digest": None, "absence_reason": missing},
            "coordinate_frame": "RH_Z_UP_FRONT_NEG_Y_UM",
            "symmetry": dict(kind="unknown", plane_normal_axis=None, plane_offset_um=None, radial_order=None,
                             counterpart_record_digest=None, receipt_digest=None, absence_reason=missing),
            "lineage": dict(operation="source", parent_records=[], parent_asset_digest=None, decomposition=None,
                            assembly_recipe_digest=None, generation_request_digest=None, source_view_digests=[],
                            original_record_digest=None, absence_reasons={}),
            "rights": {"status": "unknown", "components": [
                dict(component="fixture", provenance_digest=WITNESS, subject_digest=SUBJECT,
                     licence_expression=None, licence_text_digest=None, commercial=None, territory_excluded=[],
                     gated=None, absence_reason=missing)], "attributions": [], "decision_receipt_digests": []},
            "geometry_digest": SUBJECT, "representations": rep, "references": None,
            "files": [dict(digest=SUBJECT, role="mesh", logical_path="mesh.glb",
                           size_bytes=1, media_type="model/gltf-binary")],
            "instances": [], "sockets": [], "materials": [], "lods": [], "dependencies": [],
            "gates": [gate],
        }
        if kind == "part":
            record.update(anatomical_role="handle", side="not_applicable", reuse_status="candidate",
                          compatibility_profile_digests=[])
        return record

    def verify(self, raw, approved=None):
        if approved is None:
            approved = {("mesh", SUBJECT): {self.receipt["profile_digest"]}}
        return math.verify_record(raw, approved_scopes=approved,
                                  read_checked=lambda kind, pin: self.store.get((kind, pin)))

    def test_record_round_trip_and_forged_pass(self):
        gate = self.assess()["gate"]
        for kind in ("asset", "part"):
            with self.subTest(kind=kind):
                record = self.make_record(kind, copy.deepcopy(gate))
                data = encoded(record)
                self.assertEqual(self.verify(data)["record_digest"], digest(data))
                self.assertEqual(self.verify(data, {})["rc"], 4)
                self.assertEqual(self.verify(data.rstrip())["rc"], 2)
                self.assertEqual(self.verify(data.replace(b'"gates":', b'"gates":[],"gates":'))["rc"], 2)
                record["gates"][0]["metrics"][0]["value"] = 0
                self.assertNotEqual(self.verify(encoded(record))["rc"], 0)
                record["gates"][0]["metrics"].pop()
                self.assertNotEqual(self.verify(encoded(record))["rc"], 0)

    def test_closed_records_and_qualification_states(self):
        for kind in ("asset", "part"):
            gate = self.assess()["gate"]
            record = self.make_record(kind, gate)
            record["extra"] = 1
            self.assertEqual(self.verify(encoded(record))["rc"], 2)
            del record["extra"]
            gate["metrics"][0]["criterion"]["proof_receipt_digest"] = None
            self.assertEqual(self.verify(encoded(record))["rc"], 2)
            gate["metrics"][0]["criterion"]["proof_receipt_digest"] = WITNESS
            gate["metrics"][1]["criterion"]["calibration_receipt_digest"] = None
            self.assertEqual(self.verify(encoded(record))["rc"], 2)

    def test_unexpected_schema_or_runtime(self):
        with self.assertRaises(core.ContractError):
            math.canonical_digest({"ratio": float("nan")})
        p = self.profile
        p["$schema"] = "unregistered"
        self.assertEqual(self.assess()["rc"], 2)

    def test_changed_numeric_evidence_with_a_real_certificate(self):
        self.assertEqual(self.assess()["rc"], 0)
        self.receipt["observations"][0]["upper"] = [0, 1]
        self.assertEqual(self.assess(refresh_certificates=False)["rc"], 4)

    def test_cross_record_subject_and_omitted_gate(self):
        gate = self.assess()["gate"]
        record = self.make_record("asset", gate)
        record["geometry_digest"] = record["files"][0]["digest"] = WITNESS
        self.assertEqual(self.verify(encoded(record))["rc"], 4)
        record["gates"] = []
        self.assertEqual(self.verify(encoded(record))["rc"], 3)
        archived = self.verify(encoded(record), {})
        self.assertEqual(archived["record_digest"], digest(encoded(record)))
        self.assertNotIn("verdict", archived)

    def test_draft_pin_drift(self):
        math._validator.cache_clear()
        self.addCleanup(math._validator.cache_clear)
        with patch.object(math.Path, "read_bytes", return_value=b'{"unknownKeyword":true}'):
            self.assertEqual(self.assess()["rc"], 4)

    def test_receipt_roles_and_budget(self):
        for role in ("negative-control", "calibration", "proof", "derivation", "held-out-calibration"):
            saved = self.store.copy()
            with self.subTest(role=role):
                self.store = {key: value for key, value in self.store.items() if key[0] != role}
                self.assertEqual(self.assess()["rc"], 3)
            self.store = saved
        self.receipt["source_rc"] = 6
        self.assertEqual(self.assess()["rc"], 6)
        self.receipt["source_rc"] = 1
        self.assertEqual(self.assess()["rc"], 2)

    def test_validator_rejects_a_disabled_implementation(self):
        from jsonschema import Draft202012Validator
        math._validator.cache_clear()
        self.addCleanup(math._validator.cache_clear)
        with patch.object(Draft202012Validator, "iter_errors", return_value=iter(())):
            self.assertEqual(self.assess()["rc"], 3)

    def test_wrong_negative_control(self):
        wrong = self.store_object("negative-control", {
            "expected_rc": 5, "observed_rc": 0, "gate_id": "mesh", "witness_digest": WITNESS,
            "policy_digest": POLICY, "domain_digest": DOMAIN, "evaluator_digest": EVALUATOR,
            "toolchain_digest": TOOL,
        })
        self.profile["negative_control_receipt_refs"] = [wrong]
        self.receipt["negative_control_receipt_refs"] = [wrong]
        self.assertNotEqual(self.assess()["rc"], 0)

    def test_exact_arithmetic_large_and_signed(self):
        denominator = 10 ** 80
        self.assertEqual(math.outward_bounds([-1, denominator], [denominator + 1, denominator],
                                            1000000), (-1, 1000001))
        with self.assertRaises(ValueError):
            math.outward_bounds([0, 1], [0, 1], scale=True)
