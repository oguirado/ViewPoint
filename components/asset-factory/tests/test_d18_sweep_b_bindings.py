"""Witness CAD identity gates and BiRefNet informational pin consistency."""

import unittest

from asset_factory import contracts, core
import test_w3_cad_adapter as cad_fixtures
import test_n18_homepath_hygiene as hygiene_fixtures


class CadAdvisoryTests(unittest.TestCase):
    def setUp(self):
        case = cad_fixtures.AdapterTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case

    def test_declarations_can_be_omitted_and_a_legacy_denial_is_informational(self):
        case = self.case
        for key in ("jurisdiction", "commercial_use"):
            case.factory.pop(key)
            case.request["context"].pop(key)
        case.decision.update(configuration_sha256=core.sha256_json(case.factory))
        case.provider.update(runnable=True, commercial=False)
        case.provider.pop("blocked_reason")
        case.provider["runtime"].update(ram_peak_gb_measured=1,
            ram_peak_measurement_ref={"path": "measured.json", "sha256": "a" * 64})
        case.decision.update(provider_sha256=core.sha256_json(case.provider))
        result, gate = case.call()
        self.assertEqual(result["rc"], 3)
        self.assertEqual(gate["reason_codes"], ["production_cad_qualification_incomplete"])
        self.assertFalse((case.root / "output").exists())

    def test_cad_policy_configuration_and_provider_pins_remain_mandatory(self):
        case = self.case
        for field in ("provider_sha256", "configuration_sha256", "policy_sha256",
                      "provider_id", "operation"):
            with self.subTest(field=field):
                decision = case.decision.copy()
                case.decision[field] = "0" * 64
                result, gate = case.call()
                self.assertEqual(result["rc"], 4)
                self.assertEqual(gate["reason_codes"], ["decision_stale"])
                case.decision = decision

    def test_cad_config_and_policy_must_be_objects(self):
        case = self.case
        for field, digest in (("factory", "configuration_sha256"), ("licence", "policy_sha256")):
            with self.subTest(field=field):
                original = getattr(case, field)
                setattr(case, field, [])
                case.decision[digest] = core.sha256_json([])
                if field == "licence":
                    case.request["context"]["policy_sha256"] = case.decision[digest]
                result, gate = case.call()
                self.assertEqual(result["rc"], 2)
                self.assertEqual(gate["reason_codes"], ["cad_input_invalid"])
                setattr(case, field, original)
                case.decision[digest] = core.sha256_json(original)
                if field == "licence":
                    case.request["context"]["policy_sha256"] = case.decision[digest]

    def test_cad_decision_shape_remains_mandatory(self):
        self.case.decision["schema"] = "unknown"
        result, gate = self.case.call()
        self.assertEqual(result["rc"], 2)
        self.assertEqual(gate["reason_codes"], ["decision_invalid"])

    def test_slot_validator_still_rejects_blank_jurisdiction_when_supplied(self):
        self.case.request["context"]["jurisdiction"] = " "
        with self.assertRaises(core.ContractError) as caught:
            contracts.validate_slot(self.case.request, "cad_code", "request")
        self.assertEqual(caught.exception.path, "$/context/jurisdiction")

    def test_cad_blocked_provider_stays_refused(self):
        case = self.case
        case.provider["blocked_reason"] = "no_technical_admission"
        case.decision.update(provider_sha256=core.sha256_json(case.provider), rc=0,
                             verdict="PASS", allowed=True)
        result, gate = case.call()
        self.assertEqual(result["rc"], 5)
        self.assertEqual(gate["reason_codes"], ["provider_blocked"])


class SlotContextTests(unittest.TestCase):
    def test_all_shared_slots_preserve_technical_context_and_optional_declaration_types(self):
        root = hygiene_fixtures.ROOT / "slots"
        technical = {"policy_sha256": "a" * 64, "toolchain_sha256": "b" * 64, "timeout_s": 5}
        for slot in contracts.SLOTS:
            with self.subTest(slot=slot):
                schema = core.load_json(root / (slot + ".schema.json"), max_bytes=1 << 20)["$defs"]["context"]
                core.validate(technical, schema)
                core.validate({**technical, "jurisdiction": "ES"}, schema)
                core.validate({**technical, "commercial_use": False}, schema)
                for missing in technical:
                    with self.assertRaises(core.ContractError):
                        core.validate({k: v for k, v in technical.items() if k != missing}, schema)
                for field in ("jurisdiction", "commercial_use"):
                    with self.assertRaises(core.ContractError):
                        core.validate({**technical, field: []}, schema)


class BiRefNetPinTests(unittest.TestCase):
    def test_assessment_resource_and_chained_identities(self):
        factory = hygiene_fixtures.ROOT
        resource = "providers/birefnet/resources.py"
        resource_digest = core.sha256_file(factory / resource)
        root = factory / "providers/birefnet/rights"
        assessment = core.load_json(root / "assessment.json", max_bytes=1 << 20)

        def verify_code(code):
            files = [pin for pin in code["files"] if not pin["path"].startswith("$AF_CAMPAIGN_ROOT/")]
            resource_pin, = [pin for pin in files if pin["path"] == resource]
            self.assertEqual(resource_pin["sha256"], resource_digest)
            self.assertEqual(code["revision"], core.sha256_json([code["entrypoint"], *files]))

        for record in assessment["subjects"]:
            with self.subTest(subject=record["name"]):
                subject = record["subject"]
                verify_code(subject["code"])
                advisory = root / record["rights_file"]
                self.assertEqual(record["rights_sha256"], core.sha256_file(advisory))
                decision = core.load_json(advisory, max_bytes=1 << 20)
                self.assertEqual(decision["subject_sha256"], core.sha256_json(subject))
        for key in ("candidate_pin_refresh", "input_matte_source_pin_refresh", "a3_rp1_settle_pin_refresh"):
            with self.subTest(refresh=key):
                refresh = assessment[key]
                verify_code(refresh["code"])
                for record in assessment["subjects"]:
                    subject = {**record["subject"], "code": refresh["code"]}
                    self.assertEqual(refresh["subject_sha256"][record["name"]], core.sha256_json(subject))
