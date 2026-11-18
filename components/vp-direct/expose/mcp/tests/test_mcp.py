import ast
from copy import deepcopy
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator


SURFACE = Path(__file__).resolve().parents[1]
VP_DIRECT = SURFACE.parents[1]
REPO = VP_DIRECT.parents[1]
sys.path.insert(0, str(VP_DIRECT))

from control.core import control
from expose.mcp import MAX_LINE_BYTES, MCPServer, PROTOCOL_VERSION, serve
from expose.mcp.generate import MANIFEST, main as generate
from expose.mcp.tools import _portable, registration_manifest


FROZEN_MCP = "agents/metaviewer_agents/mcp_server.py"
FROZEN_CONTRACT = "contracts/v0.2/mv_tool_call.schema.json"
FROZEN_DIGESTS = {
    FROZEN_MCP: "a31d4381c53be7b2417b0acf46ad42008ed2710ac58c0929a1748c392357d862",
    FROZEN_CONTRACT: "7da7513b1b457dfeb08689c0ae70b2b950a2e093dab8706d99267acbd868cd5f",
}
FROZEN_NAMES = {"mv_observe", "mv_list", "mv_propose", "mv_wait_verify"}
V3_NAMES = {"vp_perception_call", "vp_guest_call", "vp_data_call", "vp_factory_call"}
AGENT = control.LaneIdentity("agent", "fixture-agent")
WINDOW = "vp_window_summon"


