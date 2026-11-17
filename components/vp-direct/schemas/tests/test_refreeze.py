"""W-B0′ wire contracts and negative controls. All examples are synthetic."""

import copy
import hashlib
import importlib.util
import unittest

from jsonschema import ValidationError

from test_schemas import PAIRS, ROOT, SCHEMAS, STORE, fixture, load, validator


spec = importlib.util.spec_from_file_location("vp_schema_validation", ROOT / "validation.py")
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)
validate = validation.validate_contract
POLICY = load(ROOT / "adapter_ops.json")
HEX = "a" * 64


def provider():
    return {"kind": "local_model", "model": {
        "name": "fixture-model", "role": "image->3D", "deps": [], "device": "gpu",
        "weights": {"format": "safetensors", "bytes_on_disk": 12, "tree_sha256": HEX,
                    "locator": "af://fixture/model"},
        "license": {"id": "fixture", "territory_ok": True},
        "qualification": {"state": "runnable", "reason": "synthetic",
                          "receipt": {"path": "fixture/qualification", "sha256": HEX}},
        "measured": [{"metric": "wall_s", "value": 1.2, "profile": "fixture",
                      "tag": "MEASURED", "source": {"path": "fixture/measurement", "sha256": HEX}}],
        "residency": {"mode": "cold_process", "lease": "gpu_exclusive", "resident_bytes": None},
    }}


def exchange(tool, args, result):
    req = dict(fixture("vp.control.req/v1"), op=tool, args=args)
    reply = dict(fixture("vp.control.reply/v1"), op=tool, result=result, code="OK")
    return req, reply


