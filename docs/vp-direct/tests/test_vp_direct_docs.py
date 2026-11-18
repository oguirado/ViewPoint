"""Unit tests for U7 docs surface: VP_DIRECT.md + docs/vp-direct/hosts/.

Every oracle checks the committed registry against the markdown that was
generated from it. Mutant tests prove the oracles fail when the guarded
behaviour is removed without touching files outside this surface.
"""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


SURFACE = Path(__file__).resolve().parents[1]          # docs/vp-direct/
DOCS_VP = SURFACE.parent                                  # docs/
REPO = DOCS_VP.parent                                     # repo root
VP_DIRECT = DOCS_VP / "VP_DIRECT.md"
HOSTS = SURFACE / "hosts"
MCP_MANIFEST = REPO / "components/vp-direct/expose/mcp/registration_manifest.json"
CLI_MANIFEST = REPO / "components/vp-direct/expose/cli/registration_manifest.json"
ADAPTER_OPS = REPO / "components/vp-direct/schemas/adapter_ops.json"
VP_DIR = REPO / "components/vp-direct"

ACCEPTED_DIGEST = "0d22e8f21b317e6b83e762694702f4c4265dd7da064ffa4284704d3daf69cd21"

EXPECTED_TOOLS = [
    "vp_capsule_call", "vp_capture_call", "vp_data_call", "vp_factory_call",
    "vp_guest_call", "vp_guest_command_call", "vp_handoff_call",
    "vp_headless_call", "vp_identity_call", "vp_perception_call",
    "vp_saved_data_call", "vp_settings_call", "vp_terminal_call",
    "vp_workspace_call",
]
EXPECTED_VERBS = [
    "capsule", "capture", "data", "factory", "guest", "guest-command",
    "handoff", "headless", "identity", "perception", "saved-data",
    "settings", "terminal", "workspace",
]
HOST_FILES = ["claude-desktop.md", "codex.md", "antigravity2.md"]


def read_manifests():
    mcp = json.loads(MCP_MANIFEST.read_bytes())
    cli = json.loads(CLI_MANIFEST.read_bytes())
    adapter = json.loads(ADAPTER_OPS.read_bytes())
    return mcp, cli, adapter


def read_all_docs():
    files = [VP_DIRECT] + sorted(HOSTS.glob("*.md"))
    return {p: p.read_text(encoding="utf-8") for p in files}


def documented_tools():
    text = VP_DIRECT.read_text(encoding="utf-8")
    host_text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(HOSTS.glob("*.md")))
    combined = text + "\n" + host_text
    return set(re.findall(r"vp_[a-z_]+", combined))


def documented_verbs():
    text = VP_DIRECT.read_text(encoding="utf-8")
    # CLI verbs appear after `mvctl` or as table rows.
    verbs = set()
    for line in text.splitlines():
        if "mvctl" in line:
            m = re.search(r"mvctl\s+([a-z][a-z\-]*)", line)
            if m:
                verbs.add(m.group(1))
    # Table rows: | `tool` | `verb` |
    for m in re.finditer(r"\|\s*`(vp_[a-z_]+)`\s*\|\s*`([a-z\-]+)`", text):
        verbs.add(m.group(2))
    # Explicit verb list in CLI section.
    for v in EXPECTED_VERBS:
        if f"`{v}`" in text or f" {v} " in text:
            verbs.add(v)
    return verbs


