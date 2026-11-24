"""End-to-end CLI refusal and flag parsing tests for mvctl v0.2.

Mutation: change _UsageParser.error in agents/metaviewer_agents/mvctl.py to exit 0
or emit input_error -> the rc 2 and usage_error assertions fail.
"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
AGENTS_ROOT = TESTS_DIR.parent
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(AGENTS_ROOT))

import test_mvctl_v02 as cli_fixture
from metaviewer_agents import mvctl
from metaviewer_agents.client import CoreClient
from metaviewer_agents.envelope import make_envelope, validate_envelope


class MvctlV02E2ETests(unittest.TestCase):
    def cli(self, command, *flags, env=None):
        test_env = dict(os.environ, PYTHONPATH=str(ROOT / "agents"), PYTHONDONTWRITEBYTECODE="1")
        if env:
            test_env.update(env)
        return subprocess.run(
            [sys.executable, "-m", "metaviewer_agents.mvctl", command, *flags],
            env=test_env, capture_output=True, text=True, timeout=10
        )

    def test_bare_observe_without_identity_refuses_closed(self):
        """Bare observe without identity/limits returns rc 2, usage_error on stderr, empty stdout."""
        for command in ("observe", "propose", "wait-verify"):
            with self.subTest(command=command):
                res = self.cli(command)
                self.assertEqual(res.returncode, 2, res.stderr)
                self.assertEqual(res.stdout, "")
                err = json.loads(res.stderr.strip())
                self.assertEqual(err["verdict"], "refuse")
                self.assertEqual(err["code"], "usage_error")

    def test_missing_mandatory_flag_refuses_closed(self):
        """Omit mandatory identity flag --agent-instance from valid invocation shape."""
        sock = str(Path(tempfile.gettempdir()) / "nonexistent.sock")
        res = self.cli(
            "observe", "--socket", sock,
            "--session-id", "sess-1", "--core-instance", "core-1",
            "--surface-id", "surf-1"
        )
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(res.stdout, "")
        err = json.loads(res.stderr.strip())
        self.assertEqual(err["code"], "usage_error")

    def test_observe_stamp_export_and_disabled_control(self):
        stages = ["observe_begin", "observe_client_ready", "observe_request_sent",
                  "observe_reply_received", "observe_validated", "observe_returned"]
        for mode in ("off", "relative", "symlink", "missing_parent", "enabled"):
            with self.subTest(mode=mode):
                reply = [[lambda call: {
                    "schema": "mv_tool_result/v0.2",
                    "request_id": call["request_id"],
                    "op": "observe",
                    "status": "ok",
                    "observation": copy.deepcopy(cli_fixture.OBSERVATION),
                }]]
                with cli_fixture.MvctlV02Tests().core(reply) as (sock, calls):
                    trusted = sock.parent / "config.json"
                    trusted.write_text(json.dumps(cli_fixture.config()), encoding="utf-8")
                    destination = sock.parent / "stages.jsonl"
                    sentinel = sock.parent / "sentinel"
                    sentinel.write_text("unchanged")
                    symlink = sock.parent / "symlink"
                    symlink.symlink_to(sentinel)
                    missing = sock.parent / "missing"
                    settings = {"off": "", "relative": "stages.jsonl", "symlink": str(symlink),
                                "missing_parent": str(missing / "stages.jsonl"), "enabled": str(destination)}
                    res = self.cli(
                        "observe", "--config", str(trusted), "--socket", str(sock),
                        "--session-id", "sess-1", "--agent-instance", "agent-1",
                        "--core-instance", "core-1", "--timeout-ms", "2000",
                        "--max-line-bytes", "65536", "--max-reply-bytes", "65536",
                        "--surface-id", "surf-1",
                        env={"MV_OBS_STAGES_PATH": settings[mode]},
                    )
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertEqual(json.loads(res.stdout), cli_fixture.OBSERVATION)
                    self.assertEqual(sentinel.read_text(), "unchanged")
                    self.assertFalse(missing.exists())
                    if mode != "enabled":
                        self.assertFalse(destination.exists())
                        continue
                    self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                    rows = [json.loads(line) for line in destination.read_text().splitlines()]
                    self.assertEqual([row["stage"] for row in rows], stages)
                    self.assertEqual({row["request_id"] for row in rows}, {calls[0]["body"]["request_id"]})
                    self.assertEqual({row["role"] for row in rows}, {"agent"})
                    self.assertTrue(all(row["boot_id"] and row["time_namespace"] and row["pid"] > 0
                                        and row["mono_ns"] > 0 for row in rows))
                    self.assertEqual([row["mono_ns"] for row in rows],
                                     sorted(row["mono_ns"] for row in rows))
                    frame = cli_fixture.OBSERVATION["frame_ref"]
                    self.assertEqual(rows[-1]["frame_id"],
                                     f"surf-1:{frame['generation']}:{frame['sha256']}")

    def test_v02_compat_spelling_accepted_in_flag(self):
        """Leading --v02 is stripped and executes v0.2 observe with rc 0; misplaced --v02 refuses with rc 2."""
        observe_reply = [
            [lambda call: {
                "schema": "mv_tool_result/v0.2",
                "request_id": call["request_id"],
                "op": "observe",
                "status": "ok",
                "observation": copy.deepcopy(cli_fixture.OBSERVATION),
            }]
        ]
        harness = cli_fixture.MvctlV02Tests()
        try:
            with harness.core(observe_reply) as (sock, calls):
                trusted = sock.parent / "config.json"
                trusted.write_text(json.dumps(cli_fixture.config()), encoding="utf-8")
                res = self.cli(
                    "--v02", "observe",
                    "--config", str(trusted),
                    "--socket", str(sock),
                    "--session-id", "sess-1",
                    "--agent-instance", "agent-1",
                    "--core-instance", "core-1",
                    "--timeout-ms", "2000",
                    "--max-line-bytes", "65536",
                    "--max-reply-bytes", "65536",
                    "--surface-id", "surf-1",
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(res.stderr, "")
                self.assertEqual(json.loads(res.stdout), cli_fixture.OBSERVATION)
                self.assertEqual(len(calls), 1)
        except PermissionError:
            with tempfile.TemporaryDirectory(prefix="mvctl-e2e-mem-") as tmp:
                sock = Path(tmp) / "core.sock"
                sock.write_text("", encoding="utf-8")
                sock.chmod(0o600)
                trusted = Path(tmp) / "config.json"
                trusted.write_text(json.dumps(cli_fixture.config()), encoding="utf-8")
                calls = []

                def fake_connect(self_core):
                    self_core._seq = 0
                    return copy.deepcopy(cli_fixture.registration("core")["hello"])

                def fake_call(self_core, envelope_msg):
                    validated = validate_envelope(copy.deepcopy(envelope_msg))
                    calls.append(validated)
                    body = observe_reply[0][0](validated["body"])
                    self_core._seq = getattr(self_core, "_seq", 0) + 1
                    return validate_envelope(
                        make_envelope(
                            msg_id="reply",
                            from_component="core",
                            from_instance="core-1",
                            to_component="agent",
                            to_instance="agent-1",
                            session_id="sess-1",
                            seq=self_core._seq,
                            body=body,
                        )
                    )

                out, err = io.StringIO(), io.StringIO()
                with patch.object(CoreClient, "_check_endpoint", lambda self_c, p: (1, 1)), \
                        patch.object(CoreClient, "connect", fake_connect), \
                        patch.object(CoreClient, "call", fake_call), \
                        patch.object(CoreClient, "close", lambda self_core: None), \
                        contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(err):
                    rc = mvctl.run([
                        "--v02", "observe",
                        "--config", str(trusted),
                        "--socket", str(sock),
                        "--session-id", "sess-1",
                        "--agent-instance", "agent-1",
                        "--core-instance", "core-1",
                        "--timeout-ms", "2000",
                        "--max-line-bytes", "65536",
                        "--max-reply-bytes", "65536",
                        "--surface-id", "surf-1",
                    ])
                self.assertEqual(rc, 0, err.getvalue())
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(json.loads(out.getvalue()), cli_fixture.OBSERVATION)
                self.assertEqual(len(calls), 1)

        # Negative control: --v02 in a non-leading position is rejected with rc 2.
        bad = self.cli("observe", "--v02", "--surface-id", "surf-1")
        self.assertEqual(bad.returncode, 2, bad.stderr)
        self.assertEqual(bad.stdout, "")
        self.assertEqual(json.loads(bad.stderr.strip())["code"], "usage_error")

    def test_missing_session_id_refuses_closed(self):
        """Dropping mandatory --session-id from propose command must be refused."""
        sock = str(Path(tempfile.gettempdir()) / "nonexistent.sock")
        prop = str(Path(tempfile.gettempdir()) / "proposal.json")
        res = self.cli(
            "propose", "--socket", sock,
            "--agent-instance", "agent-1", "--core-instance", "core-1",
            "--json-arg", prop
        )
        self.assertEqual(res.returncode, 2, "Dropping --session-id must yield rc 2 refusal")
        self.assertEqual(res.stdout, "")
        self.assertEqual(json.loads(res.stderr.strip())["code"], "usage_error")


if __name__ == "__main__":
    unittest.main()
