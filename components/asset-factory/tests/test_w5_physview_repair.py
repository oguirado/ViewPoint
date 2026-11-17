"""Adversarial physics/collision regressions; synthetic oracles are labelled."""
from asset_factory import proc as af_proc
from copy import deepcopy
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asset_factory import core
from providers.coacd import adapter, geometry, worker
from tools.physics import api, gates
from tools.physics.fixtures import fall_fixture
from test_w3_collision_gates import box, gate_policy, glb
from test_w3_physics_contracts import synthetic_trace


class CollisionReviewRegressions(unittest.TestCase):
    def test_exact_surface_identity_certificate_and_changed_surface_refusal(self):
        original = box((-1, 0, 0))
        reordered = original.copy()
        reordered.faces = reordered.faces[::-1]
        source, candidate = glb([original]), glb([reordered])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "source.glb").write_bytes(source)
            (folder / "collision.glb").write_bytes(candidate)
            rec = worker.measure(adapter._job("gate", source, gate_policy(), candidate), folder)
        self.assertEqual((rec["rc"], rec["verdict"]), (0, "PASS"))
        self.assertEqual(rec["metrics"]["volume_bound_method"], "exact_surface_identity")
        self.assertEqual(rec["metrics"]["lost_volume_upper_ppm"], 0)
        self.assertEqual(rec["metrics"]["extra_volume_upper_ppm"], 0)
        self.assertIsNone(rec["metrics"]["volume_uncertainty_ppm"])
        moved = original.copy()
        moved.vertices[0][0] += .00000001
        self.assertFalse(geometry.exact_surface_identity([original], [moved]))
        # Even equal surfaces cannot bypass exact convexity via a tolerant
        # supporting-plane check (this corner is moved into its own cube).
        dented = original.copy()
        dented.vertices[0] += [.1, .1, .1]
        self.assertFalse(geometry.exact_surface_identity([dented], [dented]))

    def test_underestimated_truncation_cannot_pass_without_volume_bound(self):
        # Reviewer fixture: analytic loss 20000 ppm [CITED: box volume ratio],
        # but this fixed seed reports only 7813 ppm [MEASURED in red run].
        source = glb([box((-1, 0, 0))])
        candidate = glb([box((-1.01, 0, 0), (.98, 1, 1))])
        policy = gate_policy(sample_count=256, sampling_seed=3)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "source.glb").write_bytes(source)
            (folder / "collision.glb").write_bytes(candidate)
            record = worker.measure(adapter._job("gate", source, policy, candidate), folder)
        self.assertLess(record["metrics"]["lost_volume_ppm"], 10000)
        self.assertIsNone(record["metrics"]["volume_uncertainty_ppm"])
        self.assertEqual((record["rc"], record["verdict"]), (3, "CANNOT_SEE"))
        self.assertEqual(record["reason_codes"], ["volume_bound_unavailable"])

    def test_unknown_worker_operation_is_structured_error_before_measurement(self):
        source = glb([box((-1, 0, 0))])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in ("source.glb", "collision.glb"):
                (folder / name).write_bytes(source)
            job = adapter._job("UNRECOGNIZED_OPERATION", source, gate_policy(), source)
            with mock.patch.object(worker, "runtime_info", wraps=worker.runtime_info) as runtime:
                record = worker.measure(job, folder)
            self.assertEqual((record["rc"], record["verdict"]), (2, "ERROR"))
            self.assertEqual(record["reason_codes"], ["operation_invalid"])
            runtime.assert_not_called()
            self.assertEqual((folder / "collision.glb").read_bytes(), source)

    def test_closed_worker_job_rejects_bad_containers_and_unknown_fields(self):
        source = glb([box((-1, 0, 0))])
        valid = adapter._job("gate", source, gate_policy(), source)
        for job in (None, [], {**valid, "extra": True}, {**valid, "params": {}},
                    {**valid, "source_sha256": None}, {**valid, "toolchain_sha256": True}):
            with self.subTest(job=job), tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(worker, "runtime_info") as runtime:
                    rec = worker.measure(job, Path(tmp))
                self.assertEqual((rec["rc"], rec["verdict"]), (2, "ERROR"))
                runtime.assert_not_called()

    def test_worker_cli_unknown_operation_preserves_artifacts(self):
        source = glb([box((-1, 0, 0))])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "source.glb").write_bytes(source)
            (folder / "collision.glb").write_bytes(source)
            (folder / "hull_0000.obj").write_bytes(b"caller-owned hull")
            core.atomic_write_json(folder / "job.json", adapter._job(
                "UNRECOGNIZED_OPERATION", source, gate_policy(), source))
            child = subprocess.run([sys.executable, "-B", "-m", "providers.coacd.worker", tmp],
                                   capture_output=True, timeout=20)
            self.assertEqual(child.returncode, 2, child.stderr.decode(errors="replace"))
            record = core.load_json(folder / "worker-receipt.json", max_bytes=2**20)
            self.assertEqual(record["reason_codes"], ["operation_invalid"])
            self.assertEqual((folder / "collision.glb").read_bytes(), source)
            self.assertEqual((folder / "hull_0000.obj").read_bytes(), b"caller-owned hull")


class PhysicsReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.scene, self.protocol = fall_fixture(
            {"path": "synthetic.json", "sha256": "a" * 64, "media_type": "application/json"})
        self.protocol.update(timestep_us=10000, steps=60)
        self.protocol["free_fall"].update(steps=10, tolerance_um=1)

    def traces(self, protocol=None):
        p = self.protocol if protocol is None else protocol
        return [synthetic_trace(self.scene, p, nonce, factor)
                for nonce, factor in zip(("fresh1", "fresh2", "fresh3"), (1, 2, 1))]

    def evaluate(self, traces, protocol=None):
        return gates.evaluate(self.scene, self.protocol if protocol is None else protocol, traces,
                              run_ids=["fresh1", "fresh2", "fresh3"],
                              model_sha256s=[t["model_sha256"] for t in traces])

    def test_inflated_normalization_cannot_hide_one_metre_trace_error(self):
        traces = self.traces()
        self.assertEqual(self.evaluate(traces)["rc"], 0)
        traces[1]["samples"][50]["states"][0]["position_um"][0] += 1000000
        red = self.evaluate(traces)
        self.assertEqual((red["rc"], red["reason_codes"]), (5, ["timestep_diverged"]))
        inflated = deepcopy(self.protocol)
        inflated["normalization_length_um"] = 10**12
        # Preserve the adversary's rebinding even though invalid protocols are
        # now refused before MJCF compilation. Scale never changes MJCF bytes.
        rebound = deepcopy(traces)
        for trace in rebound:
            trace["protocol_sha256"] = core.sha256_json(inflated)
        refused = self.evaluate(rebound, inflated)
        self.assertEqual((refused["rc"], refused["verdict"]), (2, "ERROR"))

    def test_malformed_trace_containers_return_structured_error(self):
        for bad in (None, 42, "bad", {}, True, [None], [42], ["bad"]):
            with self.subTest(container=repr(bad)):
                record = gates.evaluate(self.scene, self.protocol, bad,
                                        run_ids=[], model_sha256s=[])
                self.assertEqual((record["rc"], record["verdict"]), (2, "ERROR"))
                self.assertEqual(record["trace_sha256s"], [])
        self.assertEqual(self.evaluate(self.traces())["rc"], 0)

    def test_absolute_displacement_guard_survives_relative_oracle_mutant(self):
        traces = self.traces()
        traces[1]["samples"][50]["states"][0]["position_um"][0] += 1000000
        with mock.patch.object(gates, "_error_ppm", return_value=0):
            red = self.evaluate(traces)
        self.assertEqual((red["rc"], red["reason_codes"]), (5, ["timestep_diverged"]))
        self.assertEqual(red["metrics"]["trajectory_displacement_um"], 1000000)
        self.assertEqual(self.evaluate(self.traces())["rc"], 0)

    def test_successful_parent_does_not_leave_executing_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "descendant.pid"
            code = ("import os,time; from pathlib import Path; child=os.fork(); "
                    f"(Path({str(pidfile)!r}).write_text(str(os.getpid())),time.sleep(30)) "
                    "if child==0 else time.sleep(0.2)")
            pid = None
            try:
                rc, _ = api.run_child([sys.executable, "-B", "-c", code],
                                      timeout_s=3, cwd=api.COMPONENT_ROOT)
                self.assertEqual(rc, 0)
                pid = int(pidfile.read_text())
                deadline = time.monotonic() + 1
                state = "unknown"
                while time.monotonic() < deadline:
                    stat = Path(f"/proc/{pid}/stat")
                    try:
                        state = stat.read_text().rsplit(")", 1)[1].split()[0]
                    except FileNotFoundError:
                        state = "gone"
                    if state in ("gone", "Z", "X"):
                        break
                    time.sleep(.01)
                self.assertIn(state, ("gone", "Z", "X"))
            finally:
                if pid is None and pidfile.exists():
                    pid = int(pidfile.read_text())
                if pid is not None:
                    stat = Path(f'/proc/{pid}/stat')
                    self.assertTrue(not stat.exists() or stat.read_text().rsplit(')',1)[1].split()[0] in ('Z','X'),
                                    'owned child survived cleanup')


if __name__ == "__main__":
    unittest.main()