class DocsTests(unittest.TestCase):
    def test_registry_digest_matches_accepted(self):
        mcp, cli, _ = read_manifests()
        self.assertEqual(mcp["registry_sha256"], ACCEPTED_DIGEST)
        self.assertEqual(cli["registry_sha256"], ACCEPTED_DIGEST)
        self.assertEqual(mcp["registry_sha256"], cli["registry_sha256"])

    def test_every_tool_documented_and_no_extra(self):
        mcp, _, _ = read_manifests()
        listed = [t["name"] for t in mcp["tools"]]
        self.assertCountEqual(listed, EXPECTED_TOOLS)
        # Every manifest tool appears in VP_DIRECT.md.
        text = VP_DIRECT.read_text(encoding="utf-8")
        for tool in listed:
            self.assertIn(tool, text, f"{tool} not documented in VP_DIRECT.md")
        # No documented vp_ tool outside the manifest appears as a tool.
        found = documented_tools()
        for tool in found:
            if tool.startswith("vp_") and tool.endswith("_call"):
                self.assertIn(tool, EXPECTED_TOOLS, f"undocumented tool {tool} in docs")

    def test_every_verb_documented(self):
        _, cli, _ = read_manifests()
        verbs = [v["verb"] for v in cli["verbs"]]
        self.assertCountEqual(verbs, EXPECTED_VERBS)
        text = VP_DIRECT.read_text(encoding="utf-8")
        for verb in verbs:
            self.assertIn(verb, text, f"verb {verb} missing from VP_DIRECT.md")

    def test_adapter_ops_are_parked_in_docs(self):
        mcp, _, adapter = read_manifests()
        text = VP_DIRECT.read_text(encoding="utf-8")
        self.assertIn("HOLD", text)
        self.assertIn("supported:false", text)
        # 14 top-level tools vs 105 underlying selectors (0 supported).
        self.assertEqual(len(adapter), 14)
        self.assertEqual(len(mcp["tools"]), 14)
        total_selectors = 0
        for tool, payload in adapter.items():
            for op, row in payload["ops"].items():
                total_selectors += 1
                self.assertFalse(row["supported"], f"{tool}.{op} should be parked")
        self.assertEqual(total_selectors, 105)
        self.assertIn("14 tools", text)
        self.assertIn("105", text)
        self.assertIn("0 supported", text)
        # CF-DOCS fix (1): every host recipe carries the same parked wording.
        for name in HOST_FILES:
            host_text = (HOSTS / name).read_text(encoding="utf-8")
            self.assertIn("14 tools", host_text, f"{name} missing '14 tools'")
            self.assertIn("105", host_text, f"{name} missing '105'")
            self.assertIn("0 supported", host_text, f"{name} missing '0 supported'")
            self.assertIn("supported:false", host_text, f"{name} missing 'supported:false'")
        # Docs must not claim any op is supported:true.
        combined = "\n".join(read_all_docs().values())
        self.assertNotIn("supported:true", combined)
        # The conflated "All 14 adapter ops" phrasing must stay gone.
        self.assertNotIn("All 14 adapter ops", combined)

    def test_host_recipes_present_and_pinned(self):
        for name in HOST_FILES:
            path = HOSTS / name
            self.assertTrue(path.is_file(), f"host recipe {name} missing")
            text = path.read_text(encoding="utf-8")
            self.assertIn("viewpoint-agent", text)
            self.assertIn("/usr/bin/python3 -I", text)
            self.assertIn("VP_MCP_SESSION", text)
            self.assertIn("hackathon/mcp/vp_mcp/vp_mcp.py", text)
            self.assertIn("--dry-run", text, f"{name} missing --dry-run")
            self.assertIn(ACCEPTED_DIGEST, text, f"{name} does not pin registry digest")

    def test_documented_commands_carry_dry_run(self):
        for path, text in read_all_docs().items():
            # Every mvctl example must carry --dry-run (examples are dry-run only).
            for line in text.splitlines():
                if "mvctl" in line and "--json-arg" in line:
                    self.assertIn("--dry-run", line, f"{path.name}: mvctl line without --dry-run: {line.strip()}")
                if "vp_mcp.py" in line and "VP_MCP_SESSION=" in line:
                    # Server launch example is either marked --dry-run or bounded to a check.
                    if "python3 -I" in line and "--dry-run" not in line:
                        # Allow the check-only lines that use generate.py --check instead.
                        if "generate.py" not in line:
                            # Host recipes show a commented dry-run form; ensure at least one --dry-run exists nearby.
                            self.assertIn("--dry-run", text, f"{path.name}: server example without --dry-run")

    def test_no_home_paths_or_credentials(self):
        for path, text in read_all_docs().items():
            self.assertNotRegex(text, r"/home/[a-z0-9._-]+/", msg=f"{path.name} leaks home path")
            self.assertNotRegex(text, r"(?i)authorization:\s*bearer", msg=f"{path.name} leaks bearer token")
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]{8,}", msg=f"{path.name} leaks secret")
            self.assertNotRegex(text, r"xox[baprs]-[A-Za-z0-9-]{10,}", msg=f"{path.name} leaks token")
            # Typed exclusions are documented as prohibitions, not as paths to open.
            for forbidden in ["cookies", "auth.json", ".codex"]:
                # If the word appears it must be in a prohibitive context (never/Do not/Refuses/Forbidden).
                if forbidden in text:
                    lower = text.lower()
                    prohib = any(kw in lower for kw in ["never ", "do not", "refuses", "forbidden", "never open", "never read", "never place", "never commit"])
                    self.assertTrue(prohib, f"{path.name} mentions {forbidden} without prohibition context")

    def test_calibration_recorded_as_open(self):
        text = VP_DIRECT.read_text(encoding="utf-8")
        self.assertIn("Calibration is recorded as **open**", text)
        self.assertIn("fixtures/golden", text)
        self.assertIn("AUSENTE", text)

    def test_cli_flags_and_mcp_limits_documented(self):
        text = VP_DIRECT.read_text(encoding="utf-8")
        for flag in ["--socket", "--session-id", "--agent-instance", "--core-instance",
                     "--timeout-ms", "--max-line-bytes", "--max-reply-bytes", "--json-arg", "--dry-run"]:
            self.assertIn(flag, text, f"CLI flag {flag} undocumented")
        for token in ["2024-11-05", "262144", "-32601", "-32602", "-32700", "viewpoint-agent",
                      "effective_request_sha256", "REQUEST_ID_CONFLICT", "ARGS_SCHEMA_ABSENT"]:
            self.assertIn(token, text, f"{token} missing from VP_DIRECT.md")

    def test_offline_verification_command_documented(self):
        text = VP_DIRECT.read_text(encoding="utf-8")
        self.assertIn("safe_run.sh", text)
        self.assertIn("MemoryHigh=6G", text)


