"""Qualification freeze integrity; native measurements live in external receipts."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from asset_factory import core
from providers.coacd import adapter, contracts, qualify, worker
from test_w3_collision_gates import box, glb, gate_policy, request


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.glb"
        self.source.write_bytes(glb([box((-1, 0, 0))]))
        self.runtime = {"coacd_available": True, "coacd_version": "1.0.14",
                        "code_sha256": "a" * 64}
        self.patch = mock.patch.object(worker, "runtime_info", return_value=self.runtime)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cases = [{"name": "fixture", "request": request(self.source), "policy": gate_policy()}]

    def assertRefusal(self, code, call, *args):
        with self.assertRaises(contracts.Refusal) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)

    def test_frozen_seed_mutation_red_then_original_green(self):
        plan = qualify.freeze(self.cases)
        digest = core.sha256_json(plan)
        changed = copy.deepcopy(plan)
        changed["cases"][0]["request"]["params"]["seed"] += 1
        self.assertRefusal("freeze_stale", qualify.check_plan, changed, digest)
        qualify.check_plan(plan, digest)
        self.assertIs(plan["production_ready"], False)

    def test_runtime_drift_red_then_pinned_runtime_green(self):
        plan = qualify.freeze(self.cases)
        digest = core.sha256_json(plan)
        with mock.patch.object(worker, "runtime_info", return_value=dict(self.runtime, code_sha256="b" * 64)):
            self.assertRefusal("runtime_code_stale", qualify.check_plan, plan, digest)
        qualify.check_plan(plan, digest)

    def test_wrapper_drift_is_stale(self):
        plan = qualify.freeze(self.cases)
        with mock.patch.object(adapter, "toolchain_identity", return_value="b" * 64):
            self.assertRefusal("toolchain_stale", qualify.check_plan, plan, core.sha256_json(plan))

    def test_source_drift_preserves_existing_output(self):
        plan = qualify.freeze(self.cases)
        output = self.root / "existing"
        output.mkdir()
        marker = output / "marker"
        marker.write_bytes(b"caller bytes")
        self.source.write_bytes(b"changed")
        self.assertRefusal("source_stale", qualify.run_case, plan, core.sha256_json(plan), "fixture", output)
        self.assertEqual(marker.read_bytes(), b"caller bytes")
        self.assertEqual(sorted(p.name for p in output.iterdir()), ["marker"])

    def test_missing_runtime_and_duplicate_cases_refused(self):
        with mock.patch.object(worker, "runtime_info", return_value=dict(self.runtime, coacd_available=False)):
            self.assertRefusal("coacd_unavailable", qualify.freeze, self.cases)
        self.assertRefusal("case_name_invalid", qualify.freeze, self.cases * 2)

    def test_worker_qualification_requires_exact_freeze_runtime(self):
        job = adapter._job("qualify", self.source.read_bytes(), gate_policy(),
                           params=self.cases[0]["request"]["params"])
        job["qualification_runtime"] = dict(self.runtime, code_sha256="b" * 64)
        (self.root / "source.glb").write_bytes(self.source.read_bytes())
        with mock.patch.object(worker, "native_decompose") as native:
            rec = worker.measure(job, self.root)
        self.assertEqual((rec["rc"], rec["reason_codes"]), (4, ["runtime_code_stale"]))
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
