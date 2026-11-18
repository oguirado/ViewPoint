"""Structural freeze gates. No network, profile reads, GUI calls or model calls."""

import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from jsonschema import Draft202012Validator, RefResolver


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
EXAMPLES = ROOT / "tests" / "examples"
CAMPAIGN = REPO.parent / "vpcomms-campaign"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


SCHEMAS = {load(p)["title"]: load(p) for p in ROOT.glob("*.schema.json")}
INVENTORY = load(ROOT / "tests" / "contract_inventory.json")
STORE = {resource["$id"]: resource for schema in SCHEMAS.values()
         for resource in (schema, *schema.get("$defs", {}).values()) if "$id" in resource}
PAIRS = load(EXAMPLES / "adapter_pairs.json")


class LocalResolver(RefResolver):
    def resolve_remote(self, uri):
        raise AssertionError("Only registered local schema references are allowed: " + uri)


def validator(identifier, replacement=None):
    schema = SCHEMAS[identifier] if replacement is None else replacement
    return Draft202012Validator(
        schema, resolver=LocalResolver.from_schema(schema, store=STORE),
    )


def fixture(identifier, kind="valid"):
    name = identifier.replace("/", "_") + "." + kind + ".json"
    return load(EXAMPLES / name)


def object_paths(value, path=()):
    if isinstance(value, dict):
        yield path
        for key, item in value.items():
            yield from object_paths(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from object_paths(item, path + (index,))


def at_path(value, path):
    for key in path:
        value = value[int(key) if isinstance(value, list) else key]
    return value


def schema_nodes(value, path=()):
    """Visit actual schema keywords, not example data or annotations."""
    if not isinstance(value, dict):
        return
    yield path, value
    for keyword in ("properties", "$defs", "dependentSchemas", "patternProperties"):
        for key, child in value.get(keyword, {}).items():
            yield from schema_nodes(child, path + (keyword, key))
    for keyword in ("items", "contains", "not", "if", "then", "else", "additionalProperties"):
        if isinstance(value.get(keyword), dict):
            yield from schema_nodes(value[keyword], path + (keyword,))
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for index, child in enumerate(value.get(keyword, [])):
            yield from schema_nodes(child, path + (keyword, index))


def missing_codes(codes, expected):
    return set(expected) - set(codes)


class SchemaFreezeTests(unittest.TestCase):
    def assert_invalid(self, identifier, payload):
        self.assertFalse(validator(identifier).is_valid(payload), (identifier, payload))

    def test_all_schemas_are_valid_draft_2020_12(self):
        for identifier, schema in SCHEMAS.items():
            with self.subTest(schema=identifier):
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                Draft202012Validator.check_schema(schema)

    def test_ids_unique_and_manifest_complete(self):
        files = list(ROOT.glob("*.schema.json"))
        ids = [load(p)["$id"] for p in files]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(files), len(SCHEMAS))
        self.assertEqual(set(SCHEMAS), {entry["id"] for entry in INVENTORY["schemas"]})
        for entry in INVENTORY["schemas"]:
            with self.subTest(schema=entry["id"]):
                self.assertEqual(entry["file"], entry["id"].replace("/", "_") + ".schema.json")
                self.assertTrue(entry["owners"])
                self.assertIn(entry["file"], (ROOT / "SCHEMAS.md").read_text())

    def test_every_schema_has_a_valid_example(self):
        for identifier in SCHEMAS:
            with self.subTest(schema=identifier):
                path = EXAMPLES / (identifier.replace("/", "_") + ".valid.json")
                self.assertTrue(path.is_file(), "SCHEMA_GAP: positive example absent: " + identifier)
                validator(identifier).validate(load(path))

    def test_invalid_examples_are_rejected(self):
        for identifier in SCHEMAS:
            with self.subTest(schema=identifier):
                self.assert_invalid(identifier, fixture(identifier, "invalid"))

    def test_every_declared_object_is_closed(self):
        upstream_agent_maps = {
            ("$defs", "agent", "properties", "rule_overrides"): {"enum": ["add", "remove"]},
            ("$defs", "agent", "properties", "promoted_seat"): None,
        }
        for identifier, schema in SCHEMAS.items():
            for path, node in schema_nodes(schema):
                if node.get("type") == "object":
                    with self.subTest(schema=identifier, path=path):
                        expected = upstream_agent_maps.get(path, False) if identifier == "lineage.event/v1" else False
                        self.assertEqual(node.get("additionalProperties"), expected)

    def test_unknown_keys_rejected_at_every_example_object(self):
        for identifier in SCHEMAS:
            path = EXAMPLES / (identifier.replace("/", "_") + ".valid.json")
            original = load(path)
            for nested in object_paths(original):
                payload = copy.deepcopy(original)
                at_path(payload, nested)["__unknown__"] = "must fail"
                with self.subTest(schema=identifier, path=nested):
                    self.assert_invalid(identifier, payload)

    def test_references_are_local_and_resolvable(self):
        for identifier, schema in SCHEMAS.items():
            for path, node in schema_nodes(schema):
                if "$ref" in node:
                    with self.subTest(schema=identifier, path=path):
                        self.assertTrue(node["$ref"].startswith("urn:"))
                        base, _, fragment = node["$ref"].partition("#")
                        self.assertIn(base, STORE)
                        if fragment:
                            at_path(STORE[base], fragment.lstrip("/").split("/"))

    def test_control_context_is_split_and_wrapper_is_refused(self):
        request = fixture("vp.control.req/v1")
        self.assert_invalid("vp.control.req/v1", {"request": request})
        flattened = copy.deepcopy(request)
        flattened["args"].update(flattened.pop("context"))
        self.assert_invalid("vp.control.req/v1", flattened)
        nested = copy.deepcopy(request)
        nested["args"]["context"] = nested["context"]
        self.assert_invalid("vp.control.req/v1", nested)

    def test_control_unknown_op_and_missing_explicit_dry_run_refused(self):
        request = fixture("vp.control.req/v1")
        request["op"] = "vp_unknown"
        self.assert_invalid("vp.control.req/v1", request)
        request = fixture("vp.control.req/v1")
        del request["dry_run"]
        self.assert_invalid("vp.control.req/v1", request)

    def test_lane_aliases_carry_identical_wire_objects(self):
        for lane, canonical in [("vp.oflane.req/v1", "vp.control.req/v1"),
                                ("vp.oflane.reply/v1", "vp.control.reply/v1")]:
            with self.subTest(lane=lane):
                self.assertEqual(SCHEMAS[lane]["$ref"], SCHEMAS[canonical]["$id"])
                payload = fixture(canonical)
                validator(lane).validate(payload)
                self.assert_invalid(lane, {"request": payload})
                self.assert_invalid(lane, {**payload, "status": "accepted"})

    def test_no_schema_supplies_a_default(self):
        for identifier, schema in SCHEMAS.items():
            for path, node in schema_nodes(schema):
                with self.subTest(schema=identifier, path=path):
                    self.assertNotIn("default", node)

    def test_capability_tool_and_json_op_are_equal(self):
        payload = fixture("vp.capability/v1")
        payload["json_op"] = "vp_window_dismiss"
        self.assert_invalid("vp.capability/v1", payload)
        self.assertEqual(len(INVENTORY["tools"]), 53)
        self.assertEqual(len([name for name in INVENTORY["tools"] if name.startswith("mv_")]), 4)

    def test_adapter_selectors_freeze_concrete_ops_only(self):
        for identifier, schema in SCHEMAS.items():
            if not identifier.endswith("_call.args/v1"):
                continue
            choices = schema["properties"]["op"]["enum"]
            self.assertEqual(len(choices), len(set(choices)))
            for op in choices:
                with self.subTest(schema=identifier, op=op):
                    self.assertNotIn("*", op)
                    args = PAIRS[identifier.split(".args/")[0]][op]["args"]
                    validator(identifier).validate(args)
            for op in ("arbitrary", "layout_*", "storage_*", "screen_undefined"):
                self.assert_invalid(identifier, {"op": op})
        self.assertIn("pin", SCHEMAS["vp_capsule_call.args/v1"]["properties"]["op"]["enum"])
        self.assertIn("unsupported", (ROOT / "SCHEMA_GAPS.md").read_text())

    def test_adapter_policy_preserves_operator_and_unsupported_restrictions(self):
        policy = load(ROOT / "adapter_ops.json")
        selectors = {name.split(".args/")[0]: schema
                     for name, schema in SCHEMAS.items() if name.endswith("_call.args/v1")}
        self.assertEqual(set(selectors), set(policy))
        for tool, schema in selectors.items():
            self.assertEqual(set(schema["properties"]["op"]["enum"]), set(policy[tool]["ops"]))
        for op in ("pin", "layout_save", "layout_load"):
            self.assertFalse(policy["vp_capsule_call"]["ops"][op]["supported"])
        for tool, op in (("vp_capsule_call", "role_switch"), ("vp_saved_data_call", "delete"),
                         ("vp_terminal_call", "cmd"), ("vp_handoff_call", "network_open"),
                         ("vp_handoff_call", "network_allowlist")):
            self.assertTrue(policy[tool]["ops"][op]["operator_only"])

    def test_selected_schema_gaps_closed(self):
        gaps = load(ROOT / "schema_gaps.json")
        self.assertEqual(gaps["status"], "FROZEN_STRUCTURAL")
        self.assertEqual(gaps["gaps"], [])
        self.assertEqual(gaps["acceptance_gate"], "test_every_schema_has_a_valid_example")

    def test_profile_ids_are_not_paths_and_hashes_have_exact_size(self):
        for profile in ("/synthetic/profile", "../synthetic", "synthetic/profile"):
            payload = fixture("vp.session/v1")
            payload["profile_id"] = profile
            self.assert_invalid("vp.session/v1", payload)
        for digest in ("a" * 64 + "\n", "a" * 63, "A" * 64):
            payload = fixture("vp.channel.reply/v1")
            payload["value_sha256"] = digest
            self.assert_invalid("vp.channel.reply/v1", payload)

    def test_live_session_needs_full_binding_pace_and_risk(self):
        for field in ("pace", "risk_ceiling", "window", "incarnation"):
            payload = fixture("vp.session/v1")
            del payload[field]
            self.assert_invalid("vp.session/v1", payload)
        payload = fixture("vp.session/v1")
        del payload["window"]["witness"]
        self.assert_invalid("vp.session/v1", payload)
        payload = fixture("vp.session/v1")
        payload["cdp_port"] = 0
        self.assert_invalid("vp.session/v1", payload)
        payload = fixture("vp.session/v1")
        payload["browser"] = "firefox"
        self.assert_invalid("vp.session/v1", payload)
        payload = fixture("vp.session/v1")
        payload["cdp_allowed"] = False
        self.assert_invalid("vp.session/v1", payload)

    def test_human_session_has_no_holder_and_broker_only_records(self):
        payload = fixture("vp.session/v1")
        payload["state"] = "HUMAN"
        self.assert_invalid("vp.session/v1", payload)
        payload["lease"]["holder"] = None
        validator("vp.session/v1").validate(payload)
        human = fixture("vp.human_lease/v1")
        human["by"] = "agent"
        self.assert_invalid("vp.human_lease/v1", human)

    def test_channel_raw_input_is_ephemeral_not_a_reply_or_timeline_field(self):
        request = fixture("vp.channel.req/v1")
        request.update(op="act", action="type", input_text="synthetic test input")
        validator("vp.channel.req/v1").validate(request)
        request["slot_id"] = "slot-fixture"
        self.assert_invalid("vp.channel.req/v1", request)
        for field in ("input_text", "text", "profile_path", "frame_bytes"):
            response = fixture("vp.channel.reply/v1")
            response[field] = "synthetic"
            self.assert_invalid("vp.channel.reply/v1", response)
            timeline = fixture("rec.timeline/v1")
            timeline["events"][0][field] = "synthetic"
            self.assert_invalid("rec.timeline/v1", timeline)

    def test_timeline_requires_polling_tag_and_redaction_pairs(self):
        payload = fixture("rec.timeline/v1")
        del payload["events"][0]["atspi_capture"]
        self.assert_invalid("rec.timeline/v1", payload)
        payload = fixture("rec.timeline/v1")
        del payload["events"][0]["text_sha256"]
        self.assert_invalid("rec.timeline/v1", payload)
        gap = {"mono_ns": 11, "capsule_id": "c-fixture", "incarnation": "i-fixture",
               "source": "human", "gap": True}
        validator("rec.timeline/v1").validate({"schema": "rec.timeline/v1", "events": [gap]})
        gap["action"] = "click"
        self.assert_invalid("rec.timeline/v1", {"schema": "rec.timeline/v1", "events": [gap]})

    def test_frame_needs_witness_and_ax_is_not_raster(self):
        frame = fixture("vp.frame/v1")
        del frame["witness"]
        self.assert_invalid("vp.frame/v1", frame)
        frame = fixture("vp.frame/v1")
        frame["frame_kind"] = "ax_only"
        self.assert_invalid("vp.frame/v1", frame)
        frame = fixture("vp.frame/v1")
        frame["width"] = 0
        self.assert_invalid("vp.frame/v1", frame)

    def test_route_match_is_cited_and_has_no_missing_arguments(self):
        for missing in ("sha256", "line_start", "path"):
            payload = fixture("vp.tool_route.reply/v1")
            del payload["candidates"][0]["citations"][0][missing]
            self.assert_invalid("vp.tool_route.reply/v1", payload)
        payload = fixture("vp.tool_route.reply/v1")
        payload["candidates"][0]["missing_args"] = ["session_id"]
        self.assert_invalid("vp.tool_route.reply/v1", payload)
        payload = fixture("vp.tool_route.reply/v1")
        payload["candidates"][0]["mcp_call"]["arguments"]["request"] = {}
        self.assert_invalid("vp.tool_route.reply/v1", payload)
        payload = fixture("vp.tool_route.reply/v1")
        payload["candidates"][0]["json"]["op"] = "vp_window_dismiss"
        self.assert_invalid("vp.tool_route.reply/v1", payload)

    def test_route_abstention_reasons_remain_distinct(self):
        for reason in ("policy_denied", "ambiguous", "missing_arguments", "stale_index", "absent_capability"):
            payload = {"verdict": "ABSTAIN", "code": "ROUTE_ABSTAIN", "reason": reason,
                       "index_sha256": "a" * 64, "candidates": []}
            validator("vp.tool_route.reply/v1").validate(payload)
            del payload["reason"]
            self.assert_invalid("vp.tool_route.reply/v1", payload)

    def test_forge_shell_and_python_bodies_are_refused(self):
        for key in ("shell", "python", "body"):
            payload = fixture("vp.forge_tool/v1")
            payload[key] = "synthetic body"
            self.assert_invalid("vp.forge_tool/v1", payload)

    def test_incomplete_forge_receipt_fails_closed(self):
        for value in (None, {}, {"schema": "vp.forge_receipt/v1"},
                      {"schema": "vp.forge_receipt/v1", "state": "admitted"}):
            self.assert_invalid("vp.forge_receipt/v1", value)
        row = next(r for r in INVENTORY["schemas"] if r["id"] == "vp.forge_receipt/v1")
        self.assertEqual(row["status"], "FROZEN_STRUCTURAL")

    def test_strategy_error_code_coverage_and_sources(self):
        codes = load(ROOT / "error_codes.json")
        for document, source in INVENTORY["sources"].items():
            self.assertFalse(missing_codes(codes, source["error_codes"]))
            unknown = set(source["uppercase_identifiers"]) - set(codes) - set(INVENTORY["non_error_identifiers"])
            self.assertFalse(unknown)
            # The pinned inventory is portable; when the supplied source is
            # present, also check its bytes and classify every current token.
            path = CAMPAIGN / document
            if path.is_file():
                text = path.read_text()
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source["sha256"])
                current = set(re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text))
                self.assertEqual(current, set(source["uppercase_identifiers"]))
                for code in source["error_codes"]:
                    self.assertRegex(text, re.escape(code))
        for code, detail in codes.items():
            with self.subTest(code=code):
                self.assertTrue(detail["meaning"])
                self.assertNotIn("\n", detail["meaning"])
                self.assertTrue(detail["source"])
                for source in detail["source"]:
                    self.assertIn(source["document"], INVENTORY["sources"])
                    self.assertGreater(source["line"], 0)
                    self.assertTrue(source["section"])
        self.assertTrue({"OWNER", "identity_mismatch", "host_window_unavailable", "-32601", "-32602"} <= codes.keys())

    def test_wl0_cited_statuses_not_run_and_history_are_preserved(self):
        config = load(ROOT.parent / "config" / "wl0.json")
        self.assertEqual(set(config["steps"]), {"L0-" + str(n) for n in range(1, 9)})
        self.assertTrue(all(step["status"] == "PASS" for step in config["steps"].values()))
        oauth = config["steps"]["L0-7"]["oauth_browser_wrapper"]
        self.assertEqual(oauth["status"], "NOT RUN")
        self.assertIsNone(oauth["passed"])
        self.assertEqual(config["history"]["atspi_initial_status"], "BLOCKED")
        self.assertEqual(config["history"]["initial_codex_probe_status"], "PENDING")
        self.assertEqual(config["history"]["final_codex_cdp_status"], "PASS")
        self.assertIsNone(config["end_state"]["pids"])
        self.assertEqual(config["source"]["claim_label"], "CITED")
        path = CAMPAIGN / "packs" / "wl0_receipt.md"
        if path.is_file():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), config["source"]["sha256"])

    def test_wl0_backend_and_focus_safety_rules(self):
        c = load(ROOT.parent / "config" / "wl0.json")
        self.assertEqual(c["focus_rules"]["create_target"]["required_args"], {"background": True})
        self.assertIn("PUT /json/new", c["focus_rules"]["create_target"]["forbidden_on_host"])
        self.assertTrue(c["focus_rules"]["launch"]["launch_then_dismiss_one_step"])
        self.assertTrue(c["focus_rules"]["human_return"]["restore_saved_operator_active_xid_at_return"])
        self.assertFalse(c["focus_rules"]["move"]["activate_window"])
        self.assertFalse(c["focus_rules"]["move"]["xdotool_allowed_on_host"])
        cdp = c["backend_rules"]["cdp"]
        self.assertEqual(cdp["bind_address"], "127.0.0.1")
        self.assertEqual(len(cdp["browser_allowed_flags"]), 6)
        self.assertFalse(any("user-data-dir" in flag for flag in cdp["app_allowed_flags"]))
        self.assertFalse(cdp["read_profile_files"])
        for app in c["apps"].values():
            self.assertTrue(app["cdp"]["available"])
        for app in ("Antigravity 2", "Codex Desktop"):
            self.assertTrue(c["apps"][app]["cdp"]["children_inherit_socket"])
        self.assertEqual(c["apps"]["Antigravity 2"]["atspi"]["status"], "SHALLOW")
        self.assertTrue(c["backend_rules"]["atspi"]["persistent_connection_required"])
        self.assertFalse(c["backend_rules"]["atspi"]["persistent_connection_measured"])
        self.assertEqual(c["backend_rules"]["atspi"]["chrome_walk_latency_s"], 7.4)

    def test_frozen_files_keep_initial_sha256(self):
        expected = {
            "agents/metaviewer_agents/mcp_server.py": "a31d4381c53be7b2417b0acf46ad42008ed2710ac58c0929a1748c392357d862",
            "contracts/v0.2/mv_tool_call.schema.json": "7da7513b1b457dfeb08689c0ae70b2b950a2e093dab8706d99267acbd868cd5f",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((REPO / name).read_bytes()).hexdigest(), digest)