def rpc(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def exchange(server, *messages):
    stream = BytesIO(b"".join(control.canonical_bytes(message) for message in messages))
    output = BytesIO()
    serve(server, stream, output)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def arguments(**fields):
    return {"request_id": "mcp-fixture", "dry_run": True, **fields}


def allow(_request, _risk):
    return True


def read_frozen(path):
    return (REPO / path).read_bytes()


class MCPTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(TemporaryDirectory(dir=SURFACE / "tests", prefix=".mcp-"))
        self.store = control.RequestStore(Path(root) / "requests.sqlite")
        self.workspace = Path(root)
        self.calls = []
        self.previews = []
        self.policy_rows = control._validation._adapter_rows()

    def handler(self, request):
        self.calls.append(request)
        return control.Outcome(True, "OK")

    def preview(self, request):
        self.previews.append(request)
        return control.Outcome(True, "OK")

    def fixture_registry(self, names):
        source_path = "components/vp-direct/schemas/vp.control.req_v1.schema.json"
        source = read_frozen(source_path)
        entries = []
        for tool in names:
            if tool in self.policy_rows:
                continue
            entries.append({
                "schema": control.CAPABILITY, "tool": tool, "json_op": tool,
                "cli": tool.replace("_", "-"), "origin": "new",
                "args_schema_sha256": control.digest(control._args_by_tool[tool]),
                "ops": [], "risk_class": "preparar",
                "operator_only": tool in {"vp_forge_admit", "vp_identity_delete"},
                "supported": True, "route": {"verbs": [], "objects": [], "argv_template": tool},
                "source": [{"path": source_path, "line_start": 1, "line_end": len(source.splitlines()),
                            "sha256": sha256(source).hexdigest()}],
            })
        return control.CapabilityRegistry(entries)

    def dispatcher(self, registry, *, lease=allow, bindings=()):
        return control.Dispatcher(
            registry, self.store, pace_risk=allow, lease_epoch=lease,
            tools={WINDOW: control.Handler(self.handler, self.preview)}, bindings=bindings,
        )

    def assert_registry_listing(self, registry, server):
        # Reuse the request and schema-shape assertions in
        # hackathon/mcp/vp_mcp/tests/test_vp_mcp.py::TestVpMcp.test_tools_list_schema.
        [response] = exchange(server, rpc(1, "tools/list", {}))
        tools = response["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], [row["tool"] for row in registry.entries()])
        for tool in tools:
            shape = tool["inputSchema"]
            self.assertEqual(shape["type"], "object")
            self.assertIn("properties", shape)
            self.assertIn("required", shape)
            Draft202012Validator.check_schema(shape)
        return tools

    def assert_frozen(self, read):
        for path, digest in FROZEN_DIGESTS.items():
            self.assertEqual(sha256(read(path)).hexdigest(), digest, path)
        module = ast.parse(read(FROZEN_MCP))
        declaration = next(statement for statement in module.body
                           if isinstance(statement, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "TOOL_NAMES"
                                   for target in statement.targets))
        names = ast.literal_eval(declaration.value)
        self.assertCountEqual(names, FROZEN_NAMES)

    def test_tools_list_matches_every_registered_tool_with_closed_flat_schemas(self):
        inventory = control._request_schema["properties"]["op"]["enum"]
        registry = self.fixture_registry(inventory)
        server = MCPServer(self.dispatcher(registry), AGENT)
        tools = self.assert_registry_listing(registry, server)
        self.assertCountEqual([row["name"] for row in tools], inventory)
        self.assertCountEqual([row["name"] for row in tools if row["name"].startswith("mv_")], FROZEN_NAMES)
        self.assertLessEqual(V3_NAMES, {row["name"] for row in tools})
        self.assertEqual(registration_manifest(registry), {
            "registry_sha256": control.digest(registry.entries()), "tools": tools,
        })
        for tool in tools:
            props = tool["inputSchema"]["properties"]
            self.assertTrue({"caller", "agent_instance", "request", "context"}.isdisjoint(props))
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.assertEqual(set(schemas[WINDOW]["required"]), {"request_id", "dry_run", "session_id"})
        validate = Draft202012Validator(schemas["vp_perception_call"]).is_valid
        self.assertTrue(validate(arguments(op="sleep", session_id="fixture-session", reason="rest")))
        self.assertFalse(validate(arguments(op="sleep", session_id="fixture-session", surface_id="surface", reason="rest")))
        self.assertFalse(validate(arguments(op="sleep", session_id="fixture-session", reason="rest", goal_id="wrong-op-field")))

    def test_catalog_manifest_is_byte_stable_and_pins_registry(self):
        registry = control.CapabilityRegistry()
        manifest = registration_manifest(registry)
        self.assertEqual(MANIFEST.read_bytes(), control.canonical_bytes(manifest))
        self.assertEqual(generate(["--catalog-only", "--check"]), 0)
        self.assertEqual(manifest["registry_sha256"], control.digest(registry.entries()))
        self.assert_registry_listing(registry, MCPServer(self.dispatcher(registry), AGENT))

    def test_generator_accepts_only_an_explicit_snapshot_and_checks_staleness(self):
        registry = self.fixture_registry({WINDOW})
        snapshot = self.workspace / "registry.json"
        snapshot.write_bytes(control.canonical_bytes(registry.entries()))
        artifact = self.workspace / "manifest.json"
        with patch("expose.mcp.generate.MANIFEST", artifact):
            self.assertEqual(generate(["--registry", str(snapshot)]), 0)
            self.assertEqual(artifact.read_bytes(), control.canonical_bytes(registration_manifest(registry)))
            self.assertEqual(generate(["--registry", str(snapshot), "--check"]), 0)
            with patch("sys.stderr"):
                self.assertEqual(generate(["--catalog-only", "--check"]), 1)

    def assert_publication_lock(self):
        artifact = self.workspace / "manifest.json"
        prior = b"previous manifest\n"
        artifact.write_bytes(prior)
        expected = control.canonical_bytes(registration_manifest(control.CapabilityRegistry()))
        replace = os.replace

        def replace_while_locked(staged, destination):
            self.assertEqual(destination, artifact)
            self.assertEqual(destination.read_bytes(), prior)
            self.assertEqual(staged.read_bytes(), expected)
            contender = os.open(artifact.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError, msg="publication lock is bypassed"):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(contender)
            replace(staged, destination)

        with patch("expose.mcp.generate.MANIFEST", artifact), patch.object(os, "replace", replace_while_locked):
            self.assertEqual(generate(["--catalog-only"]), 0)
        self.assertEqual(artifact.read_bytes(), expected)
        self.assertEqual(list(artifact.parent.glob(".registration-*")), [])
        contender = os.open(artifact.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)

    def test_generator_holds_directory_lock_during_atomic_replace(self):
        self.assert_publication_lock()

    def test_mutant_mcp_publication_without_lock_fails_serialization_oracle(self):
        with patch("expose._publication.flock", return_value=None) as unlocked:
            with self.assertRaisesRegex(AssertionError, "publication lock is bypassed"):
                self.assert_publication_lock()
            unlocked.assert_called_once()

    def test_stdio_dispatch_preserves_c1_digest_and_dry_run(self):
        dispatcher = self.dispatcher(self.fixture_registry({WINDOW}))
        binding = {"session_id": "fixture-session", "authority_epoch": 7}
        server = MCPServer(dispatcher, AGENT, binding_context=binding)
        initialize = rpc(1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "fixture"},
        })
        call = rpc(2, "tools/call", {"name": WINDOW, "arguments": arguments(session_id=binding["session_id"])})
        initialized, response = exchange(server, initialize, {"jsonrpc": "2.0", "method": "notifications/initialized"}, call)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "viewpoint-agent")
        self.assertEqual(initialized["result"]["protocolVersion"], PROTOCOL_VERSION)
        reply = json.loads(response["result"]["content"][0]["text"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(reply["code"], "OK")
        request = {
            "schema": control.REQUEST, "request_id": "mcp-fixture", "op": WINDOW, "dry_run": True,
            "args": {"session_id": "fixture-session"},
            "context": dict(binding, caller=AGENT.caller, agent_instance=AGENT.agent_instance),
        }
        self.assertEqual(reply["effective_request_sha256"], control.digest(request))
        self.assertEqual(self.previews, [request])
        self.assertEqual(self.calls, [])

    def test_tools_call_enforces_c1_lease_and_preserves_refusal(self):
        registry = self.fixture_registry({WINDOW})
        server = MCPServer(self.dispatcher(registry, lease=lambda _req, _risk: "NO_LEASE"), AGENT)
        call = rpc(1, "tools/call", {"name": WINDOW, "arguments": arguments(session_id="fixture-session")})
        [response] = exchange(server, call)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["code"], "NO_LEASE")
        self.assertEqual(self.previews, [])

    def test_v3_adapter_dispatches_only_after_a_frozen_binding_is_qualified(self):
        catalog = deepcopy(self.policy_rows)
        row = catalog["vp_data_call"]["ops"]["status"]
        data_result = {"copy": "file", "leases": []}

        def handler(request):
            self.calls.append(request)
            return control.Outcome(True, "OK", data_result)

        route = ("core_route", "fixture.data.status")
        bindings = {route: control.Handler(handler, handler)}
        parked = MCPServer(self.dispatcher(control.CapabilityRegistry(), bindings=bindings), AGENT)
        call = rpc(1, "tools/call", {"name": "vp_data_call", "arguments": arguments(op="status")})
        [refusal] = exchange(parked, call)
        self.assertEqual(json.loads(refusal["result"]["content"][0]["text"])["code"], "HOLD")
        self.assertEqual(self.calls, [])
        row.update(supported=True, binding={
            "kind": route[0], "name": route[1],
            "source": control.CapabilityRegistry().get("vp_data_call")["source"][0],
        })
        # This binding is synthetic and exists only during the test.
        with patch.object(control._validation, "_adapter_rows", return_value=catalog):
            registry = control.CapabilityRegistry()
            qualified = MCPServer(self.dispatcher(registry, bindings=bindings), AGENT)
            listed = self.assert_registry_listing(registry, qualified)
            self.assertIn("vp_data_call", {tool["name"] for tool in listed})
            call["params"]["arguments"]["request_id"] = "qualified-fixture"
            [receipt] = exchange(qualified, call)
        self.assertFalse(receipt["result"]["isError"])
        self.assertEqual(json.loads(receipt["result"]["content"][0]["text"])["result"], data_result)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_method_and_invalid_or_unregistered_tool_keep_rpc_codes(self):
        server = MCPServer(self.dispatcher(self.fixture_registry({WINDOW})), AGENT)
        unknown = rpc(1, "bogus/method")
        schema_only = rpc(2, "tools/call", {"name": "vp_window_dismiss", "arguments": arguments()})
        invalid = rpc(3, "tools/call", {"name": WINDOW, "arguments": arguments(caller="operator")})
        bad_arguments = rpc(4, "tools/call", {"name": WINDOW, "arguments": []})
        unknown_tool = rpc(5, "tools/call", {"name": "vp_unregistered", "arguments": arguments()})
        replies = exchange(server, unknown, schema_only, invalid, bad_arguments, unknown_tool)
        self.assertEqual([(row["id"], row["error"]["code"]) for row in replies],
                         [(1, -32601), (2, -32602), (3, -32602), (4, -32602), (5, -32602)])
        self.assertEqual(self.calls + self.previews, [])

    def test_invalid_stdio_frame_cannot_dispatch_its_tail(self):
        server = MCPServer(self.dispatcher(self.fixture_registry({WINDOW})), AGENT)
        call = rpc(1, "tools/call", {"name": WINDOW, "arguments": arguments(session_id="fixture-session")})
        raw = b" " * MAX_LINE_BYTES + control.canonical_bytes(call)
        out = BytesIO()
        serve(server, BytesIO(raw), out)
        [response] = out.getvalue().splitlines()
        self.assertEqual(json.loads(response)["error"]["code"], -32700)
        self.assertEqual(self.calls + self.previews, [])

    def test_stdio_accepts_a_newline_terminated_frame_at_the_limit(self):
        server = MCPServer(self.dispatcher(self.fixture_registry({WINDOW})), AGENT)
        ping = control.canonical_bytes(rpc(1, "ping"))
        raw = b" " * (MAX_LINE_BYTES - len(ping)) + ping
        out = BytesIO()
        serve(server, BytesIO(raw), out)
        self.assertEqual(json.loads(out.getvalue()), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_tools_list_rejects_nonempty_params(self):
        server = MCPServer(self.dispatcher(self.fixture_registry({WINDOW})), AGENT)
        [response] = exchange(server, rpc(1, "tools/list", {"cursor": "fixture"}))
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertNotIn("result", response)

    def test_portable_schema_inlines_references_inside_lists(self):
        reference = control._args_by_tool["vp_perception_call"]["$ref"]
        direct = _portable({"$ref": reference})
        self.assertNotIn("$ref", direct)
        self.assertEqual(_portable([{"$ref": reference}]), [direct])

    def test_frozen_hashes_and_exact_four_delegated_names(self):
        self.assert_frozen(read_frozen)

    def test_mutant_add_tool_to_frozen_mcp_server_fails_pin_oracle(self):
        source = read_frozen(FROZEN_MCP)
        original = b'TOOL_NAMES = ("mv_observe", "mv_list", "mv_propose", "mv_wait_verify")'
        altered = original[:-1] + b', "mv_extra")'
        mutant = source.replace(original, altered, 1)
        self.assertNotEqual(mutant, source)
        with self.assertRaisesRegex(AssertionError, FROZEN_MCP):
            self.assert_frozen(lambda path: mutant if path == FROZEN_MCP else read_frozen(path))

    def test_mutant_schema_only_tool_in_listing_fails_registry_oracle(self):
        registry = self.fixture_registry({WINDOW})
        server = MCPServer(self.dispatcher(registry), AGENT)
        extra = self.fixture_registry({"vp_window_dismiss"}).get("vp_window_dismiss")
        declarations = registration_manifest(control.CapabilityRegistry([extra]))["tools"]
        server._manifest["tools"].extend(row for row in declarations if row["name"] == extra["tool"])
        self.assertIn(extra["tool"], [row["name"] for row in server._manifest["tools"]])
        with self.assertRaisesRegex(AssertionError, extra["tool"]):
            self.assert_registry_listing(registry, server)

    def test_mutant_missing_required_list_fails_upstream_shape_oracle(self):
        registry = self.fixture_registry({WINDOW})
        server = MCPServer(self.dispatcher(registry), AGENT)
        tool = next(item for item in server._manifest["tools"] if item["name"] == WINDOW)
        del tool["inputSchema"]["required"]
        with self.assertRaisesRegex(AssertionError, "'required' not found"):
            self.assert_registry_listing(registry, server)


if __name__ == "__main__":
    unittest.main()