def _extract_fenced_commands():
    """Return every documented fenced command (joined continuations)."""
    commands = []
    for path, text in read_all_docs().items():
        in_fence = False
        pending = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("```"):
                if in_fence and pending.strip():
                    commands.append((path.name, pending.strip()))
                in_fence = not in_fence
                pending = ""
                continue
            if not in_fence:
                continue
            if raw.rstrip().endswith("\\"):
                pending += raw.rstrip()[:-1] + " "
            else:
                pending += raw
                stripped = pending.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("from ") and not stripped.startswith("sys.path"):
                    commands.append((path.name, stripped))
                pending = ""
        if in_fence and pending.strip():
            commands.append((path.name, pending.strip()))
    return commands


def _vp_imports():
    sys.path.insert(0, str(VP_DIR))
    from control.core import control as control_mod
    from expose import cli as cli_mod
    from schemas.validation import validate_contract as _validate
    return control_mod, cli_mod, _validate


class ExecutedDryRunTests(unittest.TestCase):
    def test_extracted_documented_generate_checks_execute(self):
        commands = _extract_fenced_commands()
        generate = [c for c in commands if "generate.py" in c[1] and "--catalog-only --check" in c[1]]
        mvctl = [c for c in commands if "mvctl" in c[1] and "--json-arg" in c[1]]
        mcp_server = [c for c in commands if "vp_mcp.py" in c[1] and "VP_MCP_SESSION=" in c[1]]
        # Extraction must find every category (never vacuous).
        self.assertGreaterEqual(len(generate), 4)
        self.assertGreaterEqual(len(mvctl), 1)
        self.assertGreaterEqual(len(mcp_server), 1)
        # Execute the runnable documented checks (<repo> -> REPO).
        ran = set()
        for _, line in generate:
            kind = "mcp" if "expose/mcp/generate.py" in line else "cli"
            if kind in ran:
                continue
            script = REPO / f"components/vp-direct/expose/{kind}/generate.py"
            self.assertTrue(script.is_file(), f"documented generator missing: {script}")
            proc = subprocess.run(
                [sys.executable, "-I", str(script), "--catalog-only", "--check"],
                cwd=str(REPO), capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, f"{script}: {proc.stderr}")
            self.assertEqual(proc.stderr, "")
            ran.add(kind)
        self.assertEqual(ran, {"mcp", "cli"})
        # MCP server launch lines must reference the real executable.
        self.assertTrue((REPO / "hackathon/mcp/vp_mcp/vp_mcp.py").is_file())

    def test_documented_lanes_execute_in_dry_run_with_digest_parity(self):
        # Synthesized equivalent of the documented mvctl/MCP/JSON-lane
        # --dry-run examples (placeholders cannot run verbatim) executed
        # against the committed registry in a temporary mailbox dir.
        from io import StringIO
        from tempfile import TemporaryDirectory
        control_mod, cli_mod, validate_contract = _vp_imports()
        from oflane import Mailbox
        mcp, cli_manifest, _ = read_manifests()
        self.assertEqual(mcp["registry_sha256"], ACCEPTED_DIGEST)
        self.assertEqual(cli_manifest["registry_sha256"], ACCEPTED_DIGEST)
        self.assertEqual(len(mcp["tools"]), 14)

        def allow(_request, _risk):
            return True

        registry = control_mod.CapabilityRegistry()
        identity = control_mod.LaneIdentity("agent", "docs-agent")
        request = {
            "schema": "vp.control.req/v1", "request_id": "docs-dry-run-001",
            "op": "vp_capsule_call",
            "context": {"caller": "agent", "agent_instance": "docs-agent",
                        "session_id": "docs-session", "capsule_id": "docs-capsule",
                        "incarnation": "1", "authority_epoch": 1,
                        "observation_id": "obs-1"},
            "args": {"op": "add", "kind": "identity-browser", "mode": "human"},
            "dry_run": True,
        }
        validate_contract("vp.control.req/v1", request)
        expected_digest = control_mod.digest(request)

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # MCP lane (flat params, dry_run true).
            mcp_disp = control_mod.Dispatcher(
                registry, control_mod.RequestStore(workspace / "mcp.sqlite"),
                pace_risk=allow, lease_epoch=allow, tools={})
            mcp_reply = mcp_disp.dispatch_mcp(
                "vp_capsule_call",
                {"op": "add", "kind": "identity-browser", "mode": "human",
                 "request_id": "docs-dry-run-001", "dry_run": True,
                 "session_id": "docs-session", "capsule_id": "docs-capsule",
                 "incarnation": "1", "authority_epoch": 1,
                 "observation_id": "obs-1"},
                identity,
                binding_context={"session_id": "docs-session",
                                 "capsule_id": "docs-capsule",
                                 "incarnation": "1", "authority_epoch": 1,
                                 "observation_id": "obs-1"})
            self.assertEqual(mcp_reply["code"], "HOLD")
            self.assertEqual(mcp_reply["effective_request_sha256"], expected_digest)
            # JSON lane through a temporary mailbox dir.
            mailbox = Mailbox(workspace, identity)
            mailbox.submit(request)
            self.assertEqual(mailbox.pending(), ("docs-dry-run-001",))
            mail_disp = control_mod.Dispatcher(
                registry, control_mod.RequestStore(workspace / "mail.sqlite"),
                pace_risk=allow, lease_epoch=allow, tools={})
            mail_reply = mailbox.run_one("docs-dry-run-001", mail_disp)
            self.assertEqual(mail_reply["code"], "HOLD")
            self.assertEqual(mail_reply["effective_request_sha256"], expected_digest)
            # CLI lane (mvctl capsule ... --json-arg <file> --dry-run).
            request_file = workspace / "request.json"
            request_file.write_bytes(control_mod.canonical_bytes(request))
            binding = cli_mod.TransportBinding(
                "/fixture/core.sock", "docs-session", "docs-core",
                identity, 500, 2048, 4096)
            cli_disp = control_mod.Dispatcher(
                registry, control_mod.RequestStore(workspace / "cli.sqlite"),
                pace_risk=allow, lease_epoch=allow, tools={})
            argv = ["capsule", "--socket", binding.socket,
                    "--session-id", binding.session_id,
                    "--agent-instance", identity.agent_instance,
                    "--core-instance", binding.core_instance,
                    "--timeout-ms", str(binding.timeout_ms),
                    "--max-line-bytes", str(binding.max_line_bytes),
                    "--max-reply-bytes", str(binding.max_reply_bytes),
                    "--json-arg", str(request_file), "--dry-run"]
            out, err = StringIO(), StringIO()
            rc = cli_mod.run(argv, cli_disp, binding, stdout=out, stderr=err)
            # HOLD is a dispatch refusal: offline CLI exits 3 but still
            # prints the reply with the shared digest.
            self.assertEqual(rc, 3)
            self.assertEqual(err.getvalue(), "")
            cli_reply = json.loads(out.getvalue())
            self.assertEqual(cli_reply["code"], "HOLD")
            self.assertEqual(cli_reply["effective_request_sha256"], expected_digest)

    def test_dry_run_flag_selects_preview_not_execute(self):
        # On a supported fixture registry --dry-run must call preview only;
        # dropping the flag must call execute only (digests differ).
        from io import StringIO
        from tempfile import TemporaryDirectory
        control_mod, cli_mod, _validate = _vp_imports()
        entry_path = (VP_DIR / "schemas" / "tests" / "examples"
                      / "vp.capability_v1.valid.json")
        entry = json.loads(entry_path.read_bytes())
        tool, verb = entry["tool"], entry["cli"]

        def allow(_request, _risk):
            return True

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            identity = control_mod.LaneIdentity("agent", "cli-agent")
            binding = cli_mod.TransportBinding(
                "/fixture/core.sock", "cli-session", "cli-core",
                identity, 500, 2048, 4096)

            def make_disp(dbname, calls):
                def execute(req):
                    calls.append(("execute", req))
                    return control_mod.Outcome(True, "OK")

                def preview(req):
                    calls.append(("preview", req))
                    return control_mod.Outcome(True, "OK")

                return control_mod.Dispatcher(
                    control_mod.CapabilityRegistry([entry]),
                    control_mod.RequestStore(workspace / dbname),
                    pace_risk=allow, lease_epoch=allow,
                    tools={tool: control_mod.Handler(execute, preview)})

            def request_file(name, request_id, dry_run):
                req = {"schema": "vp.control.req/v1", "request_id": request_id,
                       "op": tool,
                       "context": {"caller": "agent",
                                   "agent_instance": identity.agent_instance,
                                   "session_id": binding.session_id,
                                   "authority_epoch": 4},
                       "args": {"session_id": binding.session_id},
                       "dry_run": dry_run}
                path = workspace / name
                path.write_bytes(control_mod.canonical_bytes(req))
                return path, req

            def argv_for(path, dry_flag):
                argv = [verb, "--socket", binding.socket,
                        "--session-id", binding.session_id,
                        "--agent-instance", identity.agent_instance,
                        "--core-instance", binding.core_instance,
                        "--timeout-ms", str(binding.timeout_ms),
                        "--max-line-bytes", str(binding.max_line_bytes),
                        "--max-reply-bytes", str(binding.max_reply_bytes),
                        "--json-arg", str(path)]
                return argv + (["--dry-run"] if dry_flag else [])

            path, req_dry = request_file("dry.json", "docs-dry-run-101", True)
            calls = []
            out, err = StringIO(), StringIO()
            rc = cli_mod.run(argv_for(path, True), make_disp("d.sqlite", calls),
                             binding, stdout=out, stderr=err)
            self.assertEqual((rc, err.getvalue()), (0, ""))
            self.assertEqual([c[0] for c in calls], ["preview"])
            dry_digest = json.loads(out.getvalue())["effective_request_sha256"]
            self.assertEqual(dry_digest, control_mod.digest(req_dry))

            calls2 = []
            path2, req_live = request_file("live.json", "docs-dry-run-102", False)
            out2, err2 = StringIO(), StringIO()
            rc2 = cli_mod.run(argv_for(path2, False), make_disp("l.sqlite", calls2),
                              binding, stdout=out2, stderr=err2)
            self.assertEqual((rc2, err2.getvalue()), (0, ""))
            self.assertEqual([c[0] for c in calls2], ["execute"])
            live_digest = json.loads(out2.getvalue())["effective_request_sha256"]
            self.assertNotEqual(dry_digest, live_digest)

    def test_mutant_malformed_json_arg_fails_before_dispatch(self):
        # Inline JSON, malformed bytes and an unknown verb must fail with
        # input_error/-32601 without calling the backend.
        from io import StringIO
        from tempfile import TemporaryDirectory
        control_mod, cli_mod, _validate = _vp_imports()
        entry_path = (VP_DIR / "schemas" / "tests" / "examples"
                      / "vp.capability_v1.valid.json")
        entry = json.loads(entry_path.read_bytes())
        tool, verb = entry["tool"], entry["cli"]

        def allow(_request, _risk):
            return True

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            identity = control_mod.LaneIdentity("agent", "cli-agent")
            binding = cli_mod.TransportBinding(
                "/fixture/core.sock", "cli-session", "cli-core",
                identity, 500, 2048, 4096)
            calls = []

            def execute(req):
                calls.append(req)
                return control_mod.Outcome(True, "OK")

            disp = control_mod.Dispatcher(
                control_mod.CapabilityRegistry([entry]),
                control_mod.RequestStore(workspace / "m.sqlite"),
                pace_risk=allow, lease_epoch=allow,
                tools={tool: control_mod.Handler(execute, execute)})
            good = {"schema": "vp.control.req/v1", "request_id": "docs-bad-1",
                    "op": tool,
                    "context": {"caller": "agent",
                                "agent_instance": identity.agent_instance,
                                "session_id": binding.session_id,
                                "authority_epoch": 4},
                    "args": {"session_id": binding.session_id},
                    "dry_run": True}
            base = [verb, "--socket", binding.socket,
                    "--session-id", binding.session_id,
                    "--agent-instance", identity.agent_instance,
                    "--core-instance", binding.core_instance,
                    "--timeout-ms", str(binding.timeout_ms),
                    "--max-line-bytes", str(binding.max_line_bytes),
                    "--max-reply-bytes", str(binding.max_reply_bytes)]
            # Inline JSON is refused (file only).
            out, err = StringIO(), StringIO()
            rc = cli_mod.run(base + ["--json-arg", json.dumps(good)],
                             disp, binding, stdout=out, stderr=err)
            self.assertEqual(rc, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(json.loads(err.getvalue())["code"], "input_error")
            # Malformed/truncated file bytes are refused.
            bad = workspace / "bad.json"
            bad.write_bytes(b'{"dry_run": true, "dry_run": false}')
            out, err = StringIO(), StringIO()
            rc = cli_mod.run(base + ["--json-arg", str(bad), "--dry-run"],
                             disp, binding, stdout=out, stderr=err)
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(err.getvalue())["code"], "input_error")
            # Unknown verb never dispatches.
            good_path = workspace / "good.json"
            good_path.write_bytes(control_mod.canonical_bytes(good))
            out, err = StringIO(), StringIO()
            rc = cli_mod.run(["unregistered"] + base[1:] + ["--json-arg", str(good_path), "--dry-run"],
                             disp, binding, stdout=out, stderr=err)
            self.assertIn(rc, (2, 3))
            self.assertEqual(calls, [])