class MutantTests(unittest.TestCase):
    def discriminates(self, identifier, payload, mutant):
        self.assertFalse(validator(identifier).is_valid(payload), "Nominal gate accepted negative control")
        self.assertTrue(validator(identifier, mutant).is_valid(payload), "Mutant did not remove the guarded behavior")

    def test_mutant_drop_object_closure(self):
        i = "vp.window.reply/v1"
        payload = fixture(i)
        payload["unexpected"] = True
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["additionalProperties"] = True
        self.discriminates(i, payload, mutant)

    def test_mutant_allow_flattened_context(self):
        i = "vp.control.req/v1"
        payload = fixture(i)
        context = payload.pop("context")
        payload.update(context)
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["required"].remove("context")
        mutant["properties"].update(mutant["properties"].pop("context")["properties"])
        self.discriminates(i, payload, mutant)

    def test_mutant_admit_unknown_operation(self):
        i = "vp.control.req/v1"
        payload = fixture(i)
        payload["op"] = "vp_unknown"
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["properties"]["op"] = {"type": "string"}
        self.discriminates(i, payload, mutant)

    def test_mutant_let_agent_write_human_control(self):
        i = "vp.human_lease/v1"
        payload = fixture(i)
        payload["by"] = "agent"
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["properties"]["by"] = {"type": "string"}
        self.discriminates(i, payload, mutant)

    def test_mutant_drop_frame_witness(self):
        i = "vp.frame/v1"
        payload = fixture(i)
        del payload["witness"]
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["required"].remove("witness")
        self.discriminates(i, payload, mutant)

    def test_mutant_drop_polling_only_requirement(self):
        i = "rec.timeline/v1"
        payload = fixture(i)
        del payload["events"][0]["atspi_capture"]
        mutant = copy.deepcopy(SCHEMAS[i])
        mutant["properties"]["events"]["items"]["allOf"][0]["then"]["required"] = []
        self.discriminates(i, payload, mutant)

    def test_mutant_drop_error_code(self):
        codes = load(ROOT / "error_codes.json")
        required = set(INVENTORY["sources"]["STRATEGY_FABLE2.md"]["error_codes"])
        self.assertFalse(missing_codes(codes, required))
        del codes["NO_LEASE"]
        self.assertEqual(missing_codes(codes, required), {"NO_LEASE"})


if __name__ == "__main__":
    unittest.main()
