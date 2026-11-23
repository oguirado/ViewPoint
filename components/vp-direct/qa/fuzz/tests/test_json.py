"""Deterministic request cases derived from the frozen ViewPoint JSON contracts."""

from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

from json_fixtures import (
    EXAMPLES, SCHEMAS, VP_DIRECT, WINDOW, Harness, control, encode, expected_digest, mcp,
    read_json, rpc,
)
from expose import cli
from oflane import MailboxError


LANES = ("c1", "mcp", "cli", "mailbox")
REQUEST_SCHEMA = read_json(SCHEMAS / "vp.control.req_v1.schema.json")
WINDOW_ARGS = next(rule["then"]["properties"]["args"] for rule in REQUEST_SCHEMA["allOf"]
                   if rule["if"]["properties"]["op"]["const"] == WINDOW["op"])
DATA_TOOL = "vp_data_call"
DATA_ARGS = read_json(SCHEMAS / "vp_data_call.args_v1.schema.json")["$defs"]["resolve.args"]
DATA_FIXTURE = read_json(EXAMPLES / "adapter_pairs.json")[DATA_TOOL]["resolve"]["args"]
CATALOG_DIGEST = "0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21"
SCHEMA_PINS = {
    "vp.control.req_v1.schema.json": "873dce0db2805ab9ecf52980f34125842c9dbfc6f766a879ebb362c094a1ab26",
    "vp.control.reply_v1.schema.json": "a7d67c491db200e073dac1d6efc78b218fcdd46d983593a8f8207342eb3e5d91",
    "vp.oflane.req_v1.schema.json": "da1d0ce47f0a8077801241bb8aef1b287b872e96c7f65966b520e3f13867551f",
    "vp.oflane.reply_v1.schema.json": "7dea3ae04322359b23341bd2d1de0d2858b52a4a916d60d93db7f412cf590bbd",
    "vp_data_call.args_v1.schema.json": "019ff4bf60795621b04a6472ccbc82969de3d88df351738b598d4cfe6b15d4ef",
    "adapter_ops.json": "2fbd2997376f1fabde7d47a05b7fe7ca1126a8e9c047cd4f28eeca622dc6f92b",
}
MISSING = object()


def window_cases():
    context = REQUEST_SCHEMA["properties"]["context"]
    selections = (
        ((), "request_id", REQUEST_SCHEMA),
        ((), "dry_run", REQUEST_SCHEMA),
        (("args",), "session_id", WINDOW_ARGS),
        (("context",), "authority_epoch", context),
    )
    wrong_type = {"string": 7, "boolean": "false", "integer": True}
    for parents, name, schema in selections:
        field = schema["properties"][name]
        values = {"absent": MISSING, "null": None, "wrong-type": wrong_type[field["type"]]}
        if "minLength" in field:
            values["below-minimum"] = "x" * (field["minLength"] - 1)
        if "minimum" in field:
            values["below-minimum"] = field["minimum"] - 1
        for variant, value in values.items():
            case = deepcopy(WINDOW)
            if parents == ("args",):
                # MCP accepts a single session_id parameter for this tool.
                del case["context"]["session_id"]
            target = case[parents[0]] if parents else case
            if value is MISSING:
                del target[name]
            else:
                target[name] = value
            valid = variant == "absent" and name not in schema.get("required", [])
            yield ".".join((*parents, name, variant)), case, valid


def reversed_fields(value):
    if isinstance(value, dict):
        return {name: reversed_fields(item) for name, item in reversed(value.items())}
    return value


class JSONContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(dir=Path(__file__).parent, prefix=".qa-json-")))

    def assert_no_dispatch(self, harness):
        self.assertEqual(harness.gates, [], "policy gate was entered")
        self.assertEqual(harness.previews, [], "preview was entered")
        self.assertEqual(harness.sent(), b"", "fixture send occurred")
        self.assertEqual(harness.claims(), [], "C1 wrote a durable request claim")
        self.assertEqual(harness.mailbox_state(), [], "C4 wrote an execution or completion record")

    def assert_digest(self, reply, request):
        control.validate_contract(control.REPLY, reply)
        self.assertEqual(reply["effective_request_sha256"], expected_digest(request),
                         "effective request digest mismatch")

    def assert_invalid(self, case, harness, *, c1_code=-32602, mcp_code=-32602, verb="window-summon"):
        for contract in (control.REQUEST, "vp.oflane.req/v1"):
            with self.assertRaises(ValidationError):
                control.validate_contract(contract, case)
        with self.assertRaises(control.InvalidRequest) as invalid:
            harness.call_c1(case)
        self.assertEqual(invalid.exception.code, c1_code)
        response = harness.call_mcp(encode(rpc(case)))
        if isinstance(mcp_code, int):
            self.assertEqual(response["error"]["code"], mcp_code)
        else:
            result = response["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"]), {"code": mcp_code})
        status, stdout, stderr = harness.call_cli(encode(case), verb=verb)
        self.assertEqual((status, stdout, json.loads(stderr)["code"]),
                         (2 if isinstance(c1_code, int) else 3, "", c1_code))
        with self.assertRaises(ValidationError):
            harness.mailbox.submit(case)
        self.assertEqual(harness.mailbox.pending(), ())
        with self.assertRaises(ValidationError):
            harness.deliver_file("raw-invalid", encode(case))
        self.assert_no_dispatch(harness)

    def assert_parity(self, case, label):
        replies = []
        for lane in LANES:
            harness = Harness(self.root / f"{label}-{lane}")
            reply = harness.call(lane, case)
            self.assert_digest(reply, case)
            self.assertEqual(harness.previews, [case])
            self.assertEqual(harness.sent(), b"")
            replies.append(reply)
        self.assertTrue(all(reply == replies[0] and reply["code"] == "OK" for reply in replies))

    def assert_uncertain(self, harness, reply, request):
        self.assert_digest(reply, request)
        self.assertEqual((reply["ok"], reply["code"]), (False, "UNCERTAIN_OUTCOME"))
        self.assertEqual(harness.sent(), encode(request, sort_keys=True), "fixture send count or bytes changed")
        self.assertFalse((harness.root / (request["request_id"] + ".done")).exists())

    def test_frozen_schema_and_registry_pins(self):
        for name, digest in SCHEMA_PINS.items():
            with self.subTest(schema=name):
                self.assertEqual(sha256((SCHEMAS / name).read_bytes()).hexdigest(), digest)
        registry = control.CapabilityRegistry()
        self.assertEqual(expected_digest(registry.entries()), CATALOG_DIGEST)
        for module in (mcp, cli):
            name = module.__name__.rsplit(".", 1)[-1]
            with self.subTest(lane=name):
                projection = module.registration_manifest(registry)
                self.assertEqual(projection["registry_sha256"], CATALOG_DIGEST)
                path = VP_DIRECT / "expose" / name / "registration_manifest.json"
                self.assertEqual(path.read_bytes(), encode(projection, sort_keys=True))

    def test_mcp_tools_list_equals_fixture_registry_projection(self):
        harness = Harness(self.root / "mcp")
        response = harness.call_mcp(encode({"jsonrpc": "2.0", "id": "list", "method": "tools/list"}))
        tools = response["result"]["tools"]
        self.assertEqual([row["name"] for row in tools],
                         [entry["tool"] for entry in harness.dispatcher.registry.entries()])
        self.assert_no_dispatch(harness)

    def test_frozen_valid_request_matches_across_public_entry_points(self):
        control.validate_contract(control.REQUEST, WINDOW)
        self.assert_parity(WINDOW, "frozen")
        self.assertEqual(expected_digest(WINDOW),
                         "08173806f4285bfa82218f2e80747460ed7df2b72060aa0700d5be6f5615ad7d")

    def test_schema_field_mutations_refuse_before_dispatch_or_claim(self):
        for index, (name, case, valid) in enumerate(window_cases()):
            with self.subTest(case=name):
                if valid:
                    control.validate_contract(control.REQUEST, case)
                    self.assert_parity(case, f"case-{index}")
                else:
                    self.assert_invalid(case, Harness(self.root / f"case-{index}"))

    def test_unknown_tool_and_adapter_selector_preserve_lane_refusals(self):
        unknown_tool = dict(WINDOW, op="vp_unknown_fixture", dry_run=False)
        self.assert_invalid(unknown_tool, Harness(self.root / "tool"), c1_code=-32601)
        unknown_selector = dict(WINDOW, op=DATA_TOOL, args=dict(DATA_FIXTURE, op="unknown-fixture"),
                                dry_run=False)
        self.assert_invalid(unknown_selector, Harness(self.root / "selector"),
                            c1_code="ARGS_SCHEMA_ABSENT", mcp_code="ARGS_SCHEMA_ABSENT", verb="data")

    def test_frozen_adapter_sha256_length_boundaries(self):
        bounds = DATA_ARGS["properties"]["sha256"]
        lengths = (bounds["minLength"] - 1, bounds["maxLength"], bounds["maxLength"] + 1)
        for length in lengths:
            with self.subTest(length=length):
                args = dict(DATA_FIXTURE, sha256="a" * length)
                case = dict(WINDOW, op=DATA_TOOL, args=args)
                if length != bounds["maxLength"]:
                    self.assert_invalid(case, Harness(self.root / f"hash-{length}"), verb="data")
                    continue
                control.validate_contract("vp_data_call.resolve.args/v1", args)
                for lane in LANES:
                    harness = Harness(self.root / f"parked-{lane}")
                    reply = harness.call(lane, case)
                    self.assert_digest(reply, case)
                    self.assertEqual((reply["ok"], reply["code"]), (False, "HOLD"))
                    self.assertEqual(harness.gates + harness.previews, [])
                    self.assertEqual(harness.sent(), b"")

    def test_reordering_and_spacing_preserve_effective_digest(self):
        reordered = reversed_fields(WINDOW)
        mcp_harness = Harness(self.root / "mcp")
        mcp_wire = encode(rpc(reordered))
        mcp_reply = mcp_harness.call_mcp(mcp_wire)
        self.assertFalse(mcp_reply["result"]["isError"])
        file_wire = (json.dumps(reordered, ensure_ascii=False, indent=2) + "\n").encode()
        self.assertNotEqual(file_wire, encode(WINDOW, sort_keys=True))
        cli_harness = Harness(self.root / "cli")
        rc, stdout, stderr = cli_harness.call_cli(file_wire)
        self.assertEqual((rc, stderr), (0, ""))
        mailbox_harness = Harness(self.root / "mailbox")
        mailbox_reply = mailbox_harness.deliver_file(WINDOW["request_id"], file_wire)
        self.assertEqual(mailbox_harness.call_mailbox(WINDOW), mailbox_reply)
        for reply in (json.loads(mcp_reply["result"]["content"][0]["text"]),
                      json.loads(stdout), mailbox_reply):
            self.assert_digest(reply, WINDOW)
            self.assertEqual(reply, mailbox_reply)
        for harness in (mcp_harness, cli_harness, mailbox_harness):
            self.assertEqual(harness.previews, [WINDOW])
            self.assertEqual(harness.sent(), b"")

    def assert_mcp_parse_refusal(self, harness, wire):
        response = harness.call_mcp(wire)
        self.assertEqual(response.get("error", {}).get("code"), -32700, "MCP parser accepted invalid bytes")
        self.assertIsNone(response["id"])
        self.assert_no_dispatch(harness)

    def assert_cli_parse_refusal(self, harness, wire):
        status, stdout, stderr = harness.call_cli(wire)
        self.assertEqual((status, stdout, json.loads(stderr)["code"]), (2, "", "input_error"))
        self.assert_no_dispatch(harness)

    def test_raw_json_syntax_refusals_do_not_enter_dispatch(self):
        request = dict(WINDOW, dry_run=False)
        source = encode(request)
        message = encode(rpc(request))
        cases = (
            ("syntax", b'{"x":}\n', b'{"x":}\n', json.JSONDecodeError, None),
            ("truncated", source[:-2] + b"\n", message[:-2] + b"\n", json.JSONDecodeError, None),
            ("duplicate-top", source.replace(b'{', b'{"dry_run":true,', 1),
             b'{"id":"alternate",' + message[1:], MailboxError, "DUPLICATE_JSON_KEY"),
            ("duplicate-nested", source.replace(b'"args":{', b'"args":{"session_id":"other",', 1),
             message.replace(b'"arguments":{', b'"arguments":{"session_id":"other",', 1),
             MailboxError, "DUPLICATE_JSON_KEY"),
            ("nonfinite-literal", source.replace(b'"authority_epoch":1', b'"authority_epoch":NaN'),
             message.replace(b'"authority_epoch":1', b'"authority_epoch":NaN'),
             MailboxError, "NONFINITE_JSON_NUMBER"),
        )
        for name, file_wire, mcp_wire, error, code in cases:
            with self.subTest(case=name):
                self.assertNotEqual(file_wire, source)
                self.assertNotEqual(mcp_wire, message)
                harness = Harness(self.root / name)
                self.assert_mcp_parse_refusal(harness, mcp_wire)
                self.assert_cli_parse_refusal(harness, file_wire)
                with self.assertRaises(error) as invalid:
                    harness.deliver_file(request["request_id"], file_wire)
                if code is not None:
                    self.assertEqual(str(invalid.exception), code)
                self.assert_no_dispatch(harness)

    def test_transport_byte_and_mailbox_filename_limits(self):
        def pad(raw, limit):
            self.assertLess(len(raw), limit)
            return raw[:-1] + b" " * (limit - len(raw)) + raw[-1:]

        mcp_harness = Harness(self.root / "mcp")
        wire = pad(encode(rpc(WINDOW)), mcp.MAX_LINE_BYTES)
        self.assert_mcp_parse_refusal(mcp_harness, b" " + wire)
        self.assert_mcp_parse_refusal(mcp_harness, encode(rpc(WINDOW)).rstrip(b"\n"))
        response = mcp_harness.call_mcp(wire)
        self.assert_digest(json.loads(response["result"]["content"][0]["text"]), WINDOW)

        cli_harness = Harness(self.root / "cli")
        wire = pad(encode(WINDOW), cli_harness.cli_binding.max_line_bytes)
        self.assert_cli_parse_refusal(cli_harness, b" " + wire)
        rc, stdout, stderr = cli_harness.call_cli(wire)
        self.assertEqual((rc, stderr), (0, ""))
        self.assert_digest(json.loads(stdout), WINDOW)

        file_harness = Harness(self.root / "mailbox")
        capacity = os.pathconf(file_harness.root, "PC_NAME_MAX") - len(".reply.json")
        with self.assertRaisesRegex(MailboxError, "^INVALID_REQUEST_ID$"):
            file_harness.mailbox.submit(dict(WINDOW, request_id="x" * (capacity + 1)))
        self.assertEqual(file_harness.mailbox.pending(), ())
        self.assert_no_dispatch(file_harness)
        exact = dict(WINDOW, request_id="x" * capacity)
        self.assert_digest(file_harness.call_mailbox(exact), exact)

    def test_invalid_terminal_readback_remains_uncertain_after_reopen(self):
        request = dict(WINDOW, dry_run=False)
        invalid = control.Outcome(True, "OK", result=["not-an-object"])
        for initial_lane in LANES:
            with self.subTest(initial_lane=initial_lane):
                root = self.root / initial_lane
                harness = Harness(root, outcome=invalid)
                self.assert_uncertain(harness, harness.call(initial_lane, request), request)
                restarted = Harness(root)
                for retry_lane in LANES:
                    self.assert_uncertain(restarted, restarted.call(retry_lane, request), request)
                self.assertEqual(restarted.gates, [])
                [(request_id, digest, saved)] = restarted.claims()
                self.assertEqual((request_id, digest), (request["request_id"], expected_digest(request)))
                self.assertEqual(json.loads(saved)["code"], "UNCERTAIN_OUTCOME")

    def test_mailbox_changed_readback_digest_fences_reopened_worker(self):
        request = dict(WINDOW, dry_run=False)
        harness = Harness(self.root / "mailbox")
        dispatch = harness.dispatcher.dispatch_mailbox

        def cross_request_reply(envelope, identity):
            reply = dispatch(envelope, identity)
            self.assertTrue(reply["ok"])
            return dict(reply, effective_request_sha256=expected_digest(WINDOW))

        with patch.object(harness.dispatcher, "dispatch_mailbox", side_effect=cross_request_reply) as fault:
            with self.assertRaisesRegex(MailboxError, "^REPLY_REQUEST_MISMATCH$"):
                harness.call_mailbox(request)
        fault.assert_called_once()
        self.assertEqual(harness.sent(), encode(request, sort_keys=True))
        self.assertFalse((harness.root / (request["request_id"] + ".reply.json")).exists())

        restarted = Harness(harness.root)
        self.assert_uncertain(restarted, restarted.call_mailbox(request), request)
        self.assertEqual(restarted.gates, [])

    def crash_after_send(self, lane):
        request = dict(WINDOW, dry_run=False)
        root = self.root / lane
        root.mkdir()
        (root / "crash-input.json").write_bytes(encode(request))
        script = Path(__file__).with_name("crash_worker.py")
        child = subprocess.run([sys.executable, "-I", "-B", str(script), str(root), lane],
                               capture_output=True, check=False, timeout=15)
        self.assertEqual((child.returncode, child.stderr), (73, b""))
        restarted = Harness(root)
        self.assertEqual(restarted.sent(), encode(request, sort_keys=True))
        self.assertEqual(restarted.claims(), [(request["request_id"], expected_digest(request), None)])
        self.assertFalse((root / (request["request_id"] + ".reply.json")).exists())
        return request, restarted

    def test_process_exit_after_send_retries_without_redispatch(self):
        for initial_lane in LANES:
            with self.subTest(initial_lane=initial_lane):
                request, restarted = self.crash_after_send(initial_lane)
                for retry_lane in LANES:
                    self.assert_uncertain(restarted, restarted.call(retry_lane, request), request)
                self.assertEqual(restarted.gates, [])
                self.assertEqual(restarted.claims(), [(request["request_id"], expected_digest(request), None)])

    def test_mutant_tolerating_duplicate_keys_fails_parse_oracle(self):
        request = dict(WINDOW, dry_run=False)
        message = encode(rpc(request))
        duplicate = message.replace(b'"arguments":{', b'"arguments":{"dry_run":true,', 1)
        self.assertNotEqual(duplicate, message)
        harness = Harness(self.root / "duplicate")
        self.assert_mcp_parse_refusal(harness, duplicate)
        with patch.object(mcp, "_unique_object", side_effect=dict) as mutant:
            with self.assertRaisesRegex(AssertionError, "MCP parser accepted invalid bytes"):
                self.assert_mcp_parse_refusal(harness, duplicate)
        self.assertGreater(mutant.call_count, 0)
        self.assertEqual(harness.sent(), encode(request, sort_keys=True))

    def test_mutant_changing_cli_default_fails_digest_oracle(self):
        harness = Harness(self.root / "cli")
        original = harness.dispatcher.dispatch_cli

        def force_execute(verb, request, identity):
            return original(verb, dict(request, dry_run=False), identity)

        with patch.object(harness.dispatcher, "dispatch_cli", side_effect=force_execute) as mutant:
            reply = harness.call("cli", WINDOW)
        mutant.assert_called_once()
        self.assertEqual(harness.sent(), encode(dict(WINDOW, dry_run=False), sort_keys=True))
        with self.assertRaisesRegex(AssertionError, "effective request digest mismatch"):
            self.assert_digest(reply, WINDOW)

    def test_mutant_forgetting_durable_claim_fails_uncertainty_oracle(self):
        request, restarted = self.crash_after_send("mcp")
        self.assert_uncertain(restarted, restarted.call("cli", request), request)
        mutant = Harness(restarted.root, database="blank-request-store.sqlite")
        reply = mutant.call("cli", request)
        self.assertEqual(mutant.sent(), encode(request, sort_keys=True) * 2)
        with self.assertRaisesRegex(AssertionError, "UNCERTAIN_OUTCOME"):
            self.assert_uncertain(mutant, reply, request)
