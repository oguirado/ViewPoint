"""R-SCH fixtures and discriminating schema controls."""

import copy
import hashlib
import unittest

from jsonschema import ValidationError

from test_refreeze import POLICY, exchange, validate, validation
from test_schemas import PAIRS, SCHEMAS, STORE, fixture, validator


EVENT = "lineage.event/v1"
RUN = "lineage.run/v1"
FORGE = "vp.forge_receipt/v1"
TRANSITION = "vp.transition_receipt/v1"
HUMAN = "vp.human_lease/v1"
PACK = "vp.image_pack/v1"
CHANNEL = "vp.channel.req/v1"
ROUTE = "vp.tool_route.reply/v1"
FACTORY = "vp_factory_call"
SHA = "a" * 64


def signed_event(kind, payload, **fields):
    event = fixture(EVENT)
    event.update(kind=kind, payload=payload, **fields)
    request = {field: event[field] for field in ("agent", "op_id", "kind", "payload", "evidence")}
    event["request_sha256"] = hashlib.sha256(validation.canonical_bytes(request)[:-1]).hexdigest()
    del event["sha256"]
    return dict(event, sha256=hashlib.sha256(validation.canonical_bytes(event)[:-1]).hexdigest())


class RepairSchemaTests(unittest.TestCase):
    def assert_invalid(self, contract, payload):
        with self.assertRaises(ValidationError):
            validate(contract, payload)

    def test_lineage_offline_human_forge_and_run_close(self):
        forge = fixture(FORGE)
        body = {field: value for field, value in forge.items() if field != "lineage"}
        event = signed_event(FORGE, body)
        self.assertEqual(event, fixture(EVENT))
        validate(EVENT, event)
        line_sha = hashlib.sha256(validation.canonical_bytes(event)).hexdigest()
        reference = dict(forge["lineage"], lamport=event["lamport"],
                         prev_hash=event["prev_sha256"], event_hash=line_sha)
        validate(FORGE, dict(body, lineage=reference))

        validate(EVENT, signed_event(HUMAN, fixture(HUMAN)))
        run = fixture(RUN)
        validate(RUN, run)
        validate(EVENT, signed_event("run.close", run))
        validate(EVENT, signed_event("run.close", {
            "record": run, "memory_receipts": [],
            "gate": {"passed": True, "checks": [{"id": "fixture", "passed": True}]},
        }))

    def test_lineage_refuses_unregistered_kind_and_invalid_domain_payloads(self):
        event = fixture(EVENT)
        self.assert_invalid(EVENT, fixture(EVENT, "invalid"))
        self.assert_invalid(EVENT, signed_event("test.unregistered", event["payload"]))
        self.assert_invalid(EVENT, signed_event(HUMAN, dict(fixture(HUMAN), by="agent")))
        self.assert_invalid(RUN, fixture(RUN, "invalid"))
        self.assert_invalid(EVENT, signed_event("run.close", fixture(RUN, "invalid")))
        for kind in ("agent.born", "memory.put", "bench.declared"):
            with self.subTest(kind=kind):
                self.assert_invalid(EVENT, signed_event(kind, {}))
        body = event["payload"]
        for change in ({"risk": dict(body["risk"], risk_class="observar", child_max="enviar")},
                       {"transition": {"from": "approved", "to": "admitted"}, "by": "agent"}):
            self.assert_invalid(EVENT, signed_event(FORGE, dict(body, **change)))

    def test_lineage_preserves_viewpoint_request_ids_and_normalized_evidence(self):
        evidence = {"path": "fixture space/render #1.png", "sha256": SHA}
        for kind, payload in ((HUMAN, fixture(HUMAN)), ("run.close", fixture(RUN))):
            with self.subTest(kind=kind):
                validate(EVENT, signed_event(kind, payload, op_id="_legacy-01",
                                             evidence=[evidence, evidence]))
                for path in ("a//b", "a/../b", "C:fixture", "/fixture", "a/./b"):
                    self.assert_invalid(EVENT, signed_event(kind, payload,
                                                            evidence=[dict(evidence, path=path)]))

    def test_lineage_transition_payload_is_typed_and_requires_all_acks(self):
        receipt = dict(fixture(TRANSITION), acks=["identity", "capability", "stream"])
        event = signed_event(TRANSITION, receipt, op_id="_transition-01")
        validate(EVENT, event)
        self.assert_invalid(EVENT, signed_event(TRANSITION, fixture(HUMAN)))
        for invalid in (dict(receipt, complete=False), dict(receipt, acks=["stream"]),
                        dict(receipt, not_verified=["missing evidence"]), dict(receipt, to="OFF")):
            self.assert_invalid(EVENT, signed_event(TRANSITION, invalid))

    def test_mutant_allow_sealed_forge_event_is_discriminated(self):
        event = fixture(EVENT, "invalid")
        self.assert_invalid(EVENT, event)
        mutant = copy.deepcopy(SCHEMAS[EVENT])
        forge_rule = next(rule for rule in mutant["allOf"] if
                          rule["if"]["properties"]["kind"]["const"] == FORGE)
        forge_rule["then"]["properties"]["payload"] = {"$ref": "urn:vp.forge_receipt:v1"}
        validator(EVENT, mutant).validate(event)

    def test_vqa3d_quality_results_validate_through_adapter_exchange(self):
        legacy = {"artifact_refs": [], "receipt": {"path": "fixture", "sha256": SHA}}
        for op in ("vqa3d.score", "vqa3d.gate"):
            with self.subTest(op=op):
                row = POLICY[FACTORY]["ops"][op]
                case = PAIRS[FACTORY][op]
                quality = case["result"]
                validate(row["reply_schema"], legacy)
                validate(row["reply_schema"], dict(legacy, **quality))
                validation.validate_adapter_exchange(*exchange(FACTORY, case["args"], quality))
                self.assertFalse(row["supported"])
                self.assertEqual(row["binding"]["kind"], "none")
                with self.assertRaises(ValidationError):
                    validation.validate_adapter_exchange(*exchange(
                        FACTORY, case["args"], case["invalid_result"]
                    ))

    def test_vqa3d_refuses_invalid_score_and_predicates_and_allows_failed_gate(self):
        score_op = "vqa3d.score"
        score = PAIRS[FACTORY][score_op]["result"]
        score_contract = POLICY[FACTORY]["ops"][score_op]["reply_schema"]
        for bad_score in (-1, 1_000_001, float("nan"), 3.5):
            self.assert_invalid(score_contract, dict(score, score=bad_score))
        for bad_result in (dict(score, predicates=[]), dict(score, scene_digest="x" * 64),
                           dict(score, predicates=[{"render": "../frame.png", "frame_not_blank": True}]),
                           dict(score, receipt={"path": "fixture", "sha256": SHA})):
            self.assert_invalid(score_contract, bad_result)

        gate_op = "vqa3d.gate"
        gate = PAIRS[FACTORY][gate_op]["result"]
        gate_contract = POLICY[FACTORY]["ops"][gate_op]["reply_schema"]
        mixed = [*gate["predicates"], {"render": "render/still/still_000001.png", "frame_not_blank": False}]
        validate(gate_contract, dict(gate, predicates=mixed, accepted=False))
        self.assert_invalid(gate_contract, dict(gate, predicates=mixed))

    def test_mutant_allow_contradictory_vqa3d_gate_is_discriminated(self):
        op = "vqa3d.gate"
        contract = POLICY[FACTORY]["ops"][op]["reply_schema"]
        invalid = PAIRS[FACTORY][op]["invalid_result"]
        self.assert_invalid(contract, invalid)
        mutant = copy.deepcopy(STORE["urn:" + contract.replace("/", ":")])
        del mutant["then"]["if"]
        del mutant["then"]["then"]
        del mutant["then"]["else"]
        validator(contract, mutant).validate(invalid)

    def test_image_pack_supports_both_layouts_and_requires_matching_pins(self):
        initrdless = fixture(PACK)
        self.assertNotIn("initrd", initrdless["requested"])
        self.assertNotIn("initrd", initrdless["resolved"])
        validate(PACK, initrdless)
        validate("vp_guest_call.pack_validate.args/v1", {"op": "pack_validate", "pack": initrdless})
        validate("vp_guest_call.pack_validate.result/v1", initrdless)
        pinned = copy.deepcopy(initrdless)
        for side in ("requested", "resolved"):
            pinned[side]["initrd"] = {"path": "fixture/initrd", "sha256": SHA}
        validate(PACK, pinned)
        self.assert_invalid(PACK, fixture(PACK, "invalid"))
        self.assert_invalid(PACK, dict(pinned, resolved=initrdless["resolved"]))
        pinned["resolved"]["initrd"]["sha256"] = "b" * 64
        self.assert_invalid(PACK, pinned)
        for name in ("kernel", "root"):
            incomplete = copy.deepcopy(initrdless)
            del incomplete["requested"][name]
            self.assert_invalid(PACK, incomplete)

    def test_mutant_requiring_initrd_breaks_supported_pack(self):
        pack = fixture(PACK)
        validate(PACK, pack)
        mutant = copy.deepcopy(SCHEMAS[PACK])
        mutant["properties"]["requested"]["required"].append("initrd")
        self.assertFalse(validator(PACK, mutant).is_valid(pack))

    def test_route_unbound_is_policy_abstention_and_absence_has_no_detail(self):
        result = fixture("vp.tool_route.reply.op_unbound/v1")
        validate(ROUTE, result)
        request, reply = exchange("vp_tool_route", {"question": "Do an unbound adapter op."}, result)
        reply["request_id"] = request["request_id"]
        for contract in ("vp.control.reply/v1", "vp.oflane.reply/v1"):
            validate(contract, reply)
            self.assert_invalid(contract, dict(reply, result=dict(result, reason="absent_capability")))
        absent = dict(result, reason="absent_capability")
        del absent["detail"]
        validate(ROUTE, absent)
        self.assert_invalid(ROUTE, fixture("vp.tool_route.reply.op_unbound/v1", "invalid"))

    def test_mutant_allow_unbound_as_absent_is_discriminated(self):
        invalid = fixture("vp.tool_route.reply.op_unbound/v1", "invalid")
        self.assert_invalid(ROUTE, invalid)
        mutant = copy.deepcopy(SCHEMAS[ROUTE])
        mutant["allOf"] = [rule for rule in mutant["allOf"] if
                           rule["if"].get("required") != ["detail"]]
        validator(ROUTE, mutant).validate(invalid)

    def test_channel_vm_target_requires_typed_frame_witness(self):
        request = fixture("vp.channel.req.vm/v1")
        validate(CHANNEL, fixture(CHANNEL))
        validate(CHANNEL, request)
        self.assert_invalid(CHANNEL, fixture("vp.channel.req.vm/v1", "invalid"))
        for target_changes in ({}, {"sha256": "f" * 63}, {"sha256": "G" * 64},
                               {"sha256": SHA, "retired": False}):
            target = dict(request["target"], based_on_frame=target_changes)
            self.assert_invalid(CHANNEL, dict(request, target=target))
        self.assert_invalid(CHANNEL, dict(request, target={"based_on_frame": {"sha256": SHA}}))
        self.assert_invalid(CHANNEL, dict(request, target=dict(request["target"], surface_kind="host")))

    def test_mutant_allow_vm_without_frame_is_discriminated(self):
        invalid = fixture("vp.channel.req.vm/v1", "invalid")
        self.assert_invalid(CHANNEL, invalid)
        mutant = copy.deepcopy(SCHEMAS[CHANNEL])
        del mutant["properties"]["target"]["dependentRequired"]
        validator(CHANNEL, mutant).validate(invalid)