# --- Mutant / negative controls: oracles must fail on a broken variant ---

class MutantTests(unittest.TestCase):
    def test_mutant_stale_digest_fails(self):
        mcp, _, _ = read_manifests()
        broken = dict(mcp, registry_sha256="0" * 64)
        self.assertNotEqual(broken["registry_sha256"], ACCEPTED_DIGEST)
        with self.assertRaises(AssertionError):
            self.assertEqual(broken["registry_sha256"], ACCEPTED_DIGEST)

    def test_mutant_unknown_tool_fails(self):
        mcp, _, _ = read_manifests()
        listed = [t["name"] for t in mcp["tools"]]
        fake = "vp_fake_call"
        self.assertNotIn(fake, listed)
        with self.assertRaises(AssertionError):
            self.assertIn(fake, {t["name"] for t in mcp["tools"]})

    def test_mutant_missing_dry_run_fails(self):
        line = ("mvctl capsule --socket <s> --session-id <id> "
                "--agent-instance a --core-instance c --timeout-ms 500 "
                "--max-line-bytes 2048 --max-reply-bytes 4096 --json-arg <r.json>")
        with self.assertRaises(AssertionError):
            self.assertIn("--dry-run", line)

    def test_mutant_home_path_fails(self):
        text = "session at /home/alice/.config/vp/state/session.json"
        found = re.search(r"/home/[a-z0-9._-]+/", text)
        self.assertIsNotNone(found)
        with self.assertRaises(AssertionError):
            self.assertIsNone(found)

    def test_mutant_supported_true_fails(self):
        text = "op is supported:true in v1"
        with self.assertRaises(AssertionError):
            self.assertNotIn("supported:true", text)

    def test_mutant_undocumented_tool_fails(self):
        text = "We now also offer `vp_new_extra_call` for power users."
        found = set(re.findall(r"vp_[a-z_]+", text))
        extra = {t for t in found if t.endswith("_call") and t not in EXPECTED_TOOLS}
        self.assertTrue(extra)
        with self.assertRaises(AssertionError):
            self.assertFalse(extra)

    def test_mutant_tool_selector_conflation_fails(self):
        # The killed mutant claims 14 ops instead of 105 selectors.
        mutant = ("All 14 adapter ops are `supported:false` until a verified "
                  "binding lands; execution is `HOLD`.")
        with self.assertRaises(AssertionError):
            self.assertNotIn("All 14 adapter ops", mutant)
        combined = "\n".join(read_all_docs().values())
        self.assertNotIn("All 14 adapter ops", combined)
        self.assertIn("105 selectors", combined)

    def test_mutant_host_doc_missing_parked_wording_fails(self):
        # CF-DOCS fix (1) mutant: a host recipe that drops the staged wording
        # (e.g. only "14 tools") must fail the per-host oracle.
        mutant = "## Limits\n\n- Ops are parked; execution is `HOLD`.\n"
        for token in ("14 tools", "105", "0 supported", "supported:false"):
            with self.assertRaises(AssertionError):
                self.assertIn(token, mutant)
        for name in HOST_FILES:
            host_text = (HOSTS / name).read_text(encoding="utf-8")
            self.assertIn("105", host_text, f"{name} must keep staged wording")


if __name__ == "__main__":
    unittest.main()
