"""QT2b sampler integration. Run this file directly in an exclusive Delegate=yes scope."""

from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

T2 = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(T2), str(T2.parents[1] / "qa/live_driver")]
from driver import move_private_worker, partition_scope, require_scope
from qt2b import measurement_live as measurement
import telemetry


def sample_for_test(output):
    # Use a single virtual slot so scheduler jitter cannot decide the drain test.
    clock = [0]
    read = measurement.AvailableTelemetry.read

    def read_runner(source):
        if source.cgroup.name != "qd-runner.scope":
            raise AssertionError("telemetry read from outside the measured runner")
        return read(source)

    with ExitStack() as stack:
        stack.enter_context(patch.object(measurement, "WINDOW", measurement.INTERVAL))
        stack.enter_context(patch.object(measurement, "wait_until", lambda due: clock.__setitem__(0, due)))
        stack.enter_context(patch.object(measurement, "now", lambda: clock[0]))
        stack.enter_context(patch.object(telemetry, "now", lambda: clock[0]))
        stack.enter_context(patch.object(measurement.AvailableTelemetry, "read", read_runner))
        if os.environ.get("QT2_TEST_SKIP_MOVE") == "1":
            # Broken worker claims control membership but never leaves the runner.
            stack.enter_context(patch.object(measurement, "place_sampler", side_effect=lambda runner, scope: {
                "path": str(Path(scope["path"]).with_name("qd-control")),
                "affinity": scope["affinity"],
                "parent_limits": {k: scope[k] for k in ("memory_high", "memory_max")},
            }))
        measurement.sample_host(output)


class DelegatedSamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        group = require_scope()
        partition_scope(group, acceptance=True)
        move_private_worker("qd-control", "qd-runner.scope")
        cls.scope, cls.runner = measurement.signature()

    def setUp(self):
        self.run_data = {"scope": self.scope, "samples": []}
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.path = Path(stack.enter_context(TemporaryDirectory())) / "samples.jsonl"
        # The subprocess executes this file's short-window entry under the same real cgroups.
        stack.enter_context(patch.object(measurement, "__file__", __file__))
        stack.enter_context(patch.object(measurement, "WINDOW", measurement.INTERVAL))

    def test_control_sampler_is_live_and_runner_is_exclusive_before_start(self):
        confirm = measurement.confirm_sampler

        def check_ready(worker, scope):
            placement = confirm(worker, scope)
            self.assertIsNone(worker.poll())
            self.assertEqual((self.runner / "cgroup.procs").read_text().split(), [str(os.getpid())])
            control = self.runner.with_name("qd-control")
            self.assertIn(str(worker.pid), (control / "cgroup.procs").read_text().split())
            self.assertEqual(placement["affinity"], sorted(os.sched_getaffinity(0)))
            self.assertEqual(placement["parent_limits"], {k: scope[k] for k in ("memory_high", "memory_max")})
            return placement

        with patch.object(measurement, "confirm_sampler", side_effect=check_ready) as ready:
            with measurement.sampler_process(self.path, self.run_data):
                ready.assert_called_once()
        self.assertEqual([s["slot_index"] for s in self.run_data["samples"]], [0])
        self.assertFalse((self.runner.with_name("qd-control") / "cgroup.procs").read_text().split())

    def test_unmoved_sampler_with_forged_ready_is_refused_before_start(self):
        with patch.dict(os.environ, QT2_TEST_SKIP_MOVE="1"):
            with self.assertRaisesRegex(measurement.EvidenceError, "outside qd-control"):
                with measurement.sampler_process(self.path, self.run_data):
                    self.fail("workload started with a sampler in its exclusive cgroup")
        self.assertFalse(self.path.exists())
        self.assertEqual((self.runner / "cgroup.procs").read_text().split(), [str(os.getpid())])


if __name__ == "__main__":
    # Use the cell's assigned CPUs for this test only. The collector's live CPU gate is unchanged.
    with patch.object(telemetry, "CPUS", [12, 13, 14, 15]):
        if sys.argv[1:2] == ["--sample"]:
            sample_for_test(Path(sys.argv[2]))
        else:
            unittest.main()