class RefreezeTests(unittest.TestCase):
    def test_all_examples_with_cross_field_validation(self):
        for name in SCHEMAS:
            with self.subTest(contract=name):
                validate(name, fixture(name))
                with self.assertRaises(ValidationError):
                    validate(name, fixture(name, "invalid"))

    def test_every_adapter_pair_has_examples_and_pinned_resources(self):
        ids = set()
        for tool, body in POLICY.items():
            self.assertEqual(set(PAIRS[tool]), set(body["ops"]))
            for op, row in body["ops"].items():
                case = PAIRS[tool][op]
                for key, stem in (("args_schema", "args"), ("reply_schema", "result")):
                    with self.subTest(tool=tool, op=op, part=stem):
                        name = row[key]
                        self.assertEqual(name, f"{tool}.{op}.{stem}/v1")
                        self.assertNotIn(name, ids)
                        ids.add(name)
                        resource = STORE["urn:" + name.replace("/", ":")]
                        self.assertEqual(hashlib.sha256(validation.canonical_bytes(resource)).hexdigest(),
                                         row[key + "_sha256"])
                        validate(name, case[stem])
                        with self.assertRaises(ValidationError):
                            validate(name, case["invalid_" + stem])
        self.assertEqual(set(PAIRS), set(POLICY))

    def test_each_adapter_round_trips_both_envelopes(self):
        for tool, operations in PAIRS.items():
            for op, case in operations.items():
                with self.subTest(tool=tool, op=op):
                    req, reply = exchange(tool, case["args"], case["result"])
                    validate("vp.control.req/v1", req)
                    validate("vp.oflane.req/v1", req)
                    if POLICY[tool]["ops"][op].get("refusal_only"):
                        reply.update(ok=False, code="ARGS_SCHEMA_ABSENT", result={})
                    validate("vp.oflane.reply/v1", reply)
                    validation.validate_adapter_exchange(req, reply)
                    with self.assertRaises(ValidationError):
                        validate("vp.control.req/v1", dict(req, args=case["invalid_args"]))

    def test_wrong_result_or_request_id_is_refused(self):
        rows = PAIRS["vp_capsule_call"]
        req, reply = exchange("vp_capsule_call", rows["add"]["args"], rows["stop"]["result"])
        with self.assertRaises(ValidationError):
            validation.validate_adapter_exchange(req, reply)
        reply.update(request_id="another-request", result=rows["add"]["result"])
        with self.assertRaises(ValidationError):
            validation.validate_adapter_exchange(req, reply)

    def test_wildcards_and_policy_without_binding(self):
        groups = {
            "vp_workspace_call": "layout_get layout_fold layout_move layout_tab layout_float layout_hide multislot_get multislot_set view_list view_select view_save rename preset_list preset_apply preset_save demo_start demo_stop demo_state demo_check_site storyboard_list storyboard_open",
            "vp_terminal_call": "screen_list screen_open screen_close profile_list profile_select",
            "vp_settings_call": "storage_get storage_set",
            "vp_perception_call": "sleep wake off status",
            "vp_guest_call": "pack_list pack_validate pack_import desktop_create desktop_stop viewer_attach viewer_detach snapshot_mark status",
            "vp_data_call": "resolve put lease_acquire lease_release status",
            "vp_factory_call": "af.generate af.matte af.repair af.lod af.uv_bake af.rig af.motion_from_video af.assemble af.render af.registry.resolve af.registry.put vqa3d.score vqa3d.gate af.publish af.lipsync",
        }
        for tool, expected in groups.items():
            self.assertLessEqual(set(expected.split()), POLICY[tool]["ops"].keys())
        for entry in POLICY.values():
            for row in entry["ops"].values():
                if row["binding"]["kind"] == "none":
                    self.assertFalse(row["supported"])
                if row["supported"]:
                    self.assertTrue(row["binding"]["source"]["sha256"])
        for op in ("layout_fold", "layout_move", "layout_tab", "layout_float", "layout_hide", "multislot_set"):
            self.assertEqual(POLICY["vp_workspace_call"]["ops"][op]["agent_refusal"], "LAYOUT_INPUT_MODE_READ")
        self.assertTrue(POLICY["vp_factory_call"]["ops"]["af.publish"]["operator_only"])
        self.assertTrue(POLICY["vp_guest_call"]["ops"]["pack_import"]["operator_only"])

    def test_critical_args_refuse_ambiguous_targets_and_nonliteral_argv(self):
        for tool, op in (("vp_capture_call", "screenshot"), ("vp_perception_call", "wake")):
            args = PAIRS[tool][op]["args"].copy()
            args.pop("session_id", None)
            args.pop("capsule_id", None)
            args.pop("surface_id", None)
            with self.assertRaises(ValidationError):
                validate(tool + ".args/v1", args)
            args.update(session_id="session")
            args["capsule_id" if op == "screenshot" else "surface_id"] = "surface"
            with self.assertRaises(ValidationError):
                validate(tool + ".args/v1", args)
        for value in ("x;y", "$(fixture)", "x|y", "x\ny", "x>y", "x\n", "a'b", 'a"b'):
            with self.assertRaises(ValidationError):
                validate("vp_guest_command_call.run.args/v1", {"op": "run", "workload_id": "w", "argv": [value]})

    def test_receipt_admission_gate_and_lineage(self):
        name = "vp.forge_receipt/v1"
        base = fixture(name)
        admit = dict(base, transition={"from": "approved", "to": "admitted"}, by="operator",
                     actor={"caller": "operator", "agent_instance": "fixture"}, manifest_generation=1,
                     capability_sha256=HEX)
        validate(name, admit)
        for changed in (dict(by="agent"), dict(capability_sha256=None), dict(manifest_generation=None),
                        dict(actor=base["actor"]), dict(lineage=dict(base["lineage"], prev_hash=HEX))):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                validate(name, dict(admit, **changed))
        for changed in (dict(capability_sha256=HEX), dict(gates=dict(base["gates"], oracle="not_run")),
                        dict(gates=dict(base["gates"], negative_control="passed_unexpectedly"))):
            with self.assertRaises(ValidationError):
                validate(name, dict(base, **changed))
        refused = dict(base, transition={"from": "quarantined", "to": "rejected"}, code="FORGE_GATE_FAILED",
                       gates=dict(base["gates"], negative_control="passed_unexpectedly"))
        validate(name, refused)

    def test_receipt_pins_canonical_forge_fixture_and_steps(self):
        receipt = fixture("vp.forge_receipt/v1")
        tool = fixture("vp.forge_tool/v1")
        encoded = validation.canonical_bytes(tool)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), receipt["tool_sha256"])
        self.assertNotEqual(hashlib.sha256(encoded.rstrip(b"\n")).hexdigest(), receipt["tool_sha256"])
        self.assertEqual([hashlib.sha256(validation.canonical_bytes(step)).hexdigest()
                          for step in tool["steps"]], receipt["steps_sha256"])

    def test_forge_subset_and_utf8_byte_limit(self):
        name = "vp.forge_tool/v1"
        tool = fixture(name)
        params = {"type": "object", "properties": {"label": {"type": "string", "maxLength": 256}},
                  "required": ["label"], "additionalProperties": False}
        for spec in (params, dict(params, properties={"label": {"enum": [True, 5, "x"]}})):
            validate(name, dict(tool, args_schema=spec))
        invalid = [
            dict(params, properties={str(k): {"type": "integer"} for k in range(17)}),
            dict(params, properties={"label": {"type": "string", "maxLength": 257}}),
            dict(params, properties={"label": {"$ref": "#"}}),
            dict(params, properties={"label": {"type": "array"}}),
            dict(params, additionalProperties=True), dict(params, required=["undeclared"]),
        ]
        wide = dict(params, properties={"label": {"enum": [str(i) + "é"*220 for i in range(10)]}})
        self.assertLess(len(validation.canonical_bytes(wide).decode("utf-8")), 4096)
        self.assertGreater(len(validation.canonical_bytes(wide)), 4096)
        for invalid_spec in [*invalid, wide]:
            with self.assertRaises(ValidationError):
                validate(name, dict(tool, args_schema=invalid_spec))

    def test_postconditions_are_closed_and_op_typed(self):
        name = "vp.forge_tool/v1"
        tool = fixture(name)
        for assertion in ({"op": "code_eq", "code": "OK"}, {"op": "field_eq", "field": "ok", "value": True},
                          {"op": "digest_eq", "field": "state_sha256", "sha256": HEX},
                          {"op": "ax_node_exists", "ax_stable_sha256": HEX},
                          {"op": "ax_text_sha256_eq", "sha256": HEX}, {"op": "frame_differs", "frame_sha256": HEX}):
            step = {**tool["steps"][0], "assert": assertion}
            validate(name, dict(tool, oracle=assertion, negative_control=assertion, steps=[step]))
            for bad in (dict(assertion, op="eval"), dict(assertion, script="fixture")):
                with self.assertRaises(ValidationError):
                    validate(name, dict(tool, oracle=bad))

    def test_model_qualification_and_hashed_measurements(self):
        name = "vp.capability/v1"
        cap = dict(fixture(name), provider=provider())
        validate(name, cap)
        for field, value in (("license", {"id": "fixture", "territory_ok": False}),
                             ("qualification", {"state": "runnable", "receipt": None, "reason": "fixture"}),
                             ("qualification", {"state": "unqualified", "receipt": None, "reason": "fixture"}),
                             ("qualification", {"state":"runnable","reason":"fixture","receipt":{"path":"AUSENTE","sha256":"0"*64}}),
                             ("name", "Hunyuan3D-2.1")):
            bad = copy.deepcopy(cap)
            bad["provider"]["model"][field] = value
            with self.assertRaises(ValidationError):
                validate(name, bad)
        for state in ("excluded", "unqualified"):
            entry = copy.deepcopy(cap)
            model = entry["provider"]["model"]
            model["qualification"].update(state=state, receipt=None)
            if state == "excluded":
                model.update(name="Hunyuan3D-2.1", license={"id": "fixture", "territory_ok": False})
            validate(name, dict(entry, supported=False))
            if state == "excluded":
                index = dict(fixture("vp.tool_route.index/v1"), entries=[dict(entry, supported=False)])
                self.assertFalse(validator("vp.tool_route.index/v1").is_valid(index))
        fact = cap["provider"]["model"]["measured"][0]
        del fact["source"]["sha256"]
        with self.assertRaises(ValidationError):
            validate(name, cap)

    def test_per_op_flags_projection_and_parked_operations(self):
        name = "vp.capability/v1"
        tool = "vp_capsule_call"
        def row(op):
            fields = ("supported", "operator_only", "risk_class", "args_schema_sha256", "reply_schema_sha256", "binding")
            return dict(op=op, **{k: POLICY[tool]["ops"][op][k] for k in fields})
        cap = dict(fixture(name), tool=tool, json_op=tool, ops=["add", "role_switch"], supported=False,
                   operator_only=False, op_table=[row("add"), row("role_switch")])
        validate(name, cap)
        for bad in (dict(cap, ops=["role_switch", "add"]), dict(cap, supported=True), dict(cap, operator_only=True)):
            with self.assertRaises(ValidationError):
                validate(name, bad)
        cap["op_table"][0].update(supported=True)
        with self.assertRaises(ValidationError):
            validate(name, dict(cap, supported=True))
        cap["op_table"][0]["binding"] = {"kind": "core_route", "name": "fixture", "source": cap["source"][0]}
        validate(name, dict(cap, supported=True))
        for forbidden in ("pin", "layout_save", "layout_load"):
            entry = dict(cap["op_table"][0], op=forbidden)
            with self.assertRaises(ValidationError):
                validate(name, dict(cap, supported=True, ops=[forbidden], op_table=[entry]))

    def test_index_calibration_digest_and_route_details(self):
        name = "vp.tool_route.index/v1"
        index = fixture(name)
        validate(name, index)
        for k, v in (("state", "uncalibrated"), ("wrong_match", 1), ("ood_abstain_rate_milli", 999),
                     ("margin_min", 1001), ("score_min", -1), ("margin_min", 0.5)):
            self.assertFalse(validator(name).is_valid(dict(index, calibration=dict(index["calibration"], **{k:v}))))
        with self.assertRaises(ValidationError):
            validate(name, dict(index, index_sha256="0"*64))
        reply = {"verdict": "ABSTAIN", "code": "ROUTE_ABSTAIN", "reason": "policy_denied",
                 "index_sha256": HEX, "candidates": []}
        for detail in ("model_unqualified", "model_excluded", "op_unbound"):
            validate("vp.tool_route.reply/v1", dict(reply, detail=detail))
            with self.assertRaises(ValidationError):
                validate("vp.tool_route.reply/v1", dict(reply, detail=detail, reason="absent_capability"))

    def test_correlated_receipts_and_capacity(self):
        n = "vp.snapshot_receipt/v1"
        snap = fixture(n)
        for change in (dict(participants=["guest", "disk"]), dict(acks=[{"participant":"guest", "generation":2}]),
                       dict(admission_closed=False), dict(active_writers=1)):
            with self.assertRaises(ValidationError):
                validate(n, dict(snap, **change))
        n = "vp.admission/v1"
        admission = fixture(n)
        for change in (dict(margin_percent=14), dict(surface_count=17), dict(gpu_dependent=True),
                       dict(reserved_peak_bytes=6)):
            with self.assertRaises(ValidationError):
                validate(n, dict(admission, **change))

    def test_forge_and_router_results_use_control_and_mailbox(self):
        for tool, result in (("vp_forge_validate", fixture("vp.forge_receipt/v1")),
                             ("vp_tool_route", fixture("vp.tool_route.reply/v1"))):
            reply = dict(fixture("vp.control.reply/v1"), op=tool, result=result)
            for envelope in ("vp.control.reply/v1", "vp.oflane.reply/v1"):
                validate(envelope, reply)
                with self.assertRaises(ValidationError):
                    validate(envelope, dict(reply, op="vp_capsule_call"))

    def test_every_adapter_candidate_keeps_its_flat_arguments(self):
        name = "vp.tool_route.reply/v1"
        for tool, cases in PAIRS.items():
            for op, case in cases.items():
                with self.subTest(tool=tool, op=op):
                    req, _ = exchange(tool, case["args"], case["result"])
                    reply = fixture(name)
                    candidate = reply["candidates"][0]
                    flat = dict(case["args"], request_id=req["request_id"], dry_run=req["dry_run"])
                    candidate.update(tool=tool, json=req, mcp_call={"name": tool, "arguments": flat})
                    validate(name, reply)
                    flat.pop("op")
                    with self.assertRaises(ValidationError):
                        validate(name, reply)

    def test_session_guest_pack_and_focus_boundaries(self):
        self.assertEqual(set(SCHEMAS["vp.session/v1"]["properties"]["state"]["enum"]),
                         {"LAUNCHING", "LOGIN_PENDING", "AGENT", "HUMAN", "CLOSED"})
        for mode in ("OFF", "ASLEEP", "WAKING", "QUARANTINED"):
            with self.assertRaises(ValidationError):
                validate("vp.session/v1", dict(fixture("vp.session/v1"), state=mode))
        guest = fixture("vp.guest/v1")
        for change in (dict(workload_id="foreign"), dict(custody=dict(vmm=False, disk=True, snapshot=False))):
            with self.assertRaises(ValidationError):
                validate("vp.guest/v1", dict(guest, **change))
        pack = fixture("vp.image_pack/v1")
        for field, value in (("isa", "x86_64"), ("acceleration", "tcg"), ("ram_bytes", 1)):
            with self.assertRaises(ValidationError):
                validate("vp.image_pack/v1", dict(pack, resolved=dict(pack["resolved"], **{field:value})))
        trace = fixture("vp.focus_trace/v1")
        for change in (dict(end_mono_ns=0), dict(events=[dict(kind="XInput2", mono_ns=2, key="fixture")])):
            with self.assertRaises(ValidationError):
                validate("vp.focus_trace/v1", dict(trace, **change))

    def test_mutant_drop_territory_check(self):
        name = "vp.capability/v1"
        cap = dict(fixture(name), provider=provider())
        cap["provider"]["model"]["license"]["territory_ok"] = False
        self.assertFalse(validator(name).is_valid(cap))
        mutant = copy.deepcopy(SCHEMAS[name])
        for rule in mutant["allOf"]:
            if "provider" in rule["if"].get("properties", {}):
                del rule["then"]["properties"]["provider"]["properties"]["model"]["properties"]["license"]
        self.assertTrue(validator(name, mutant).is_valid(cap))

    def test_mutant_accept_uncalibrated_index(self):
        name = "vp.tool_route.index/v1"
        payload = fixture(name)
        payload["calibration"]["state"] = "uncalibrated"
        self.assertFalse(validator(name).is_valid(payload))
        mutant = copy.deepcopy(SCHEMAS[name])
        del mutant["properties"]["calibration"]["properties"]["state"]["const"]
        self.assertTrue(validator(name, mutant).is_valid(payload))

    def test_mutant_drop_operator_admission_gate(self):
        name = "vp.forge_receipt/v1"
        payload = dict(fixture(name), transition={"from":"approved","to":"admitted"}, by="agent",
                       capability_sha256=HEX, manifest_generation=1)
        self.assertFalse(validator(name).is_valid(payload))
        mutant = copy.deepcopy(SCHEMAS[name])
        rule = mutant["allOf"][0]["then"]["properties"]
        del rule["by"], rule["actor"]
        self.assertTrue(validator(name, mutant).is_valid(payload))

