"""Witness the CPU worker's absolute nice target in the scoped runner."""

import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from asset_factory import proc
from providers.birefnet import resources
from tools.mesh import remesh


class WorkerNicenessTests(unittest.TestCase):
    scope = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryHigh=6G",
             "-p", "MemoryMax=8G", "-p", "MemorySwapMax=0"]
    cpu = ["taskset", "-c", "0-15"]
    bounded = {"memory_max_bytes": 8 * 2**30, "memory_high_bytes": 6 * 2**30,
               "swap_max_bytes": 0}

    def test_child_nice_is_absolute_for_callers_at_0_10_and_15(self):
        for parent_nice in (0, 10, 15):
            with (self.subTest(parent_nice=parent_nice),
                  patch.object(resources.os, "getpriority", return_value=parent_nice),
                  patch.object(resources, "cgroup_observation", return_value=self.bounded)):
                argv = resources.bounded_scope_prefix(self.scope)
                self.assertEqual(argv[:-1], self.cpu + ["nice", "-n"])
                self.assertEqual(parent_nice + int(argv[-1]), 15)

        self.assertEqual(os.getpriority(os.PRIO_PROCESS, 0), 15)
        with patch.object(resources, "cgroup_observation", return_value=self.bounded):
            output = subprocess.check_output(
                resources.bounded_scope_prefix(self.scope)
                + [sys.executable, "-B", "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"],
                text=True, timeout=10)
        self.assertEqual(output.strip(), "15")

    def test_new_memory_scope_uses_one_adjusted_nice_prefix(self):
        old_cpu = self.cpu + ["nice", "-n", "15"]
        full_scope = self.scope[:3] + ["--quiet"] + self.scope[4:]
        with (patch.object(resources.os, "getpriority", return_value=10),
              patch.object(resources, "cgroup_observation",
                           side_effect=resources.ObservationUnavailable("cgroup_unknown"))):
            for scope in (self.scope, self.scope + old_cpu):
                with self.subTest(scope=scope):
                    self.assertEqual(resources.bounded_scope_prefix(scope),
                                     full_scope + self.cpu + ["nice", "-n", "5"])

    def test_remesh_fallback_applies_niceness_once(self):
        program = ("import os; "
                   "assert os.getpriority(os.PRIO_PROCESS, 0) == 15; "
                   "print('AF_REMESH_RC 0')")
        with (patch.object(resources, "cgroup_observation",
                          side_effect=resources.ObservationUnavailable("cgroup_unknown")),
              patch.object(proc, "_command", wraps=proc._command) as command):
            self.assertEqual(remesh._worker([sys.executable, "-B", "-c", program],
                                           time.monotonic() + 10), 0)
        payload, limits = proc._command(command.call_args.args[0], None)
        self.assertEqual(payload.count("nice"), 1)
        self.assertEqual(limits, proc.MemoryLimits(6 * 2**30, 8 * 2**30, 0))

    def test_caller_above_target_refuses_instead_of_launching_at_wrong_nice(self):
        with (patch.object(resources.os, "getpriority", return_value=16),
              self.assertRaisesRegex(resources.ObservationUnavailable, "^nice_target_unavailable$")):
            resources.bounded_scope_prefix(self.scope)
