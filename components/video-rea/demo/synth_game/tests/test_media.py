# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the actual child supervisor inside the scoped runner."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from demo.synth_game.media import _namespace_pids, _run_child, run_media_tool


class MediaSupervision(unittest.TestCase):
    def test_namespace_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            argv = [sys.executable, "-B", "-c", "from pathlib import Path; Path('ran').touch()"]
            for env in (
                {"AF_PROC_BACKEND": "", "AF_PROC_PIDNS_PROC": "/proc/1/fd/9"},
                {"AF_PROC_BACKEND": "pidns", "AF_PROC_PIDNS_PROC": "/proc"},
                {"AF_PROC_BACKEND": "pidns", "AF_PROC_PIDNS_PROC": ""},
            ):
                with self.subTest(env=env), patch.dict(os.environ, env):
                    with self.assertRaisesRegex(RuntimeError, "PID namespace"):
                        _run_child(argv, cwd=work, timeout=1)
                    self.assertFalse((work / "ran").exists())
            # Exercise the namespace identity check even when the environment is canonical.
            pid = os.getpid()
            for statuses in (([pid + 1], [100, pid + 1]), ([pid], [pid]), ([pid], [100, pid + 1])):
                with self.subTest(statuses=statuses), patch("demo.synth_game.media._namespace_pids",
                                                           side_effect=statuses):
                    with self.assertRaisesRegex(RuntimeError, "PID namespace"):
                        _run_child(argv, cwd=work, timeout=1)
                    self.assertFalse((work / "ran").exists())
            with patch("demo.synth_game.media._namespace_pids", side_effect=FileNotFoundError):
                with self.assertRaisesRegex(RuntimeError, "Cannot read.*PID namespace"):
                    _run_child(argv, cwd=work, timeout=1)
            self.assertFalse((work / "ran").exists())

    def test_missing_namespace_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / "self").mkdir()
            (proc / "self/status").write_text("Name: python3\n")
            with self.assertRaisesRegex(ValueError, "NSpid missing"):
                _namespace_pids(proc)

    def test_invalid_timeout_does_not_launch_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            for timeout in (0, -1, float("inf"), float("nan")):
                with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "Child timeout"):
                    _run_child([sys.executable, "-B", "-c", "from pathlib import Path; Path('ran').touch()"],
                               cwd=work, timeout=timeout)
            self.assertFalse((work / "ran").exists())

    def test_timeout_kills_and_reaps_fake_child(self):
        code = """import json, os, time
from pathlib import Path
Path("child.json").write_text(json.dumps([os.getpid(), os.getsid(0), os.getpgrp()]))
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_child([sys.executable, "-B", "-c", code], cwd=work, timeout=1)
            pid, sid, pgid = json.loads((work / "child.json").read_text())
            self.assertEqual(pid, sid)
            self.assertEqual(pid, pgid)
            self.assertNotEqual(pgid, os.getpgrp())
            self.assertFalse(Path(f"/proc/{pid}").exists(), "child must be reaped")

    def test_child_error_and_tool_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with self.assertRaisesRegex(ValueError, "Unsupported media tool"):
                run_media_tool("bash", [], cwd=work, timeout=1)
            with self.assertRaisesRegex(RuntimeError, r"rc 7.*fake failure"):
                _run_child([sys.executable, "-B", "-c",
                            "import sys; sys.stderr.write('fake failure'); sys.exit(7)"],
                           cwd=work, timeout=1)
