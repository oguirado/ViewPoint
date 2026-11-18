"""Adversarial-review regressions; synthetic backends only."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_f0u3_scheduler import FakeBackend, GIB, ROOT, inputs, observation, request, s


class ReviewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "resident.lease"
        self.req = request(self.path)

    async def test_late_start_handle_is_stopped_and_persisted(self):
        backend = FakeBackend()
        start = backend.start
        stop = backend.stop
        recorded = []

        async def late_start(req, deadline):
            handle = await start(req, deadline)
            # A blocking backend completes after the absolute deadline.
            time.sleep(max(0, (deadline - time.monotonic_ns()) / 10**9) + .01)
            return handle

        async def checked_stop(handle, deadline):
            recorded.append(json.loads(self.path.read_text()))
            await stop(handle, deadline)

        backend.start, backend.stop = late_start, checked_stop
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 6)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertEqual(receipt["handle"], "owned-fixture")
        self.assertFalse(backend.active)
        self.assertFalse(self.path.exists())
        self.assertEqual(recorded[0]["handle"], "owned-fixture")

    async def test_cancelled_start_returns_owned_handle_during_cleanup(self):
        backend = FakeBackend()
        entered = asyncio.Event()
        start = backend.start

        async def late_start(req, deadline):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                return await start(req, deadline)

        backend.start = late_start
        task = asyncio.create_task(s.execute(self.req, backend))
        await entered.wait()
        task.cancel()
        receipt = await task
        self.assertEqual(receipt["operation_rc"], 6)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertEqual(receipt["handle"], "owned-fixture")
        self.assertFalse(backend.active)
        self.assertFalse(self.path.exists())

    async def test_cancelled_admission_observer_unwinds_before_unlink(self):
        backend = FakeBackend()
        entered = asyncio.Event()
        unwound = asyncio.Event()

        async def observe(handle, deadline):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                for _ in range(8):
                    await asyncio.sleep(0)
                unwound.set()
                raise

        backend.observe = observe
        task = asyncio.create_task(s.execute(self.req, backend))
        await entered.wait()
        task.cancel()
        receipt = await task
        self.assertEqual(receipt["operation_rc"], 6)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertTrue(unwound.is_set())
        self.assertEqual(backend.starts, 0)
        self.assertFalse(self.path.exists())

    async def test_release_contract_errors_are_errors_and_fence(self):
        for mode in ("stop_value", "malformed_observation"):
            with self.subTest(mode=mode):
                self.path = Path(self.tmp.name) / (mode + ".lease")
                self.req = request(self.path)
                backend = FakeBackend()
                if mode == "stop_value":
                    async def stop(handle, deadline):
                        return "not-None"
                    backend.stop = stop
                else:
                    backend.release_changes = {"unexpected": True}
                receipt = await s.execute(self.req, backend)
                self.assertEqual(receipt["operation_rc"], 0)
                self.assertEqual(receipt["cleanup_rc"], 2)
                self.assertEqual(receipt["rc"], 2)
                self.assertIn("backend_contract", receipt["reason_codes"])
                self.assertTrue(self.path.exists())
                self.path.unlink()

    async def test_backend_operation_code_survives_ceiling_failure(self):
        backend = FakeBackend(result={"rc": 4, "status": "STALE", "reason_codes": ["provider_stale"],
                                      "peak_rss_bytes": 8 * GIB + 1})
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 4)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertEqual(receipt["rc"], 3)  # Frozen worst_rc remains authoritative.
        self.assertIn("ceiling_exceeded", receipt["reason_codes"])

    async def test_all_admission_absences_survive_receipt(self):
        backend = FakeBackend()
        backend.observation_changes = {"host_mem_available_bytes": None, "cgroup_headroom_bytes": None}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 3)
        self.assertEqual(receipt["reason_codes"], ["cgroup_unknown", "memavailable_unknown"])
        self.assertEqual(backend.starts, 0)

    async def test_full_lifecycle_sample_ids_cannot_replay(self):
        backend = FakeBackend()
        backend.release_changes = {"sample_id": "sample-1"}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 0)
        self.assertEqual(receipt["cleanup_rc"], 2)
        self.assertTrue(self.path.exists())

    async def test_blocked_provider_never_starts(self):
        stage, provider, factory, decision = inputs()
        provider["runnable"] = False
        decision["provider_sha256"] = s.core.sha256_json(provider)
        req = s.build_request(stage, provider, factory, decision, lease_path=self.path)
        backend = FakeBackend()
        receipt = await s.execute(req, backend)
        self.assertEqual(receipt["rc"], 5)
        self.assertEqual(receipt["reason_codes"], ["provider_blocked"])
        self.assertEqual(backend.starts, 0)

    async def test_contradictory_decision_never_starts(self):
        for key, value in (("rc", 5), ("verdict", "FAIL")):
            req = deepcopy(self.req)
            req["evidence"]["decision"][key] = value
            req["decision_sha256"] = s.core.sha256_json(req["evidence"]["decision"])
            backend = FakeBackend()
            receipt = await s.execute(req, backend)
            self.assertEqual(receipt["rc"], 2)
            self.assertIn("request_invalid", receipt["reason_codes"])
            self.assertEqual(backend.starts, 0)

    async def test_wrong_decision_identity_never_starts(self):
        for key, value in (("provider_id", "other"), ("operation", "render"),
                           ("jurisdiction", "GB"), ("commercial_use", True)):
            req = deepcopy(self.req)
            req["evidence"]["decision"][key] = value
            req["decision_sha256"] = s.core.sha256_json(req["evidence"]["decision"])
            backend = FakeBackend()
            receipt = await s.execute(req, backend)
            self.assertEqual(receipt["operation_rc"], 4)
            self.assertEqual(receipt["reason_codes"], ["decision_stale"])
            self.assertEqual(backend.starts, 0)

    async def test_post_start_inventory_mismatch_is_error(self):
        backend = FakeBackend()
        backend.running_changes = {"residents": []}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 2)
        self.assertIn("backend_contract", receipt["reason_codes"])
        self.assertEqual(backend.starts, 1)
        self.assertNotIn(("run", None), backend.events)
        self.assertEqual(receipt["cleanup_rc"], 0)

    async def test_nonstring_handle_is_error_and_fences(self):
        backend = FakeBackend()
        async def bad_start(req, deadline):
            backend.starts += 1
            return 42
        backend.start = bad_start
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 2)
        self.assertIn("backend_contract", receipt["reason_codes"])
        self.assertEqual(receipt["cleanup_rc"], 2)
        self.assertEqual(backend.starts, 1)
        self.assertNotIn(("run", None), backend.events)
        self.assertTrue(self.path.exists())


class ReviewHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.req = request(self.root / "resident.lease")
        self.source = self.root / "request.json"
        self.source.write_text(s.core.canonical_json(self.req))
        self.output = self.root / "receipt.json"
        self.output.write_bytes(b"sentinel")

    def cli(self, backend, output):
        stdout = io.StringIO()
        with patch.object(s, "_backend", return_value=backend), redirect_stdout(stdout):
            rc = s.main(["--request", str(self.source), "--receipt", str(output)])
        return rc, json.loads(stdout.getvalue())

    def test_cli_aliases_and_missing_parent_refuse_before_start(self):
        hardlink = self.root / "hardlink.json"
        hardlink.hardlink_to(self.source)
        for output in (self.source, Path(self.req["lease_path"]), hardlink,
                       self.root / "absent" / "receipt.json"):
            backend = FakeBackend()
            before = self.source.read_bytes()
            rc, result = self.cli(backend, output)
            self.assertEqual(rc, 2)
            self.assertEqual(backend.starts, 0)
            self.assertIn("request_invalid", result["reason_codes"])
            self.assertEqual(self.source.read_bytes(), before)

    def test_cli_publication_failure_has_distinct_reason(self):
        with patch.object(s.core, "atomic_write_json", side_effect=OSError("injected")):
            rc, result = self.cli(FakeBackend(), self.output)
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason_codes"], ["publish_failed"])
        self.assertEqual(self.output.read_bytes(), b"sentinel")

    def test_zero_peak_cannot_prove_admission(self):
        req = request(self.root / "resident.lease", peak=0)
        self.assertEqual(s.admit(req, observation()), s._result(3, "peak_unmeasured"))

    def test_admit_does_not_reload_schema_files(self):
        with patch.object(s.core, "load_json", side_effect=AssertionError("admit read disk")):
            self.assertEqual(s.admit(self.req, observation())["rc"], 0)

    def test_nested_observation_does_not_duplicate_schema_identity(self):
        schema = json.loads((ROOT / "tools/scheduler/schemas/receipt.schema.json").read_text())
        self.assertNotIn("$id", schema["$defs"]["observation"])
        self.assertNotIn("$schema", schema["$defs"]["observation"])

    def test_mutation_runner_never_writes_live_sources(self):
        path = ROOT / "tests/fixtures/f0u3/mutations.py"
        spec = importlib.util.spec_from_file_location("f0u3_mutations", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        before = runner.SCHEDULER.read_bytes()
        destination = self.root / "mutations.json"

        def child(*args, **kwargs):
            self.assertEqual(runner.SCHEDULER.read_bytes(), before)
            return subprocess.CompletedProcess(args[0], 1, "FAIL: planted\nAssertionError: planted", "")

        with patch.object(sys, "argv", [str(path), str(destination)]), \
             patch.object(runner, "CASES", runner.CASES[:1]), \
             patch.object(runner.subprocess, "run", side_effect=child), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(runner.SCHEDULER.read_bytes(), before)

    def test_all_mutation_controls_are_gated(self):
        harness = ROOT / "tests/fixtures/f0u3/mutations.py"
        source = ROOT / "tools/scheduler/scheduler.py"
        before = source.read_bytes()
        destination = self.root / "all-mutations.json"
        child = subprocess.run([sys.executable, "-I", "-B", str(harness), str(destination)],
                               cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(child.returncode, 0, child.stdout[-3000:] + child.stderr[-3000:])
        result = json.loads(destination.read_text())
        self.assertGreater(result["expected"], 0)
        self.assertEqual(result["killed"], result["expected"])
        self.assertTrue(all(row["killed"] for row in result["mutants"]))
        self.assertTrue(result["live_unchanged"])
        self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
