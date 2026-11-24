# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from video_rea.expose import Exposure, handle, register
from video_rea.expose.catalog import VP_SCHEMAS
from video_rea.expose.mcp import respond, serve
from video_rea.expose.tests.supervised import run_module
from video_rea.expose.wire import MAX_LINE_BYTES


CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
TABLE = "vp_rea_call.op_table.json"
SCHEMA = "vp_rea_call.args_v1.schema.json"


def read(path):
    return json.loads(path.read_bytes())


def example(name):
    return read(CONTRACTS / "tests/examples" / (name + ".json"))["valid"]


def envelope(args, dry_run=False):
    return {"schema": "vp.control.req/v1", "request_id": "w1-local", "op": "vp_rea_call",
            "context": {"caller": "agent", "agent_instance": "video-rea-local"},
            "args": args, "dry_run": dry_run}


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": {} if params is None else params}


def tool_call(args, dry_run=False, rid="w1-local"):
    return rpc("tools/call", {"name": "vp_rea_call", "arguments": dict(args, dry_run=dry_run, request_id=rid)})


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    return hashlib.sha256(raw.encode()).hexdigest()


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.table = read(CONTRACTS / TABLE)
        self.api = Exposure()
        self.args = example("vp_rea_call.rea.ingest.args")

    def tool(self, api):
        tools = respond(rpc("tools/list"), api)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [self.table["tool"]])
        return tools[0]["inputSchema"]

    def test_discovery_and_parked_replies_for_every_selector(self):
        schema = self.tool(self.api)
        rows = self.table["op_table"]
        self.assertEqual(set(schema["properties"]["op"]["enum"]), set(rows))
        validator = Draft202012Validator(schema)
        main_request, main_reply = (
            read(VP_SCHEMAS / f"vp.control.{kind}_v1.schema.json") for kind in ("req", "reply")
        )
        registry = Registry().with_resources(
            (schema["$id"], DRAFT202012.create_resource(schema)) for schema in (main_request, main_reply)
        )
        requests = Draft202012Validator(main_request, registry=registry)
        replies = Draft202012Validator(main_reply, registry=registry)
        for op, row in rows.items():
            with self.subTest(op=op):
                args = example(row["args_schema"].replace("/v1", ""))
                flat = tool_call(args)["params"]["arguments"]
                validator.validate(flat)
                self.assertFalse(validator.is_valid(dict(flat, unexpected=True)))
                for preview in (True, False):
                    request = envelope(args, preview)
                    requests.validate(request)
                    self.assertFalse(requests.is_valid(dict(request, args=[])))
                    reply = handle(request)
                    replies.validate(reply)
                    self.assertFalse(reply["ok"])
                    self.assertEqual(reply["code"], "HOLD")
                    self.assertEqual(reply["effective_request_sha256"], digest(request))
                    plan = reply["result"]["plan"]
                    self.assertFalse(replies.is_valid(dict(reply, result={"plan": dict(plan, executed=True)})))
                    self.assertEqual((plan["op"], plan["reply_schema"]), (op, row["reply_schema"]))
                    result = self.api.catalog.resources[row["reply_schema"]]
                    for name in plan["produces"]:
                        self.assertRegex(name, r"^rea\..+/v\d+$")
                        self.assertIn(name, result["description"])

    def test_discovery_tracks_a_changed_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = read(CONTRACTS / SCHEMA)
            retired = schema["properties"]["op"]["enum"].pop()
            del self.table["op_table"][retired]
            schema["allOf"] = [b for b in schema["allOf"] if b["if"]["properties"]["op"]["const"] != retired]
            (root / TABLE).write_text(json.dumps(self.table))
            (root / SCHEMA).write_text(json.dumps(schema))
            alternate = Exposure(root)
            self.assertEqual(set(self.tool(alternate)["properties"]["op"]["enum"]), set(self.table["op_table"]))
            self.assertEqual(alternate.handle(envelope({"op": retired}))["code"], "-32601")
            original = json.dumps(schema)
            schema["allOf"][0]["then"] = schema["allOf"][-1]["then"]
            (root / SCHEMA).write_text(json.dumps(schema))
            with self.assertRaisesRegex(ValueError, "selector conditions"):
                Exposure(root)
            (root / SCHEMA).write_text(original)
            next(iter(self.table["op_table"].values()))["args_schema_sha256"] = "0" * 64
            (root / TABLE).write_text(json.dumps(self.table))
            with self.assertRaises(ValueError):
                Exposure(root)

    def test_invalid_unknown_and_untrusted_metadata(self):
        invalid_args = [
            {**self.args, "inputs": [{}]}, {**self.args, "seed": -1},
            {**self.args, "unexpected": True}, {"op": "rea.ingest"},
        ]
        for args in invalid_args:
            self.assertEqual(handle(envelope(args))["code"], "-32602")
            self.assertEqual(respond(tool_call(args), self.api)["error"]["code"], -32602)
        unknown = dict(self.args, op="rea.unknown")
        self.assertEqual(handle(envelope(unknown))["code"], "-32601")
        self.assertEqual(respond(tool_call(unknown), self.api)["error"]["code"], -32601)
        for field, value in [("context", {}), ("dry_run", "true"), ("request_id", ""), ("extra", 1)]:
            self.assertEqual(handle(dict(envelope(self.args), **{field: value}))["code"], "-32602")
        self.assertEqual(respond(tool_call(dict(self.args, caller="operator")), self.api)["error"]["code"], -32602)

    def test_repaired_args_are_used_for_discovery_and_calls(self):
        schema = read(CONTRACTS / SCHEMA)
        row = self.table["op_table"]["rea.ingest"]
        resource = next(item for item in schema["$defs"].values() if item.get("title") == row["args_schema"])
        resource["properties"]["fixture_revision"] = {"const": "repaired"}
        resource["required"].append("fixture_revision")
        schema["properties"]["fixture_revision"] = {}
        row["args_schema_sha256"] = digest(resource)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / TABLE).write_text(json.dumps(self.table))
            (root / SCHEMA).write_text(json.dumps(schema))
            repaired = Exposure(root)
            wire = Draft202012Validator(self.tool(repaired))
            for revision, accepted in (({}, False), ({"fixture_revision": "repaired"}, True)):
                args = {**self.args, **revision}
                rpc_call = tool_call(args)
                self.assertEqual(wire.is_valid(rpc_call["params"]["arguments"]), accepted)
                self.assertEqual(repaired.handle(envelope(args))["code"], "HOLD" if accepted else "-32602")
                self.assertEqual("result" in respond(rpc_call, repaired), accepted)

    def test_registration_and_in_memory_preview(self):
        calls = []
        expected = example("vp_rea_call.rea.ingest.result")
        def preview(args):
            calls.append(args)
            return expected
        self.api.register("rea.ingest", preview)
        self.assertFalse(self.api.handle(envelope(self.args))["ok"])
        self.assertFalse(self.api.handle(envelope({"op": "rea.ingest"}, True))["ok"])
        self.assertEqual(calls, [])
        reply = self.api.handle(envelope(self.args, True))
        self.assertEqual(calls, [self.args])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], expected)
        with self.assertRaises(ValueError):
            self.api.register("unknown", preview)
        with patch("video_rea.expose.lane.local", return_value=self.api):
            register("rea.ingest", preview)
            self.assertTrue(handle(envelope(self.args, True))["ok"])

    def test_preview_cannot_write_or_return_invalid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unexpected"
            def tries_to_write(_args):
                path.write_text("unexpected")
                return example("vp_rea_call.rea.ingest.result")
            self.api.register("rea.ingest", tries_to_write)
            self.assertFalse(self.api.handle(envelope(self.args, True))["ok"])
            self.assertFalse(path.exists())
        api = Exposure()
        api.register("rea.ingest", lambda _: {"not_a_result": True})
        self.assertFalse(api.handle(envelope(self.args, True))["ok"])

    def test_denied_io_does_not_affect_the_caller(self):
        expected = example("vp_rea_call.rea.ingest.result")
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp) / "existing"
            protected.write_bytes(b"before")
            with protected.open("ab", buffering=0) as stream:
                def preview(_args):
                    with self.assertRaises(PermissionError):
                        protected.read_bytes()
                    with self.assertRaises(PermissionError):
                        os.write(stream.fileno(), b"from-preview")
                    return expected
                self.api.register("rea.ingest", preview)
                self.assertTrue(self.api.handle(envelope(self.args, True))["ok"])
                stream.write(b"-after")
            self.assertEqual(protected.read_bytes(), b"before-after")

    def test_privileged_rows_and_unavailable_isolation_refuse_before_callback(self):
        called = []
        def unexpected(_args):
            called.append(True)
        self.api.register("rea.ingest", unexpected)
        row = self.api.catalog.rows["rea.ingest"]
        request = envelope(self.args, True)
        request["context"]["caller"] = "operator"
        for change in ({"operator_only": True}, {"risk_class": "enviar"}):
            with patch.dict(row, change):
                self.assertEqual(self.api.handle(request)["code"], "HUMAN_NEEDED")
        with patch("video_rea.expose.preview._restrict_thread", side_effect=OSError):
            self.assertEqual(self.api.handle(request)["code"], "HOLD")
        self.assertEqual(called, [])

    def test_frame_limits_and_protocol_errors(self):
        for data in (b"{}" * MAX_LINE_BYTES + b"\n", b"{}"):
            out = BytesIO()
            serve(BytesIO(data), out, self.api)
            self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)
        for frame in (br'{"jsonrpc":"2.0","id":"\ud800","method":"tools/list"}', b'{"id":1e999}'):
            out = BytesIO()
            serve(BytesIO(frame + b"\n"), out, self.api)
            self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)
        for message, code in (([], -32600), (rpc("initialize"), -32602),
                              (rpc("tools/list", {"x": 1}), -32602),
                              (rpc("tools/call", {"name": "other", "arguments": {}}), -32601)):
            self.assertEqual(respond(message, self.api)["error"]["code"], code)

    def test_mcp_pipe_and_bad_frames(self):
        messages = [
            rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc("tools/list"), tool_call(self.args, True), tool_call({"op": "rea.ingest"}),
            rpc("unknown"),
        ]
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        child = run_module("mcp", stdin=stdin + '{"id":0,"id":1}\n')
        self.assertEqual(child.returncode, 0, child.stderr)
        init, listing, call, invalid, unknown, malformed = map(json.loads, child.stdout.splitlines())
        self.assertIn("tools", init["result"]["capabilities"])
        self.assertEqual(listing["result"]["tools"][0]["inputSchema"], self.tool(self.api))
        result = call["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), handle(envelope(self.args, True)))
        self.assertEqual([r["error"]["code"] for r in (invalid, unknown, malformed)], [-32602, -32601, -32700])

    def test_cli_and_lane_agree(self):
        child = run_module("cli", "list")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["op_table"], self.table["op_table"])
        child = run_module("cli", "describe", "rea.ingest")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["args_schema"], self.table["op_table"]["rea.ingest"]["args_schema"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "args.json"
            args = dict(self.args)
            del args["op"]
            path.write_text(json.dumps(args))
            for flags in (["--args-file", str(path)], ["--args", json.dumps(args)]):
                child = run_module("cli", "call", "rea.ingest", "--dry-run", *flags)
                self.assertNotEqual(child.returncode, 0)
                reply = json.loads(child.stdout)
                request = dict(envelope(self.args, True), request_id=reply["request_id"])
                self.assertEqual(reply, handle(request))
            for raw, code in [('{"op":"different"}', "-32602"), ("{", "-32602")]:
                child = run_module("cli", "call", "rea.ingest", "--args", raw)
                self.assertNotEqual(child.returncode, 0)
                self.assertEqual(json.loads(child.stdout)["code"], code)

    def test_subprocess_guard(self):
        with patch.dict(os.environ, {"AF_PROC_PIDNS_PROC": "/proc"}):
            with self.assertRaisesRegex(RuntimeError, "namespace"):
                run_module("mcp")
        with self.assertRaises(ValueError):
            run_module("another_module")

    def test_cli_rejects_non_regular_oversized_and_duplicate_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "args.json"
            link = Path(tmp) / "link"
            link.symlink_to(path)
            path.write_text(json.dumps(self.args))
            for invalid in (link, Path(tmp)):
                reply = run_module("cli", "call", "rea.ingest", "--args-file", str(invalid))
                self.assertEqual(json.loads(reply.stdout)["code"], "-32602")
            for raw in (b" " * MAX_LINE_BYTES + b"{}", b'{"seed":1,"seed":2}'):
                path.write_bytes(raw)
                reply = run_module("cli", "call", "rea.ingest", "--args-file", str(path))
                self.assertEqual(json.loads(reply.stdout)["code"], "-32602")

