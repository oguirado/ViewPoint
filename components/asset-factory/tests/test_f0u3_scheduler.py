"""Synthetic lifecycle witnesses and refusal controls; no models or services."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Isolated Python intentionally excludes cwd/PYTHONPATH; load this owned root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.scheduler import scheduler as s

GIB = 2**30
H = "a" * 64


def inputs():
    policy = {"schema": "af.territory-groups/v1", "countries": ["ES"], "groups": {"EU": ["ES"]}}
    provider = {"schema": "af.provider/v1", "id": "fixture", "slot": "generate", "kind": "model",
                "runnable": True, "revision": "fixture-pinned-revision", "licence": {"code": "MIT", "weights": "MIT", "deps": []},
                "territory_excluded": [], "commercial": True, "gated": False,
                "weights": [{"repo": "synthetic/fixture", "revision": "fixture-weights-revision", "files": [{"path": "fixture", "sha256": H}]}],
                "runtime": {"container": "fixture-only", "ram_peak_gb_measured": 1,
                            "ram_peak_measurement_ref": {"path": "fixture-measurement.json", "sha256": H}}, "known_issues": []}
    factory = {"schema": "af.factory-config/v1", "jurisdiction": "ES", "commercial_use": False,
               "models_path": "synthetic-models", "memory": {"max_resident_models": 1, "ceiling_gb_default": 8, "min_free_gb_before_start": 2},
               "slots": {slot: {"provider": "fixture" if slot == "generate" else "none"} for slot in s.core.SLOTS},
               "gates": {"appearance": {"floor": 4, "target": 7}, "vqa3d_profile": "synthetic-profile"}}
    stage = {"schema": "af.generate/v1", "kind": "request", "slot": "generate", "request_id": "fixture-request",
             "provider_id": provider["id"], "provider_revision": provider["revision"],
             "inputs": [{"role": "image", "path": "fixture.png", "media_type": "image/png", "sha256": H}], "params": {"seed": 42},
             "context": {"jurisdiction": "ES", "commercial_use": False, "policy_sha256": s.core.sha256_json(policy), "toolchain_sha256": H, "timeout_s": 1}}
    decision = {"schema": "af.licence-decision/v1", "rc": 0, "verdict": "PASS", "allowed": True,
                "provider_id": provider["id"], "operation": "generate", "jurisdiction": "ES", "commercial_use": False,
                "monetisable": True, "reason_codes": [], "evaluated_dependencies": [],
                "configuration_sha256": s.core.sha256_json(factory), "provider_sha256": s.core.sha256_json(provider),
                "policy_sha256": stage["context"]["policy_sha256"]}
    return stage, provider, factory, decision


def request(path, **changes):
    stage, provider, factory, decision = inputs()
    for key, value in changes.items():
        if key == "peak":
            provider["runtime"]["ram_peak_gb_measured"] = value
            provider["runtime"]["ram_peak_measurement_ref"] = None if value is None else {"path": "fixture-measurement.json", "sha256": H}
        elif key == "allowed":
            decision.update(allowed=value, rc=0 if value else 5, verdict="PASS" if value else "FAIL")
        elif key == "ceiling":
            factory["memory"]["ceiling_gb_default"] = value
    decision["provider_sha256"] = s.core.sha256_json(provider)
    decision["configuration_sha256"] = s.core.sha256_json(factory)
    return s.build_request(stage, provider, factory, decision, lease_path=Path(path))


def observation(**changes):
    value = {"schema": "af.scheduler-observation/v1", "sample_id": "fixture-sample", "monotonic_ns": time.monotonic_ns(),
             "host_mem_available_bytes": 3 * GIB, "cgroup_headroom_bytes": 3 * GIB,
             "effective_ceiling_bytes": 8 * GIB, "swap_limit_bytes": 0, "residents": [], "absence_reasons": {}}
    value.update(changes)
    for key, item in value.items():
        if item is None:
            value["absence_reasons"][key] = "synthetic-unavailable"
    return value


class FakeBackend:
    def __init__(self, *, run_mode="ok", stop_mode="ok", result=None):
        self.run_mode, self.stop_mode = run_mode, stop_mode
        self.result = {"rc": 0, "status": "OK", "reason_codes": [], "peak_rss_bytes": GIB} if result is None else result
        self.active = False
        self.events = []
        self.samples = 0
        self.starts = 0
        self.observation_changes = {}
        self.running_changes = {}
        self.release_changes = {}
        self.started = asyncio.Event()
        self.running = asyncio.Event()
        self.stopping = asyncio.Event()
        self.unblock = asyncio.Event()
        self.stop_unblock = asyncio.Event()
        self.identity = {"handle": "owned-fixture", "provider_id": "fixture", "provider_revision": "fixture-pinned-revision"}

    async def observe(self, handle, deadline_ns):
        self.samples += 1
        self.events.append(("observe", self.active))
        changes = self.observation_changes if handle is None else (self.running_changes if self.active else self.release_changes)
        values = {"sample_id": "sample-" + str(self.samples), "residents": [self.identity.copy()] if self.active else []}
        values.update(changes)
        return observation(**values)

    async def start(self, request, deadline_ns):
        self.starts += 1
        self.events.append(("start", None))
        self.active = True
        self.started.set()
        return "owned-fixture"

    async def run(self, handle, request, deadline_ns):
        self.events.append(("run", None))
        self.running.set()
        if self.run_mode == "error":
            raise RuntimeError("synthetic failure")
        if self.run_mode == "wait":
            await self.unblock.wait()
        if self.run_mode == "timeout":
            await asyncio.sleep(1.2)
        return deepcopy(self.result)

    async def stop(self, handle, deadline_ns):
        self.events.append(("stop", None))
        self.stopping.set()
        if self.stop_mode == "error":
            raise RuntimeError("synthetic stop failure")
        if self.stop_mode == "wait":
            await self.stop_unblock.wait()
        if self.stop_mode != "noop":
            self.active = False


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "residency.lease"
        self.req = request(self.path)

    def test_build_request_gib_boundary_and_identity(self):
        source = inputs()
        before = deepcopy(source)
        built = s.build_request(*source, lease_path=self.path)
        self.assertEqual(source, before)
        self.assertEqual(built["memory"]["measured_peak_bytes"], GIB)
        self.assertEqual(built["memory"]["ceiling_bytes"], 8 * GIB)
        self.assertEqual(built["decision_sha256"], s.core.sha256_json(source[3]))
        self.assertEqual(built["memory"]["measurement_ref"], source[1]["runtime"]["ram_peak_measurement_ref"])
        built["evidence"]["provider"]["known_issues"].append("changed")
        self.assertEqual(source, before)
        for bad in (True, 1.5, -1, "1"):
            stage, provider, factory, decision = inputs()
            provider["runtime"]["ram_peak_gb_measured"] = bad
            with self.subTest(bad=bad), self.assertRaises(s.core.ContractError):
                s.build_request(stage, provider, factory, decision, lease_path=self.path)

    def test_admission_boundary_bytes(self):
        self.assertEqual(s.admit(self.req, observation())["rc"], 0)
        for key in ("host_mem_available_bytes", "cgroup_headroom_bytes"):
            with self.subTest(key=key):
                result = s.admit(self.req, observation(**{key: 3 * GIB - 1}))
                self.assertEqual(result["rc"], 5)
                self.assertEqual(result["reason_codes"], ["headroom_below_floor"])
        self.assertEqual(s.admit(request(self.path, peak=8), observation(host_mem_available_bytes=10*GIB, cgroup_headroom_bytes=10*GIB))["rc"], 0)
        self.assertEqual(s.admit(request(self.path, peak=9), observation())["rc"], 5)

    def test_missing_peak_or_memavailable_cannot_see(self):
        self.assertEqual(s.admit(self.req, observation())["rc"], 0)
        self.assertEqual(s.admit(request(self.path, peak=None), observation())["reason_codes"], ["peak_unmeasured"])
        for key, code in (("host_mem_available_bytes", "memavailable_unknown"), ("cgroup_headroom_bytes", "cgroup_unknown"),
                          ("residents", "resident_unknown"), ("monotonic_ns", "sample_unknown")):
            with self.subTest(key=key):
                value = observation(**{key: None})
                self.assertEqual(s.admit(self.req, value), s._result(3, code))
                del value[key]
                self.assertEqual(s.admit(self.req, value)["rc"], 2)

    def test_effective_ceiling_verified(self):
        self.assertEqual(s.admit(self.req, observation())["rc"], 0)
        for key, value, rc in (("effective_ceiling_bytes", None, 3), ("swap_limit_bytes", None, 3),
                               ("effective_ceiling_bytes", 9*GIB, 5), ("swap_limit_bytes", 1, 5), ("effective_ceiling_bytes", GIB-1, 5)):
            with self.subTest(key=key, value=value):
                self.assertEqual(s.admit(self.req, observation(**{key: value}))["rc"], rc)

    def test_revision_policy_receipt_binding(self):
        self.assertEqual(s.admit(self.req, observation())["rc"], 0)
        for key in ("stage_request_sha256", "provider_sha256", "decision_sha256"):
            value = deepcopy(self.req)
            value[key] = "b" * 64
            with self.subTest(key=key):
                result = s.admit(value, observation())
                self.assertEqual(result["rc"], 4)
                self.assertEqual(result["reason_codes"], [{"stage_request_sha256": "request_stale", "provider_sha256": "provider_stale", "decision_sha256": "decision_stale"}[key]])
        for key in ("configuration_sha256", "provider_sha256", "policy_sha256"):
            value = deepcopy(self.req)
            value["evidence"]["decision"][key] = "b" * 64
            value["decision_sha256"] = s.core.sha256_json(value["evidence"]["decision"])
            with self.subTest(key=key):
                self.assertEqual(s.admit(value, observation())["rc"], 4)
        for key in ("provider_revision", "context", "memory"):
            value = deepcopy(self.req)
            if key == "context":
                value[key]["timeout_s"] += 1
            elif key == "memory":
                value[key]["measurement_ref"]["sha256"] = "b" * 64
            else:
                value[key] += "-changed"
            with self.subTest(key=key):
                self.assertEqual(s.admit(value, observation())["rc"], 4)

    def test_identity_evidence_required_and_licence_denial_is_metadata(self):
        self.assertEqual(s.admit(self.req, observation())["rc"], 0)
        self.assertEqual(s.admit(request(self.path, allowed=False), observation()), s._result(0))
        del self.req["evidence"]
        self.assertEqual(s.admit(self.req, observation()), s._result(3, "authorization_unproven"))

    def test_closed_integer_only_schema(self):
        s._request(self.req)
        for name, value in (("request", self.req), ("observation", observation())):
            invalid = deepcopy(value)
            invalid["unknown"] = 1
            with self.subTest(name=name), self.assertRaises(s.core.ContractError):
                s._validate(invalid, name)
        for invalid in (True, 1.25, -1):
            value = deepcopy(self.req)
            value["memory"]["min_free_bytes"] = invalid
            self.assertEqual(s.admit(value, observation())["rc"], 2)
        for invalid in (True, 1.25, -1):
            self.assertEqual(s.admit(self.req, observation(host_mem_available_bytes=invalid))["rc"], 2)
        value = observation(host_mem_available_bytes=None)
        value["absence_reasons"] = {}
        self.assertEqual(s.admit(self.req, value)["rc"], 2)
        for key, invalid in (("provider_revision", "main"), ("lease_path", "relative-lease")):
            value = deepcopy(self.req)
            value[key] = invalid
            with self.subTest(key=key), self.assertRaises(s.core.ContractError):
                s._request(value)
        value = deepcopy(self.req)
        value["memory"]["max_resident_models"] = 2
        with self.assertRaises(s.core.ContractError):
            s._request(value)

    def test_canonical_json_independent_oracle(self):
        self.assertEqual(s.core.canonical_json({"z": 1, "a": "é", "b": True}), '{"a":"é","b":true,"z":1}\n')
        with self.assertRaises(s.core.ContractError):
            s.core.canonical_json({"number": 1.5})

    def test_receipt_schema_and_rc_precedence(self):
        self.assertEqual(s.core.worst_rc([0, 0]), 0)
        for codes, expected in (([], 3), ([6, 0], 3), ([4, 0], 3), ([5, 3], 5), ([2, 5], 2), ([1], 2), ([True], 2)):
            self.assertEqual(s.core.worst_rc(codes), expected)
        receipt = s._receipt(self.req, 0)
        s._validate(receipt, "receipt")
        receipt["rc"] = 1
        with self.assertRaises(s.core.ContractError):
            s._validate(receipt, "receipt")
        receipt = s._receipt(self.req, 0)
        receipt["status"] = "OK"
        with self.assertRaises(s.core.ContractError):
            s._validate(receipt, "receipt")
        receipt = s._receipt(self.req, 0)
        receipt["absence_reasons"] = {}
        with self.assertRaises(s.core.ContractError):
            s._validate(receipt, "receipt")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "residency.lease"
        self.req = request(self.path)

    async def test_serial_switch_releases_before_start(self):
        for _ in range(2):
            backend = FakeBackend()
            receipt = await s.execute(self.req, backend)
            self.assertEqual(receipt["rc"], 0)
            self.assertEqual(receipt["status"], "OK")
            self.assertNotIn("verdict", receipt)
            self.assertEqual(receipt["release_observation"]["residents"], [])
            self.assertFalse(self.path.exists())
            self.assertEqual([event[0] for event in backend.events], ["observe", "start", "observe", "run", "stop", "observe"])
            self.assertEqual(receipt["request_sha256"], s.core.sha256_json(self.req))
            self.assertEqual(receipt["peak_rss_bytes"], GIB)

    async def test_legacy_advisory_decision_reaches_execute(self):
        req = request(self.path, allowed=False)
        _, _, _, decision = inputs()
        decision.update(allowed=False, rc=5, verdict="FAIL")
        self.assertEqual(req["decision_sha256"], s.core.sha256_json(decision))
        self.assertEqual(req["context"]["jurisdiction"], decision["jurisdiction"])
        self.assertIs(req["context"]["commercial_use"], decision["commercial_use"])
        backend = FakeBackend()
        receipt = await s.execute(req, backend)
        self.assertEqual(receipt["rc"], 0, receipt)
        self.assertEqual(receipt["request_sha256"], s.core.sha256_json(req))
        self.assertEqual(backend.starts, 1)

    async def test_refusal_never_starts(self):
        cases = [(request(self.path, peak=None), {}, 3),
                 (self.req, {"host_mem_available_bytes": 3*GIB-1}, 5),
                 (self.req, {"residents": [{"handle": "foreign", "provider_id": "other", "provider_revision": "pinned"}]}, 5)]
        for req, changes, expected in cases:
            backend = FakeBackend()
            backend.observation_changes = changes
            with self.subTest(expected=expected, changes=changes):
                receipt = await s.execute(req, backend)
                self.assertEqual(receipt["operation_rc"], expected)
                self.assertEqual(backend.starts, 0)
                self.assertFalse(self.path.exists())

    async def test_exception_cancel_timeout_cleanup(self):
        for mode, expected in (("ok", 0), ("error", 2), ("timeout", 6), ("wait", 6)):
            self.path = Path(self.tmp.name) / (mode + ".lease")
            self.req = request(self.path)
            backend = FakeBackend(run_mode=mode)
            task = asyncio.create_task(s.execute(self.req, backend))
            if mode == "wait":
                await asyncio.wait_for(backend.running.wait(), timeout=3)
                task.cancel()
            receipt = await task
            with self.subTest(mode=mode):
                self.assertEqual(receipt["operation_rc"], expected)
                self.assertEqual(receipt["rc"], 0 if expected == 0 else (2 if expected == 2 else 3))
                self.assertEqual(receipt["cleanup_rc"], 0)
                self.assertIsNotNone(receipt["release_observation"])
                self.assertEqual(receipt["release_observation"]["residents"], [])
                self.assertIn(("stop", None), backend.events)
                self.assertFalse(self.path.exists())

    async def test_cancel_during_cleanup_is_shielded(self):
        backend = FakeBackend(stop_mode="wait")
        task = asyncio.create_task(s.execute(self.req, backend))
        await backend.stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        backend.stop_unblock.set()
        receipt = await task
        self.assertEqual(receipt["operation_rc"], 6)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(receipt["release_observation"]["residents"], [])

    async def test_stop_failure_blocks_next_start(self):
        backend = FakeBackend(stop_mode="error")
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 0)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertEqual(receipt["rc"], 3)
        self.assertTrue(self.path.exists())
        original = self.path.read_bytes()
        next_backend = FakeBackend()
        next_receipt = await s.execute(self.req, next_backend)
        self.assertEqual(next_receipt["rc"], 5)
        self.assertEqual(next_receipt["reason_codes"], ["lease_held"])
        self.assertEqual(next_backend.starts, 0)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_gate_rejects_noop_cleanup_mutant(self):
        backend = FakeBackend(stop_mode="noop")
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 0)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertEqual(receipt["rc"], 3)
        self.assertIn("release_unproven", receipt["reason_codes"])
        self.assertTrue(self.path.exists())

    async def test_readiness_and_release_require_evidence(self):
        backend = FakeBackend()
        backend.running_changes = {"effective_ceiling_bytes": None}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 3)
        self.assertNotIn(("run", None), backend.events)
        self.assertEqual(receipt["cleanup_rc"], 0)
        backend = FakeBackend()
        backend.release_changes = {"residents": None}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertTrue(self.path.exists())

    async def test_invalid_backend_rc_is_error(self):
        for code in (1, True, -1, "0"):
            backend = FakeBackend(result={"rc": code, "status": "OK", "reason_codes": []})
            with self.subTest(code=code):
                receipt = await s.execute(self.req, backend)
                self.assertEqual(receipt["rc"], 2)
                self.assertEqual(receipt["cleanup_rc"], 0)
                self.assertFalse(self.path.exists())
        backend = FakeBackend(result={"rc": 5, "status": "OK", "reason_codes": []})
        self.assertEqual((await s.execute(self.req, backend))["rc"], 2)

    async def test_runtime_ceiling_exceeded(self):
        self.assertEqual((await s.execute(self.req, FakeBackend()))["rc"], 0)
        backend = FakeBackend(result={"rc": 0, "status": "OK", "reason_codes": [], "peak_rss_bytes": 8*GIB+1})
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 6)
        self.assertEqual(receipt["cleanup_rc"], 0)
        self.assertEqual(receipt["rc"], 3)
        self.assertIn("ceiling_exceeded", receipt["reason_codes"])

    async def test_operation_and_cleanup_codes_preserved(self):
        for operation, status, cleanup, expected in ((0, "OK", "ok", 0), (5, "FAIL", "error", 5),
                                                     (3, "INCOMPLETE", "ok", 3), (6, "BUDGET_EXCEEDED", "ok", 3)):
            backend = FakeBackend(stop_mode=cleanup, result={"rc": operation, "status": status, "reason_codes": []})
            receipt = await s.execute(self.req, backend)
            with self.subTest(operation=operation, cleanup=cleanup):
                self.assertEqual(receipt["operation_rc"], operation)
                self.assertEqual(receipt["cleanup_rc"], 3 if cleanup == "error" else 0)
                self.assertEqual(receipt["rc"], expected)
                self.assertIn("peak_rss_bytes", receipt["absence_reasons"])
            if self.path.exists():
                self.path.unlink()
        backend = FakeBackend(run_mode="error", stop_mode="error")
        receipt = await s.execute(self.req, backend)
        self.assertEqual((receipt["rc"], receipt["operation_rc"], receipt["cleanup_rc"]), (2, 2, 3))

    async def test_stale_decision_never_starts(self):
        self.req["decision_sha256"] = "b" * 64
        backend = FakeBackend()
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 4)
        self.assertEqual(receipt["rc"], 3)
        self.assertEqual(receipt["reason_codes"], ["decision_stale"])
        self.assertEqual(backend.starts, 0)
        self.assertFalse(self.path.exists())

    async def test_late_cancel_resistant_work_keeps_lease(self):
        backend = FakeBackend()
        unblock = asyncio.Event()
        async def stubborn_run(handle, request, deadline_ns):
            backend.running.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await unblock.wait()
            return deepcopy(backend.result)
        backend.run = stubborn_run
        task = asyncio.create_task(s.execute(self.req, backend))
        await backend.running.wait()
        task.cancel()
        try:
            receipt = await task
            self.assertEqual(receipt["operation_rc"], 6)
            self.assertEqual(receipt["cleanup_rc"], 3)
            self.assertTrue(self.path.exists())
        finally:
            unblock.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def test_backend_cannot_mutate_bound_request(self):
        original = deepcopy(self.req)
        backend = FakeBackend()
        run = backend.run
        async def mutate(handle, req, deadline_ns):
            req["provider_revision"] = "changed-by-backend"
            return await run(handle, req, deadline_ns)
        backend.run = mutate
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["rc"], 0)
        self.assertEqual(receipt["request_sha256"], s.core.sha256_json(original))
        self.assertEqual(self.req, original)

    async def test_shared_lease_excludes_second_process(self):
        backend = FakeBackend(run_mode="wait")
        owner = asyncio.create_task(s.execute(self.req, backend))
        await backend.running.wait()
        program = ("import sys,asyncio,json;sys.path.insert(0,sys.argv[1]);"
                   "from test_f0u3_scheduler import FakeBackend,s; b=FakeBackend();"
                   "r=asyncio.run(s.execute(json.loads(sys.argv[2]),b));print(json.dumps({'receipt':r,'starts':b.starts}))")
        try:
            child = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(ROOT / "tests"), json.dumps(self.req)],
                                   capture_output=True, text=True, timeout=5, check=False)
            self.assertEqual(child.returncode, 0, child.stderr)
            data = json.loads(child.stdout)
            self.assertEqual(data["receipt"]["rc"], 5)
            self.assertEqual(data["starts"], 0)
        finally:
            backend.unblock.set()
            receipt = await owner
        self.assertEqual(receipt["rc"], 0)
        self.assertFalse(self.path.exists())

    async def test_crashed_owner_and_symlink_lease_are_not_stolen(self):
        self.path.write_bytes(b"expired-owner-no-release-proof")
        receipt = await s.execute(self.req, FakeBackend())
        self.assertEqual(receipt["rc"], 5)
        self.assertEqual(self.path.read_bytes(), b"expired-owner-no-release-proof")
        self.path.unlink()
        sentinel = self.path.with_name("sentinel")
        sentinel.write_bytes(b"untouched")
        self.path.symlink_to(sentinel)
        receipt = await s.execute(self.req, FakeBackend())
        self.assertEqual(receipt["rc"], 5)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    async def test_start_failure_retains_lease(self):
        backend = FakeBackend()
        async def broken_start(request, deadline_ns):
            raise RuntimeError("ownership missing")
        backend.start = broken_start
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["operation_rc"], 2)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertTrue(self.path.exists())
        self.assertNotIn(("stop", None), backend.events)

    async def test_observation_sample_must_be_fresh(self):
        backend = FakeBackend()
        backend.observation_changes = {"monotonic_ns": 1}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["rc"], 2)
        self.assertEqual(backend.starts, 0)
        self.assertFalse(self.path.exists())
        backend = FakeBackend()
        backend.running_changes = {"sample_id": "sample-1"}
        receipt = await s.execute(self.req, backend)
        self.assertEqual(receipt["rc"], 2)
        self.assertNotIn(("run", None), backend.events)

    async def test_cleanup_has_independent_bounded_deadline(self):
        backend = FakeBackend(stop_mode="wait")
        before = time.monotonic()
        receipt = await s.execute(self.req, backend)
        self.assertLess(time.monotonic() - before, 2.5)
        self.assertEqual(receipt["operation_rc"], 0)
        self.assertEqual(receipt["cleanup_rc"], 3)
        self.assertTrue(self.path.exists())

    async def test_replaced_lease_preserved(self):
        backend = FakeBackend(run_mode="wait")
        task = asyncio.create_task(s.execute(self.req, backend))
        await backend.running.wait()
        other = self.path.with_name("other")
        other.write_bytes(b"other-owner")
        other.replace(self.path)
        backend.unblock.set()
        receipt = await task
        self.assertEqual(receipt["cleanup_rc"], 2)
        self.assertEqual(receipt["rc"], 2)
        self.assertIn("backend_contract", receipt["reason_codes"])
        self.assertEqual(self.path.read_bytes(), b"other-owner")


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.req = request(self.root / "residency.lease")
        self.source, self.output = self.root / "request.json", self.root / "receipt.json"
        self.source.write_text(s.core.canonical_json(self.req), encoding="utf-8")
        self.output.write_bytes(b"caller-sentinel")

    def run_main(self, backend):
        stdout = io.StringIO()
        with patch.object(s, "_backend", return_value=backend), redirect_stdout(stdout):
            rc = s.main(["--request", str(self.source), "--receipt", str(self.output)])
        return rc, json.loads(stdout.getvalue()), stdout.getvalue()

    def test_advisory_decision_reaches_cli_and_binds_receipt(self):
        req = request(self.output.parent / "residency.lease", allowed=False)
        self.source.write_text(s.core.canonical_json(req), encoding="utf-8")
        backend = FakeBackend()
        rc, data, _ = self.run_main(backend)
        self.assertEqual(rc, 0, data)
        self.assertEqual(data["rc"], 0)
        self.assertEqual(data["request_sha256"], s.core.sha256_json(req))
        self.assertEqual(backend.starts, 1)
        self.assertEqual(s.core.load_json(self.output, max_bytes=32768), data)

    def test_canonical_atomic_publication(self):
        rc, data, stdout = self.run_main(FakeBackend())
        self.assertEqual(rc, 0)
        self.assertEqual(self.output.read_text(), s.core.canonical_json(data))
        self.assertEqual(stdout, s.core.canonical_json(data))
        self.assertEqual(list(self.root.glob(".af-*")), [])

    def test_refusal_preserves_output_bytes(self):
        for backend, expected in ((None, 3), (FakeBackend(run_mode="error"), 2), (FakeBackend(stop_mode="error"), 3)):
            with self.subTest(expected=expected):
                rc, result, _ = self.run_main(backend)
                self.assertEqual(rc, expected)
                self.assertEqual(self.output.read_bytes(), b"caller-sentinel")
                if expected == 3 and backend is None:
                    self.assertEqual(result["reason_codes"], ["backend_unavailable"])
                lease = Path(self.req["lease_path"])
                if lease.exists():
                    lease.unlink()  # Test owns this synthetic retained lease.

    def test_cli_validates_authorization_before_backend(self):
        missing = deepcopy(self.req)
        del missing["evidence"]
        stale = deepcopy(self.req)
        stale["decision_sha256"] = "b" * 64
        for value, expected, reason in ((missing, 3, "authorization_unproven"), (stale, 3, "decision_stale")):
            self.source.write_text(s.core.canonical_json(value), encoding="utf-8")
            rc, result, _ = self.run_main(None)
            self.assertEqual(rc, expected)
            self.assertEqual(result["reason_codes"], [reason])
            self.assertEqual(self.output.read_bytes(), b"caller-sentinel")
            self.assertFalse(Path(self.req["lease_path"]).exists())

    def test_injected_publication_failure_preserves_bytes(self):
        with patch.object(os, "replace", side_effect=OSError("synthetic publication failure")):
            rc, _, _ = self.run_main(FakeBackend())
        self.assertEqual(rc, 2)
        self.assertEqual(self.output.read_bytes(), b"caller-sentinel")
        self.assertEqual(list(self.root.glob(".af-*")), [])

    def test_parser_rejects_duplicate_float_nan_and_oversize(self):
        canonical = s.core.canonical_json(self.req).rstrip()
        duplicate = canonical[:-1] + ',"request_id":"fixture-request"}'
        fractional = canonical.replace('"seed":42', '"seed":1.5')
        not_finite = canonical.replace('"seed":42', '"seed":NaN')
        for text in (duplicate, fractional, not_finite, '{"broken":'):
            self.source.write_text(text, encoding="utf-8")
            rc, _, _ = self.run_main(None)
            self.assertEqual(rc, 2)
            self.assertEqual(self.output.read_bytes(), b"caller-sentinel")
        self.source.write_bytes(b" " * (s.MAX_JSON_BYTES + 1))
        self.assertEqual(self.run_main(None)[0], 2)

    def test_isolated_cli_has_no_production_backend(self):
        program = ("import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('tools.scheduler.scheduler',run_name='__main__')")
        child = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(ROOT), "--request", str(self.source), "--receipt", str(self.output)],
                               capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(child.returncode, 3, child.stderr)
        self.assertEqual(json.loads(child.stdout)["reason_codes"], ["backend_unavailable"])
        self.assertEqual(self.output.read_bytes(), b"caller-sentinel")


if __name__ == "__main__":
    unittest.main()
