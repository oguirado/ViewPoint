"""Run C's exclusive-runner drain with an offline T2 cell and synthetic CDP readback."""

from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

T2 = Path(__file__).resolve().parents[1]
DIRECT = T2.parents[1]
sys.path[:0] = [str(T2), str(DIRECT), str(DIRECT / "qa/live_driver")]

from perception.lifecycle import LifecycleError
from perception.stream import PerceptionStream
from perception.stream.tests.test_stream import put_screen
from privacy import PrivacyBinding
from qt2b import measurement_live as measurement
import t2_workload


class DrainTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory(dir="/dev/shm")))
        put_screen(root / "Xvfb_screen0", b"red" * 32)
        session = json.loads((DIRECT / "schemas/tests/examples/vp.session_v1.valid.json").read_bytes())
        browser = Mock(incarnation=session["incarnation"])
        browser.display.frame_dir = root
        browser.display.size = (32, 1)
        browser.display.name = ":99"
        browser.child.pid, browser.display.server.pid = -1, -2
        self.runner = {os.getpid()}

        def census():
            return {"scopes": [{"processes": [{"pid": p} for p in self.runner], "current_bytes": 4096}]}

        stack.enter_context(patch("perception.baseline.inventory.collect", side_effect=census))
        stack.enter_context(patch.object(t2_workload, "session_for", return_value=session))
        frames = stack.enter_context(patch.object(t2_workload, "FramePorts"))
        binding = t2_workload.binding_for(browser)
        frames.return_value.current.return_value.privacy_binding.return_value = PrivacyBinding(
            binding["surface_id"], browser.incarnation, 1, "cdp", "synthetic")
        stack.enter_context(patch("perception.stream.PerceptionStream",
                                  side_effect=lambda *a, **kw: PerceptionStream(*a, **kw, workspace_root=root / "rings")))
        self.cell = t2_workload.TimerCell(root / "cell", stack, browser, "/qd-fixture")
        observed = SimpleNamespace(nodes=[{"role": "heading", "name": "Fixture timer page"}],
                                   ax_sha256="a" * 64, ax_stable_sha256="b" * 64)
        stack.enter_context(patch.object(self.cell.cdp, "observe", return_value=observed))
        self.cell.transition("wake")

    def test_sampler_in_runner_blocks_rest_after_verified_operations_then_retry_succeeds(self):
        sampler = os.getpid() + 1
        self.runner.add(sampler)
        expected = sha256(t2_workload.EXPECTED_READBACK).hexdigest()
        for _ in range(measurement.OPERATIONS):
            _, reply = self.cell.observe(Mock())
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["value_sha256"], expected)
        self.assertEqual(self.cell.counts["analyses"], measurement.OPERATIONS)
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.cell.transition("sleep")
        self.assertIsNone(self.cell.published)
        state = self.cell.broker.perception_state(self.cell.binding["surface_id"])
        self.assertEqual(state["state"], "ACTIVE")
        # Only the sampler's placement changes, on this same lifecycle instance.
        self.runner.remove(sampler)
        asleep = self.cell.transition("sleep")
        self.assertEqual(asleep["to"], "ASLEEP")
        self.assertTrue(asleep["complete"])
        self.assertIn("agent-drain", asleep["acks"])
        self.assertEqual(self.cell.transition("wake")["to"], "ACTIVE")

    def test_another_exclusive_pid_still_blocks_rest(self):
        sampler, worker = os.getpid() + 1, os.getpid() + 2
        self.runner.update((sampler, worker))
        self.runner.remove(sampler)
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.cell.transition("sleep")
        self.assertIsNone(self.cell.published)
