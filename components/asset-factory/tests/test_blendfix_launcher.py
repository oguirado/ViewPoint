"""Namespace launch regression: measured limits, real Blender, guarded cleanup.

Invoke through the component's mandatory memory scope and PID namespace.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asset_factory import proc
from providers.birefnet import resources
from tools.blender import bake, glb_capture, topology
from tools.blender.helpers_contracts import GlbError


class BlenderLauncher(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="blendfix-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_blender_inherits_measured_scope_without_user_bus(self):
        observed = resources.cgroup_observation()
        self.assertIsNotNone(observed["memory_max_bytes"])
        self.assertLessEqual(observed["memory_max_bytes"], 8 * 2**30)
        self.assertIsNotNone(observed["memory_high_bytes"])
        self.assertLessEqual(observed["memory_high_bytes"], 6 * 2**30)
        self.assertEqual(observed["swap_max_bytes"], 0)
        self.assertNotEqual(Path("/proc/1/comm").read_text().strip(), "systemd")
        blender = shutil.which("blender")
        self.assertIsNotNone(blender, "real Blender witness is required")
        argv = [blender, "-b", "--factory-startup", "--threads", "1",
                "--python-exit-code", "2", "--python-expr", 'print("BLENDFIX_NATIVE_OK")']
        spawn = proc.popen
        commands = []

        def record_spawn(command, **kwargs):
            commands.append(command)
            self.assertIs(kwargs["start_new_session"], True)
            return spawn(command, **kwargs)

        with mock.patch.object(proc, "popen", side_effect=record_spawn):
            output = self.root / "blender.log"
            self.assertEqual(glb_capture.run_owned(argv, timeout_s=30, cwd=self.root,
                                                   stdout_path=output), 0)
            self.assertIn("BLENDFIX_NATIVE_OK", output.read_text())
            code, log = topology.default_runner(topology.SCOPE_ARGV + argv, 30)
            self.assertEqual(code, 0, log)
            self.assertIn("BLENDFIX_NATIVE_OK", log)
        self.assertEqual(len(commands), 2)
        for command in commands:
            payload, limits = proc._command(command, None)
            self.assertNotIn('systemd-run', payload)
            if limits is not None:
                self.assertEqual(limits, proc.MemoryLimits(6*2**30, 8*2**30, 0))

    def test_unknown_or_partial_limits_must_establish_scope_before_spawn(self):
        limits = {"memory_max_bytes": 8 * 2**30, "memory_high_bytes": 6 * 2**30,
                  "swap_max_bytes": 0, "headroom_bytes": 2**30}
        cases = [None]
        for key, value in (("memory_max_bytes", None), ("memory_high_bytes", None),
                           ("swap_max_bytes", None), ("memory_max_bytes", 9 * 2**30),
                           ("memory_high_bytes", 7 * 2**30), ("swap_max_bytes", 1)):
            cases.append({**limits, key: value})
        for observed in cases:
            for launcher in ("capture", "topology"):
                with self.subTest(observed=observed, launcher=launcher):
                    observation = (mock.patch.object(resources, "cgroup_observation", return_value=observed)
                                   if observed is not None else
                                   mock.patch.object(resources, "cgroup_observation",
                                                     side_effect=resources.ObservationUnavailable("cgroup_unknown")))
                    with observation, mock.patch.object(proc, "popen", side_effect=OSError("spawn intercepted")) as spawn:
                        with self.assertRaisesRegex(OSError, "spawn intercepted"):
                            if launcher == "capture":
                                glb_capture.run_owned(["blender", "-b"], timeout_s=1, cwd=self.root)
                            else:
                                topology.default_runner(topology.SCOPE_ARGV + ["blender", "-b"], 1)
                    command = spawn.call_args.args[0]
                    self.assertEqual(command[0], "systemd-run")
                    for limit in ("MemoryHigh=6G", "MemoryMax=8G", "MemorySwapMax=0"):
                        self.assertIn(limit, command)

    def test_bake_uses_guarded_launcher_without_foreign_signal_path(self):
        foreign = bake.upstream("bake_pbr")
        argv = [shutil.which("blender"), "-b", "--factory-startup", "--threads", "1",
                "--python-expr", 'print("BLENDFIX_BAKE_OK")']
        with mock.patch.object(foreign, "default_runner", side_effect=AssertionError("foreign signal path")):
            code, output, error = bake.scoped_runner(argv, 30)
        self.assertEqual(code, 0, output + error)
        self.assertIn("BLENDFIX_BAKE_OK", output)

    def test_deadline_reaps_owned_group_and_preserves_error(self):
        spawn = proc.popen
        children = []

        def record_spawn(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(proc, "popen", side_effect=record_spawn):
            with self.assertRaises(GlbError) as refusal:
                script = "import os,time;from pathlib import Path;Path('payload.pid').write_text(str(os.getpid()));time.sleep(30)"
                glb_capture.run_owned([sys.executable, "-I", "-c", script],
                                      timeout_s=1, cwd=self.root)
        self.assertEqual(refusal.exception.rc, 6)
        self.assertEqual(str(refusal.exception), "deadline_exceeded")
        self.assertEqual(len(children), 1)
        pid = int((self.root/'payload.pid').read_text())
        self.assertGreater(pid, 1)
        self.assertEqual(children[0].returncode, -signal.SIGKILL)
        self.assertFalse(Path(f"/proc/{pid}").exists())


if __name__ == "__main__":
    unittest.main()
