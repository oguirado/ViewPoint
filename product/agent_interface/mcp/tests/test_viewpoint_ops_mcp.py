#!/usr/bin/env python3
"""Scripted JSON-RPC client against the viewpoint-ops stdio server.

Run: python3 -I tests/test_viewpoint_ops_mcp.py
Every positive check has a negative control next to it.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.dirname(HERE)
SERVER = os.path.join(MCP_DIR, "viewpoint_ops_mcp.py")
STUB = os.path.join(MCP_DIR, "stub_vp")
SELFTEST_TOOLS = os.path.join(HERE, "tools_selftest.json")
SECRET = "-".join(["fixture", "value", "for", "redaction"])  # low entropy on purpose: tools/check_public.sh SECRET rule


class Client:
    """Starts the server, sends one request per line, reads one reply per line."""

    def __init__(self, tools=SELFTEST_TOOLS, cli=STUB, extra_env=None):
        self.tmp = tempfile.TemporaryDirectory(prefix="vpops-test-")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "VP_OPS_SPOOL": os.path.join(self.tmp.name, "spool"),
               "VP_OPS_MAX_BYTES": "1024", "VPOPS_TEST_TOKEN": SECRET}
        if tools:
            env["VP_OPS_TOOLS_PATH"] = tools
        if cli:
            env["VP_OPS_CLI"] = cli
        env.update(extra_env or {})
        self.proc = subprocess.Popen([sys.executable, "-I", SERVER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.next_id = 0

    def raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def rpc(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        reply = self.raw(json.dumps(msg))
        assert reply["id"] == self.next_id, reply
        return reply

    def call(self, name, arguments=None):
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result = reply["result"]
        text = result["content"][0]["text"]
        last = json.loads(text.splitlines()[-1])
        return result, text, last

    def close(self):
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.tmp.cleanup()
        return rc


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.c = Client()

    def tearDown(self):
        self.assertEqual(self.c.close(), 0)

    def test_initialize_ping_list(self):
        init = self.c.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})["result"]
        self.assertEqual(init["serverInfo"]["name"], "viewpoint-ops")
        self.assertEqual(init["protocolVersion"], "2024-11-05")
        self.assertEqual(self.c.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(self.c.rpc("ping")["result"], {})
        tools = self.c.rpc("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("vp_capsule_start", names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIsInstance(tool["description"], str)
            # private routing keys never reach the client
            self.assertLessEqual(set(tool), {"name", "description", "inputSchema", "annotations"})
        by = {t["name"]: t for t in tools}
        self.assertTrue(by["vp_capsule_stop"]["annotations"]["destructiveHint"])
        self.assertTrue(by["vp_rewind_restore"]["annotations"]["destructiveHint"])
        self.assertFalse(by["vp_capsule_start"]["annotations"]["destructiveHint"])

    def test_notification_gets_no_reply(self):
        self.c.proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.c.proc.stdin.flush()
        # The next reply on the wire must belong to the ping, not the notification.
        self.assertEqual(self.c.rpc("ping")["result"], {})

    def test_negative_protocol_errors(self):
        self.assertEqual(self.c.raw("{not json")["error"]["code"], -32700)
        self.assertEqual(self.c.raw('{"id":1,"method":"ping"}')["error"]["code"], -32600)
        self.assertEqual(self.c.raw("[1,2]")["error"]["code"], -32600)
        self.assertEqual(self.c.rpc("resources/subscribe")["error"]["code"], -32601)
        self.assertEqual(self.c.rpc("tools/call", {"arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.c.rpc("tools/call", {"name": "vp_capsule_start", "arguments": [1]})["error"]["code"], -32602)

    def test_negative_unknown_tool(self):
        reply = self.c.rpc("tools/call", {"name": "vp_capsule_explode", "arguments": {}})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertIn("unknown tool", reply["error"]["message"])
        self.assertNotIn("result", reply)


class CallTest(unittest.TestCase):
    def setUp(self):
        self.c = Client()

    def tearDown(self):
        self.assertEqual(self.c.close(), 0)

    def test_call_maps_arguments_to_flags(self):
        result, text, last = self.c.call("vp_capsule_start", {"capsule": "vpops-a", "mode": "browser", "tags": ["x", "y"], "dry_run": True})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], last)
        self.assertEqual(set(last) >= {"ok", "code", "rc", "data", "next"}, True)
        self.assertEqual(last["code"], "DRY_RUN")
        d = last["data"]
        self.assertEqual((d["noun"], d["verb"]), ("capsule", "start"))
        self.assertEqual(d["flags"], {"capsule": "vpops-a", "mode": "browser", "tags": ["x", "y"], "dry-run": True})
        self.assertLessEqual(len(text.encode()), 1024 + 1024)

    def test_negative_schema_violations_start_nothing(self):
        cases = [
            ({}, "missing required 'capsule'"),
            ({"capsule": 7}, "expected string"),
            ({"capsule": "a", "mode": "kernel"}, "not one of"),
            ({"capsule": "a", "bogus": 1}, "unknown argument 'bogus'"),
            ({"capsule": "a", "tags": ["ok", 3]}, "arguments.tags[1]"),
            ({"capsule": ""}, "shorter than"),
            ({"capsule": "a", "dry_run": "yes"}, "expected boolean"),
        ]
        for arguments, needle in cases:
            result, text, last = self.c.call("vp_capsule_start", arguments)
            self.assertTrue(result["isError"], arguments)
            self.assertEqual((last["ok"], last["code"], last["rc"]), (False, "SCHEMA", 2), arguments)
            self.assertIn(needle, text, arguments)
            self.assertIs(last["data"]["started"], False)
        result, _, last = self.c.call("t_emit", {"lines": 0})
        self.assertEqual(last["code"], "SCHEMA")

    def test_destructive_needs_confirm(self):
        # negative control: no confirm, not a dry run -> refused by the server, CLI not started
        result, text, last = self.c.call("vp_capsule_stop", {"capsule": "vpops-a"})
        self.assertTrue(result["isError"])
        self.assertEqual((last["code"], last["rc"]), ("CONFIRM_REQUIRED", 4))
        self.assertIs(last["data"]["started"], False)
        self.assertEqual(last["next"], ["vp capsule stop vpops-a --dry-run"])
        # empty confirm is no confirm; dry_run=false is not a dry run
        for arguments in ({"capsule": "vpops-a", "confirm": "  "}, {"capsule": "vpops-a", "dry_run": False}):
            self.assertEqual(self.c.call("vp_capsule_stop", arguments)[2]["code"], "CONFIRM_REQUIRED")
        # destructive flag given as annotations.destructiveHint, argv derived from the name
        self.assertEqual(self.c.call("vp_rewind_restore", {"capsule": "vpops-a"})[2]["code"], "CONFIRM_REQUIRED")
        # dry run is allowed and hands out the confirm id
        result, _, dry = self.c.call("vp_capsule_stop", {"capsule": "vpops-a", "dry_run": True})
        self.assertFalse(result["isError"])
        self.assertEqual(dry["data"]["positional"], ["vpops-a"])
        cid = dry["data"]["confirm_id"]
        self.assertEqual(dry["mcp"]["next_tools"], ["vp_capsule_stop"])
        # negative control: a wrong id passes the server gate but the CLI refuses it
        result, _, wrong = self.c.call("vp_capsule_stop", {"capsule": "vpops-a", "confirm": "c-deadbeef"})
        self.assertTrue(result["isError"])
        self.assertEqual((wrong["code"], wrong["rc"]), ("CONFIRM_REQUIRED", 4))
        self.assertNotIn("started", wrong["data"])
        # the right id goes through
        result, _, done = self.c.call("vp_capsule_stop", {"capsule": "vpops-a", "confirm": cid})
        self.assertFalse(result["isError"])
        self.assertTrue(done["ok"])
        self.assertEqual(self.c.call("vp_rewind_restore", {"capsule": "a", "dry_run": True})[2]["data"]["verb"], "restore")

    def test_long_output_is_cut_and_spooled(self):
        result, text, last = self.c.call("t_emit", {"lines": 3000})
        self.assertFalse(result["isError"])
        self.assertLess(len(text.encode()), 2600)
        self.assertIn("[cut:", text)
        spool = last["mcp"]["spool"]
        self.assertTrue(os.path.isfile(spool["path"]))
        self.assertEqual(oct(os.stat(spool["path"]).st_mode & 0o777), "0o600")
        with open(spool["path"], encoding="utf-8") as fh:
            body = fh.read()
        self.assertEqual(body.count("line "), 3000)
        self.assertEqual(spool["lines"], 3001)
        self.assertIn(" read " + spool["path"], last["next"][-1])
        self.assertEqual(last["data"], {"lines": 3000})
        # negative control: short output is neither cut nor spooled
        result, text, last = self.c.call("t_emit", {"lines": 3})
        self.assertNotIn("[cut:", text)
        self.assertNotIn("spool", last["mcp"])
        self.assertEqual(text.count("line "), 3)

    def test_oversized_receipt_data_is_cut(self):
        _, text, last = self.c.call("t_bigdata", {"bytes": 9000})
        self.assertLess(len(text.encode()), 2600)
        self.assertTrue(last["ok"])
        self.assertTrue(last["data"]["truncated"])
        with open(last["mcp"]["spool"]["path"], encoding="utf-8") as fh:
            self.assertIn('"blob"', fh.read())

    def test_failures_are_reported_not_hidden(self):
        result, text, last = self.c.call("t_fail")
        self.assertTrue(result["isError"])
        self.assertEqual((last["code"], last["rc"]), ("ENGINE", 5))
        self.assertIn("[stderr]\nit broke", text)
        result, text, last = self.c.call("t_noreceipt")
        self.assertTrue(result["isError"])
        self.assertEqual(last["code"], "NO_RECEIPT")
        self.assertIn("plain text and nothing else", text)
        result, _, last = self.c.call("t_lie")  # receipt says ok, exit status says 9
        self.assertTrue(result["isError"])
        self.assertEqual((last["code"], last["rc"]), ("RC_MISMATCH", 9))

    def test_time_limit(self):
        result, _, last = self.c.call("t_sleep", {"seconds": 30})
        self.assertTrue(result["isError"])
        self.assertEqual((last["code"], last["rc"]), ("TIMEOUT", 124))
        self.assertFalse(os.path.exists(f"/proc/{last['data']['stopped_pid']}"))
        # negative control: under the limit it passes
        self.assertTrue(self.c.call("t_sleep", {"seconds": 0.05})[2]["ok"])
        # the server is still alive after stopping the child
        self.assertEqual(self.c.rpc("ping")["result"], {})

    def test_secret_values_are_redacted(self):
        _, text, last = self.c.call("t_secret")
        self.assertNotIn(SECRET, text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(last["data"]["echo"], "[REDACTED]")

    def test_stdin_argument_is_not_put_on_the_command_line(self):
        _, _, last = self.c.call("t_stdin", {"body": "héllo\nworld", "tag": "t1"})
        self.assertEqual(last["data"], {"stdin_bytes": 12, "flags": {"tag": "t1"}})


class StartupTest(unittest.TestCase):
    def test_default_table_and_check(self):
        out = subprocess.run([sys.executable, "-I", SERVER, "--check"], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"]})
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)["data"]
        self.assertGreaterEqual(data["tools"], 10)
        self.assertTrue(data["destructive"])

    def test_every_default_tool_dry_runs(self):
        """Whatever table and CLI are found by default: each tool must accept a
        dry run built from its own schema, and each destructive one must be
        refused without confirm."""
        c = Client(tools=None, cli=None, extra_env={"VP_OPS_TOOLS": "all", "VP_OPS_MAX_BYTES": "4096"})
        try:
            tools = c.rpc("tools/list")["result"]["tools"]
            self.assertGreaterEqual(len(tools), 10)
            for tool in tools:
                schema = tool["inputSchema"]
                props = schema.get("properties", {})
                args = {}
                for key in schema.get("required", []):
                    p = props.get(key, {})
                    kind = p.get("type", "string")
                    args[key] = (p["enum"][0] if "enum" in p else p.get("minimum", 1) if kind in ("integer", "number")
                                 else True if kind == "boolean" else [] if kind == "array" else {} if kind == "object"
                                 else "https://example.invalid/" if key == "url" else "vpops-x")
                if tool["annotations"]["destructiveHint"]:
                    _, _, last = c.call(tool["name"], args)
                    self.assertEqual(last["code"], "CONFIRM_REQUIRED", tool["name"])
                if "dry_run" in props:
                    result, text, last = c.call(tool["name"], dict(args, dry_run=True))
                    self.assertIn("ok", last, tool["name"])
                    self.assertNotEqual(last["code"], "SCHEMA", tool["name"])
                    self.assertNotEqual(last["code"], "NO_RECEIPT", (tool["name"], text))
        finally:
            c.close()

    def test_tiers_core_listed_all_callable(self):
        with tempfile.TemporaryDirectory(prefix="vpops-test-") as tmp:
            path = os.path.join(tmp, "t.json")
            with open(path, "w") as fh:
                json.dump({"tools": [{"name": "vp_a_b", "tier": "core"}, {"name": "vp_c_d", "tier": "extended"},
                                     {"name": "vp_e_f"}]}, fh)
            core = Client(tools=path)
            everything = Client(tools=path, extra_env={"VP_OPS_TOOLS": "all"})
            try:
                self.assertEqual([t["name"] for t in core.rpc("tools/list")["result"]["tools"]], ["vp_a_b", "vp_e_f"])
                self.assertEqual(len(everything.rpc("tools/list")["result"]["tools"]), 3)
                # an unlisted tool is still callable
                self.assertEqual(core.call("vp_c_d", {"dry_run": True})[2]["data"]["noun"], "c")
            finally:
                core.close()
                everything.close()
            # negative control: a mode that is neither core nor all stops the server
            out = subprocess.run([sys.executable, "-I", SERVER], input="", capture_output=True, text=True,
                                 env={"PATH": os.environ["PATH"], "VP_OPS_TOOLS": path})
            self.assertEqual(out.returncode, 78)
            self.assertIn("VP_OPS_TOOLS_PATH", out.stderr)

    def test_negative_bad_tool_tables(self):
        with tempfile.TemporaryDirectory(prefix="vpops-test-") as tmp:
            bad = {
                "empty.json": "[]",
                "notjson.json": "{",
                "dup.json": '[{"name":"a"},{"name":"a"}]',
                "noname.json": '[{"description":"x"}]',
                "badname.json": '[{"name":"a b; rm"}]',
            }
            for fname, body in bad.items():
                path = os.path.join(tmp, fname)
                with open(path, "w") as fh:
                    fh.write(body)
                out = subprocess.run([sys.executable, "-I", SERVER], input="", capture_output=True, text=True,
                                     env={"PATH": os.environ["PATH"], "VP_OPS_TOOLS_PATH": path})
                self.assertEqual(out.returncode, 78, fname)
                self.assertEqual(out.stdout, "", fname)
                self.assertIn("viewpoint-ops:", out.stderr)

    def test_negative_missing_cli(self):
        c = Client(cli="/nonexistent/vp")
        try:
            result, _, last = c.call("vp_capsule_start", {"capsule": "a"})
            self.assertTrue(result["isError"])
            self.assertEqual((last["code"], last["rc"]), ("CLI_MISSING", 8))
        finally:
            c.close()

    def test_positional_that_looks_like_a_flag(self):
        c = Client()
        try:
            _, _, last = c.call("vp_capsule_stop", {"capsule": "--confirm", "dry_run": True})
            self.assertEqual(last["data"]["positional"], ["--confirm"])
            self.assertEqual(last["data"]["flags"], {"dry-run": True})
        finally:
            c.close()


class FixOps(unittest.TestCase):
    def test_no_host_paths_or_secrets_in_mcp(self):
        # xr-ops-docs A1: the repo's public gate finds nothing under mcp/.
        root = os.path.normpath(os.path.join(MCP_DIR, "..", "..", ".."))
        gate = os.path.join(root, "tools", "check_public.sh")
        if not os.path.isfile(gate):
            self.skipTest("tools/check_public.sh absent")
        out = subprocess.run([gate, "--no-history"], cwd=root, capture_output=True, text=True).stdout
        self.assertEqual([l for l in out.splitlines() if "\tproduct/agent_interface/mcp/" in l], [])

    def test_fallback_table_is_a_subset_of_the_design_table(self):
        # xr-ops-docs A4: every fallback argv is a command of the design table (vpops).
        import importlib.util
        spec = importlib.util.spec_from_file_location("vpops_mcp", SERVER)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        design = [p for p in m.TOOLS_CANDIDATES[:-1] if os.path.isfile(p)]
        if not design:
            self.skipTest("design MCP_TOOLS.json absent")
        def argvs(path):
            d = json.load(open(path))
            return {" ".join(t["argv"]) for t in (d["tools"] if isinstance(d, dict) else d) if t.get("argv")}
        self.assertEqual(sorted(argvs(m.TOOLS_CANDIDATES[-1]) - argvs(design[0])), [])

    def test_sibling_candidates_follow_vp_ops_root(self):
        r = subprocess.run([sys.executable, "-I", "-c", "import importlib.util as u,sys;s=u.spec_from_file_location('m',sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);print(m.CLI_CANDIDATES[1])", SERVER],
                           capture_output=True, text=True, env=dict(os.environ, VP_OPS_ROOT="/srv/ws"))
        self.assertEqual(r.stdout.strip(), "/srv/ws/vp-wt-fable-ai2/product/agent_interface/cli/vp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
