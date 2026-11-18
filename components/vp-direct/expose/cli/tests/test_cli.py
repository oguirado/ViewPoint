import fcntl
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


SURFACE = Path(__file__).resolve().parents[1]
VP_DIRECT = SURFACE.parents[1]
sys.path.insert(0, str(VP_DIRECT))

from control.core import control
from expose import cli
from expose.cli import generate
from expose.mcp import MCPServer, serve
from expose.mcp import generate as mcp_generate
from oflane import Mailbox


TOOL = "vp_window_summon"
VERB = "window-summon"
FLAGS = (
    "--socket", "--session-id", "--agent-instance", "--core-instance",
    "--timeout-ms", "--max-line-bytes", "--max-reply-bytes",
)
V3_TOOLS = {"vp_perception_call", "vp_guest_call", "vp_data_call", "vp_factory_call"}
FROZEN = {
    "agents/metaviewer_agents/mcp_server.py":
        "a31d4381c53be7b2417b0acf46ad42008ed2710ac58c0929a1748c392357d862",
    "contracts/v0.2/mv_tool_call.schema.json":
        "7da7513b1b457dfeb08689c0ae70b2b950a2e093dab8706d99267acbd868cd5f",
}


def allow(_request, _risk):
    return True


class CLITests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(TemporaryDirectory(dir=SURFACE / "tests")))
        self.request_file = self.workspace / "request.json"
        entry_path = VP_DIRECT / "schemas/tests/examples/vp.capability_v1.valid.json"
        self.entry = json.loads(entry_path.read_bytes())
        self.registry = control.CapabilityRegistry([self.entry])
        self.identity = control.LaneIdentity("agent", "cli-agent")
        self.binding = cli.TransportBinding("/fixture/core.sock", "cli-session", "cli-core",
                                            self.identity, 500, 2048, 4096)
        self.calls = []

    def request(self, dry_run):
        return {"schema": control.REQUEST, "request_id": "cli-example", "op": TOOL,
                "context": {"caller": "agent", "agent_instance": self.identity.agent_instance,
                            "session_id": self.binding.session_id, "authority_epoch": 4},
                "args": {"session_id": self.binding.session_id}, "dry_run": dry_run}

    def argv(self, path=None, verb=VERB):
        return [
            verb, "--socket", self.binding.socket, "--session-id", self.binding.session_id,
            "--agent-instance", self.identity.agent_instance, "--core-instance",
            self.binding.core_instance, "--timeout-ms", str(self.binding.timeout_ms),
            "--max-line-bytes", str(self.binding.max_line_bytes),
            "--max-reply-bytes", str(self.binding.max_reply_bytes),
            "--json-arg", str(self.request_file if path is None else path),
        ]

    def write(self, request):
        self.request_file.write_bytes(control.canonical_bytes(request))

    def dispatcher(self, db, *, lease=allow, registry=None):
        def execute(request):
            self.calls.append(("execute", request))
            return control.Outcome(True, "OK")

        def preview(request):
            self.calls.append(("preview", request))
            return control.Outcome(True, "OK")

        return control.Dispatcher(registry or self.registry, control.RequestStore(self.workspace / db),
                                  pace_risk=allow, lease_epoch=lease,
                                  tools={TOOL: control.Handler(execute, preview)})

    def invoke(self, *, argv=None, dispatcher=None):
        stdout, stderr = StringIO(), StringIO()
        rc = cli.run(self.argv() if argv is None else argv,
                     self.dispatcher("cli.sqlite") if dispatcher is None else dispatcher,
                     self.binding, stdout=stdout, stderr=stderr)
        return rc, stdout.getvalue(), stderr.getvalue()

    def assert_transport_flag_required(self, parser, flag):
        args = self.argv()
        pos = args.index(flag)
        with self.assertRaises(ValueError):
            parser.parse_args(args[:pos] + args[pos + 2:])

    def assert_inline_refused(self):
        rc, out, err = self.invoke(argv=self.argv(path=control.canonical_bytes(
            self.request(True)).decode("utf-8")))
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["code"], "input_error")
        self.assertEqual(self.calls, [])

    def assert_parity(self, dry_run, *, dispatcher=None, flag=False):
        expected = self.request(dry_run)
        # Three independent C1 stores prevent a cached reply from masking lane differences.
        cli_request = dict(expected, dry_run=False) if flag else expected
        self.write(cli_request)
        rc, out, err = self.invoke(argv=self.argv() + (["--dry-run"] if flag else []),
                                   dispatcher=dispatcher)
        self.assertEqual((rc, err), (0, ""))
        server = MCPServer(
            self.dispatcher("mcp.sqlite"), self.identity,
            binding_context={"session_id": self.binding.session_id},
        )
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": TOOL, "arguments": dict(
                    expected["args"], request_id=expected["request_id"],
                    dry_run=dry_run, authority_epoch=4)}}
        response = BytesIO()
        serve(server, BytesIO(control.canonical_bytes(call)), response)
        result = json.loads(response.getvalue())["result"]
        self.assertFalse(result["isError"])
        mcp = json.loads(result["content"][0]["text"])
        mailbox = Mailbox(self.workspace, self.identity)
        mailbox.submit(expected)
        reply = mailbox.run_one(expected["request_id"], self.dispatcher("mailbox.sqlite"))
        digest = control.digest(expected)
        self.assertEqual(json.loads(out)["effective_request_sha256"], digest)
        self.assertEqual(json.loads(out), mcp)
        self.assertEqual(mcp, reply)
        self.assertEqual(self.calls, [("preview" if dry_run else "execute", expected)] * 3)

    def test_registry_projection_covers_all_registered_tools_and_v3_adapters(self):
        manifest = cli.registration_manifest(self.registry)
        self.assertEqual(manifest["registry_sha256"], control.digest(self.registry.entries()))
        self.assertEqual(manifest["required_flags"], [*FLAGS, "--json-arg"])
        rows = manifest["verbs"]
        self.assertEqual([(row["tool"], row["verb"]) for row in rows],
                         [(entry["tool"], entry["cli"]) for entry in self.registry.entries()])
        self.assertTrue(V3_TOOLS <= {row["tool"] for row in rows})
        for row in rows:
            entry = self.registry.get(row["tool"])
            self.assertEqual(row["supported"], entry["supported"])
            self.assertEqual(row["json_op"], row["tool"])
            self.assertEqual(row["ops"], entry["ops"])
            self.assertEqual(row["op_table"], [
                {key: op[key] for key in ("op", "supported", "operator_only")}
                for op in entry.get("op_table", ())
            ])

    def test_shared_verb_uses_registered_tool_from_request(self):
        sibling = dict(self.entry, tool="vp_window_dismiss", json_op="vp_window_dismiss")
        registry = control.CapabilityRegistry([sibling, self.entry])
        rows = cli.registration_manifest(registry)["verbs"]
        self.assertEqual({row["tool"] for row in rows if row["verb"] == VERB},
                         {TOOL, sibling["tool"]})
        self.write(self.request(True))
        self.assertEqual(self.invoke(dispatcher=self.dispatcher("shared.sqlite", registry=registry))[0], 0)

    def test_seven_transport_flags_required_and_json_file_required(self):
        parser = cli.build_parser(self.registry)
        self.assertEqual(parser.parse_args(self.argv()).verb, VERB)
        for flag in (*FLAGS, "--json-arg"):
            with self.subTest(flag=flag):
                self.assert_transport_flag_required(parser, flag)

    def test_dry_run_flag_dispatches_only_preview_with_mcp_and_mailbox_parity(self):
        self.assert_parity(True, flag=True)

    def test_no_dry_run_flag_preserves_explicit_execution_and_digest(self):
        self.assert_parity(False)

    def test_envelope_dry_run_has_no_cli_default(self):
        self.assert_parity(True)

    def test_inline_json_is_refused_before_dispatch(self):
        self.assert_inline_refused()

    def test_usage_binding_and_request_errors_refuse_before_dispatch(self):
        self.write(self.request(True))
        argv = self.argv()
        for args in (
            argv + ["--socket", self.binding.socket],
            argv + ["--dry-run", "--dry-run"],
            [("--time" if value == "--timeout-ms" else value) for value in argv],
            [("040" if value == "500" else value) for value in argv],
            [("/wrong.sock" if value == self.binding.socket else value) for value in argv],
            [("foreign-agent" if value == self.identity.agent_instance else value) for value in argv],
            self.argv(verb="unregistered"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(argv=args)[0], 2)
        wrong_session = self.request(True)
        wrong_session["context"]["session_id"] = "foreign-session"
        self.write(wrong_session)
        self.assertEqual(self.invoke()[0], 2)
        missing_dry_run = self.request(False)
        del missing_dry_run["dry_run"]
        self.write(missing_dry_run)
        self.assertEqual(self.invoke(argv=argv + ["--dry-run"])[0], 2)
        self.assertEqual(self.calls, [])

    def test_request_must_be_a_bounded_strict_regular_file(self):
        self.write(self.request(True))
        target = self.workspace / "link.json"
        target.symlink_to(self.request_file)
        for path in (target, self.workspace, self.workspace / "absent"):
            self.assertEqual(self.invoke(argv=self.argv(path))[0], 2)
        for content in (b"x" * 2049, b"\xff", b'{"dry_run":true,"dry_run":false}',
                        b'{"n":1e999}', b'{"n":NaN}', b"[]"):
            with self.subTest(content=content[:40]):
                self.request_file.write_bytes(content)
                self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.calls, [])

    def test_request_file_at_transport_byte_limit_is_dispatched(self):
        request = self.request(True)
        data = control.canonical_bytes(request)
        limit = self.binding.max_line_bytes
        self.request_file.write_bytes(data.ljust(limit + 1, b" "))
        self.assertEqual(self.invoke()[0], 2)
        self.assertEqual(self.calls, [])
        self.request_file.write_bytes(data.ljust(limit, b" "))
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.calls, [("preview", request)])

    def test_transport_rejects_mismatched_session_before_calling_c1(self):
        dispatcher = self.dispatcher("transport-boundary.sqlite")
        for field in ("context", "args"):
            with self.subTest(field=field):
                request = self.request(True)
                request[field]["session_id"] = "foreign-session"
                self.write(request)
                with patch.object(dispatcher, "dispatch_cli", wraps=dispatcher.dispatch_cli) as dispatch:
                    rc, out, err = self.invoke(dispatcher=dispatcher)
                    self.assertEqual((rc, out, json.loads(err)["code"]), (2, "", -32602))
                    dispatch.assert_not_called()

    def test_timeout_rejects_leading_zero_even_when_the_numeric_value_matches(self):
        self.write(self.request(True))
        argv = self.argv()
        name = argv.index("--timeout-ms")
        argv[name + 1] = "0" + argv[name + 1]
        rc, out, err = self.invoke(argv=argv)
        self.assertEqual((rc, out, json.loads(err)["code"]), (2, "", "usage_error"))
        self.assertEqual(self.calls, [])

    def test_request_parser_refuses_nonfinite_numbers_before_c1_validation(self):
        for number in (b"1e999", b"NaN"):
            with self.subTest(number=number):
                self.request_file.write_bytes(b'{"n":' + number + b"}")
                with self.assertRaisesRegex(ValueError, "^non-finite JSON number$"):
                    cli._read_request(self.request_file, self.binding.max_line_bytes)

    def test_inline_input_is_refused_before_any_file_open(self):
        with patch.object(cli.os, "open") as open_file:
            with self.assertRaisesRegex(ValueError, "^expected a file path$"):
                cli._read_request(Path('{"fixture":true}'), self.binding.max_line_bytes)
            open_file.assert_not_called()

    def test_offline_exit_codes_and_c1_refusal_are_preserved(self):
        self.write(self.request(True))
        self.assertEqual(self.invoke()[0], 0)
        rc, out, err = self.invoke(argv=self.argv(verb="capsule"))
        self.assertEqual((rc, out, json.loads(err)["code"]), (2, "", -32601))
        dispatcher = self.dispatcher("refused.sqlite", lease=lambda *_: "NO_LEASE")
        rc, out, err = self.invoke(dispatcher=dispatcher)
        self.assertEqual((rc, json.loads(out)["code"], err), (3, "NO_LEASE", ""))
        self.assertEqual(len(self.calls), 1)

    def assert_duplicate_request_refused(self):
        valid = control.canonical_bytes(self.request(False))
        duplicate = valid.replace(b'"args":{', b'"args":{"session_id":"substituted",', 1)
        self.assertNotEqual(duplicate, valid)
        self.request_file.write_bytes(duplicate)
        rc, out, err = self.invoke()
        self.assertEqual(rc, 2, "CLI parser accepted a duplicate key")
        self.assertEqual((out, json.loads(err)["code"]), ("", "input_error"))
        self.assertEqual(self.calls, [])

    def test_duplicate_nested_key_refuses_before_cli_dispatch(self):
        self.assert_duplicate_request_refused()

    def test_mutant_cli_duplicate_key_acceptance_fails_parse_oracle(self):
        with patch.object(cli, "_unique_object", side_effect=dict) as permissive:
            with self.assertRaisesRegex(AssertionError, "CLI parser accepted a duplicate key"):
                self.assert_duplicate_request_refused()
        self.assertGreater(permissive.call_count, 0)
        self.assertEqual(self.calls, [("execute", self.request(False))])

    def test_c1_stamps_provisioned_identity_and_replays_on_reply_limit(self):
        request = self.request(False)
        request["context"].update(caller="operator", agent_instance="spoofed")
        self.write(request)
        args = self.argv()
        args[args.index("--max-reply-bytes") + 1] = "1"
        rc, out, err = self.invoke(argv=args)
        self.assertEqual((rc, out, json.loads(err)["code"]), (3, "", "reply_too_large"))
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.calls, [("execute", self.request(False))])

    def test_generator_is_byte_stable_and_rejects_stale_manifest(self):
        actual_path = self.workspace / "manifest.json"
        source = self.workspace / "registry.json"
        source.write_bytes(control.canonical_bytes([self.entry]))
        with patch.object(generate, "MANIFEST", actual_path):
            self.assertEqual(generate.main(["--registry", str(source)]), 0)
            expected = control.canonical_bytes(cli.registration_manifest(self.registry))
            self.assertEqual(actual_path.read_bytes(), expected)
            self.assertEqual(generate.main(["--registry", str(source)]), 0)
            self.assertEqual(actual_path.read_bytes(), expected)
            self.assertEqual(generate.main(["--registry", str(source), "--check"]), 0)
            source.write_bytes(b"[]")
            with patch.object(sys, "stderr", StringIO()):
                self.assertEqual(generate.main(["--registry", str(source), "--check"]), 1)
            self.assertEqual(actual_path.read_bytes(), expected)

    def test_generator_refuses_an_object_as_the_registry_snapshot(self):
        source = self.workspace / "registry.json"
        source.write_text("{}", encoding="utf-8")
        stderr = StringIO()
        with patch.object(generate, "publish_manifest") as publish, patch.object(sys, "stderr", stderr):
            with self.assertRaises(SystemExit) as refusal:
                generate.main(["--registry", str(source)])
        self.assertEqual(refusal.exception.code, 2)
        self.assertIn("expected a JSON array of capabilities", stderr.getvalue())
        publish.assert_not_called()

    def assert_exposures_project_same_snapshot(self):
        source = self.workspace / "registry.json"
        source.write_bytes(control.canonical_bytes(self.registry.entries()))
        cli_path = self.workspace / "cli-manifest.json"
        mcp_path = self.workspace / "mcp-manifest.json"
        with patch.object(generate, "MANIFEST", cli_path), patch.object(mcp_generate, "MANIFEST", mcp_path):
            self.assertEqual(generate.main(["--registry", str(source)]), 0)
            self.assertEqual(mcp_generate.main(["--registry", str(source)]), 0)
        cli_manifest = json.loads(cli_path.read_bytes())
        mcp_manifest = json.loads(mcp_path.read_bytes())
        names = [entry["tool"] for entry in self.registry.entries()]
        self.assertEqual([row["tool"] for row in cli_manifest["verbs"]], names)
        self.assertEqual([row["name"] for row in mcp_manifest["tools"]], names)
        registry_digest = control.digest(self.registry.entries())
        self.assertEqual(cli_manifest["registry_sha256"], registry_digest)
        self.assertEqual(mcp_manifest["registry_sha256"], registry_digest)

    def test_cli_and_mcp_generators_use_the_same_provisioned_snapshot(self):
        self.assert_exposures_project_same_snapshot()

    def test_mutant_catalog_fallback_drops_provisioned_tool(self):
        with patch.object(mcp_generate, "CapabilityRegistry", return_value=control.CapabilityRegistry()):
            with self.assertRaisesRegex(AssertionError, TOOL):
                self.assert_exposures_project_same_snapshot()

    def assert_lock_held_during_atomic_replace(self):
        manifest = self.workspace / "manifest.json"
        manifest.write_bytes(b"prior manifest\n")
        source = self.workspace / "registry.json"
        source.write_bytes(control.canonical_bytes([self.entry]))
        expected = control.canonical_bytes(cli.registration_manifest(self.registry))
        replace = os.replace

        def observe_replace(staged, destination):
            self.assertEqual(destination, manifest)
            self.assertEqual(manifest.read_bytes(), b"prior manifest\n")
            self.assertEqual(staged.read_bytes(), expected)
            competitor = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError, msg="publication lock is bypassed"):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(competitor)
            replace(staged, destination)

        with patch.object(generate, "MANIFEST", manifest), patch.object(os, "replace", observe_replace):
            self.assertEqual(generate.main(["--registry", str(source)]), 0)
        self.assertEqual(manifest.read_bytes(), expected)
        self.assertEqual(list(self.workspace.glob(".registration-*")), [])
        competitor = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competitor)

    def test_generated_writes_serialize_at_atomic_replace(self):
        self.assert_lock_held_during_atomic_replace()

    def test_mutant_bypassing_publication_lock_fails_serialization_oracle(self):
        with patch("expose._publication.flock", return_value=None):
            with self.assertRaisesRegex(AssertionError, "publication lock is bypassed"):
                self.assert_lock_held_during_atomic_replace()

    def test_frozen_files_match(self):
        for path, expected in FROZEN.items():
            with self.subTest(path=path):
                self.assertEqual(sha256((VP_DIRECT.parents[1] / path).read_bytes()).hexdigest(),
                                 expected)

    def test_mutant_dropped_transport_flag_fails_requirement_oracle(self):
        parser = cli.build_parser(self.registry)
        action = next(action for action in parser._actions if "--socket" in action.option_strings)
        action.required = False
        with self.assertRaises(AssertionError):
            self.assert_transport_flag_required(parser, "--socket")

    def test_mutant_inline_json_fails_file_only_oracle(self):
        with patch.object(cli, "_read_request", side_effect=lambda path, _: json.loads(str(path))):
            with self.assertRaisesRegex(AssertionError, "0 != 2"):
                self.assert_inline_refused()
        self.assertEqual(self.calls, [("preview", self.request(True))])

    def test_mutant_cli_only_dry_run_change_fails_parity_oracle(self):
        dispatcher = self.dispatcher("mutant.sqlite")
        dispatch = dispatcher.dispatch_cli
        with patch.object(dispatcher, "dispatch_cli",
                          side_effect=lambda verb, request, identity: dispatch(
                              verb, dict(request, dry_run=False), identity)):
            with self.assertRaisesRegex(AssertionError, control.digest(self.request(True))):
                self.assert_parity(True, dispatcher=dispatcher, flag=True)
        self.assertEqual([phase for phase, _ in self.calls], ["execute", "preview", "preview"])
