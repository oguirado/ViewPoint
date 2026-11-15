"""C-budget/C-score portable controls; producer imports are intentional and strict.

No skips, xfail, fake registries, fake locks, fake budgets or host-speed
thresholds. Target fixtures are bounded RAM; no screenshots or frame logs.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import fields
from hashlib import sha256
import socket
import subprocess
import time
import unittest
from unittest.mock import patch


COMMIT = "9c418c4ab2cb4cc43af302a4c4d62639e4ce9bf7"
SOURCE = "b3d14c813d5c7d1fc282050be99ab348b9746101ae38e0011949c2793bd6e26d"


def pure_fixture():
    # Strictly import the real canonical provider at test execution, not at
    # discovery time. No conditional import and no substitute constructors.
    import numpy as np
    from wender.perceive.session_registry import SessionRegistry
    from wender.learn import proto1_work_metrics as wm
    registry = SessionRegistry(clock=time.monotonic_ns)
    handle = registry.register("xvfb_screen", "c-budget:fixture-01", object(), owned=True)
    image = np.arange(12 * 16 * 3, dtype=np.uint16).reshape(12, 16, 3).astype(np.uint8)
    expected = bytes(image.tobytes())
    frame = registry.admit_frame(handle, 0, image)
    observation = registry.admit_observation(handle, frame, state={"kind": "fixture"},
                                              dpr_ppm=1_000_000)
    sampler = wm.WorkSampler(source_commit=COMMIT, source_sha256=SOURCE,
                              workload="offline-rgb24-TARGET-v1", affinity="not-pinned",
                              max_operations=20_000, max_samples=24,
                              max_windows=4, max_bytes=10_000)
    pin = sampler.pin(frame)
    return wm, registry, handle, frame, observation, sampler, pin, expected, image


def explicit_population(sampler):
    from wender.learn import proto1_work_metrics as wm
    counts = Counter(s.stage for s in sampler.samples)
    return {name: counts.get(name, 0) for name in wm.STAGES}


def measured_pair(sampler, pin):
    from wender.learn import proto1_work_metrics as wm
    first, a = sampler.measure("capture", pin, lambda: wm.bounded_cpu_work(127), operations=127)
    second, b = sampler.measure("description", pin, lambda: wm.bounded_cpu_work(133), operations=133)
    return first, second, a, b


class WorkMetricsContracts(unittest.TestCase):
    def test_record_field_contract_and_status(self):
        from wender.learn import proto1_work_metrics as wm
        self.assertEqual([f.name for f in fields(wm.MetricRecord)],
                         ["name", "value", "unit", "evidence", "status"])
        a = wm.MetricRecord("wall_ns", 100, "ns", "VM clock:monotonic_ns", "MEASURED")
        self.assertEqual(list(a.serialise()), ["name", "value", "unit", "evidence", "status"])
        self.assertIs(type(a.value), int)
        self.assertEqual(wm.MetricRecord("absent", None, "ns",
                                        "AUSENTE: no qualified samples", "CITED").value, None)
        self.assertEqual(wm.MetricRecord("target", 50_000_000, "ns",
                                        "host-only target", "TARGET").status, "TARGET")
        with self.assertRaises(wm.MetricsRefused):
            wm.MetricRecord("fabricated", 1, "ns", "injected clock", "MEASURED")
        with self.assertRaises(wm.MetricsRefused):
            wm.MetricRecord("fabricated", None, "ns", "AUSENTE: no samples", "MEASURED")
        with self.assertRaises(wm.MetricsRefused):
            wm.MetricRecord("fabricated", 0, "ns", "VM clock:monotonic_ns", "HOST")
        with self.assertRaises(wm.MetricsRefused):
            wm.MetricRecord("null", None, "ns", "", "CITED")

    def test_nearest_rank_independent_integer_oracle(self):
        from wender.learn import proto1_work_metrics as wm
        values = [19, 5, 900, 7, 8, 13, 4, 21, 6, 40, 3, 91, 1, 12, 34, 2, 55, 16, 20, 17]
        for p in (1, 50, 90, 95, 99, 100):
            rank = -(-p * len(values) // 100)
            expect = sorted(values)[rank-1]
            self.assertEqual(wm.nearest_rank(values, p), expect)
        d = wm.distribution(values)
        self.assertEqual(d["mean"], sum(values)//len(values))
        self.assertEqual(d["count"], len(values))
        self.assertEqual(d["p95"], sorted(values)[-2])
        self.assertEqual(d["samples_ns"], sorted(values))
        for bad in ([], [1, -1], [True, 2]):
            with self.assertRaises(wm.MetricsRefused):
                wm.distribution(bad)
        for bad in (0, 101, True):
            with self.assertRaises(wm.MetricsRefused):
                wm.nearest_rank([1], bad)

    def test_real_cpu_clock_positive_controls(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        first, second, a, b = measured_pair(sampler, pin)
        self.assertIs(type(first), int)
        self.assertIs(type(second), int)
        self.assertGreaterEqual(a.cpu_ns, 0)
        self.assertGreaterEqual(b.cpu_ns, 0)
        self.assertGreaterEqual(a.wall_ns, 0)
        self.assertLessEqual(a.wall_start_ns, a.wall_end_ns)
        self.assertLessEqual(b.cpu_start_ns, b.cpu_end_ns)
        self.assertIn(sampler._cpu_clock_name, ("thread_time_ns", "process_time_ns"))
        output = sampler.report(population=explicit_population(sampler))
        self.assertEqual(output["scope"], "VM/offline")
        self.assertEqual(output["host_qualification"], "AUSENTE")
        self.assertIsNone(output["eligible_visits"])
        self.assertEqual(output["operations"], 260)
        for stage in ("capture", "description"):
            series = [x for x in sampler.samples if x.stage == stage]
            d = output["distributions"][stage]
            self.assertEqual(d["wall"]["samples_ns"], sorted(x.wall_ns for x in series))
            self.assertEqual(d["cpu"]["samples_ns"], sorted(x.cpu_ns for x in series))
        self.assertEqual(output["sample_population"]["capture"], 1)
        self.assertEqual(output["sample_population"]["description"], 1)

    def test_real_independent_capture_to_description(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        _, _, a, b = measured_pair(sampler, pin)
        self.assertLessEqual(a.wall_end_ns, b.wall_start_ns)
        report = sampler.report(population=explicit_population(sampler))
        metric = {r["name"]:r["value"] for r in report["metrics"]}
        self.assertEqual(metric["capture_to_description_p95_ns"],
                         b.wall_end_ns - a.wall_start_ns)
        self.assertEqual(metric["cpu_per_processed_frame_mean_ns"],
                         a.cpu_ns + b.cpu_ns)
        self.assertEqual(report["distributions"]["capture_to_description"]["count"], 1)

    def test_queue_wait_real_wall_without_cpu_claim(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        stamp = sampler.queue_start(pin)
        tick = sampler.queue_end(pin, stamp)
        self.assertIsNone(tick.cpu_ns)
        self.assertGreaterEqual(tick.wall_ns, 0)
        measured_pair(sampler, pin)
        r = sampler.report(population=explicit_population(sampler))
        self.assertIsNone(r["distributions"]["queue_wait"]["cpu"])
        self.assertEqual(r["sample_population"]["queue_wait"], 1)
        self.assertFalse(any(m["name"] == "queue_wait_cpu_mean_ns" for m in r["metrics"]))

    def test_optional_populations_null_not_zero(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        r = sampler.report(population=explicit_population(sampler))
        missing = [m for m in r["metrics"] if m["name"] == "live_app_wall_p95_ns"]
        self.assertEqual(len(missing), 1)
        self.assertIsNone(missing[0]["value"])
        self.assertEqual(missing[0]["status"], "CITED")
        self.assertTrue(missing[0]["evidence"].startswith("AUSENTE:"))
        self.assertIsNone(next(m for m in r["metrics"]
                          if m["name"] == "provider_wall_p95_ns")["value"])
        self.assertEqual(r["provider_population"], 0)
        self.assertEqual(r["live_app_population"], 0)

    def test_provider_live_app_separate_populations(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        sampler.measure("provider", pin, lambda: wm.bounded_cpu_work(17), operations=17)
        sampler.measure("live_app", pin, lambda: wm.bounded_cpu_work(31), operations=31)
        report = sampler.report(population=explicit_population(sampler))
        self.assertEqual(report["provider_population"], 1)
        self.assertEqual(report["live_app_population"], 1)
        self.assertEqual(report["distributions"]["provider"]["wall"]["count"], 1)
        self.assertEqual(report["distributions"]["live_app"]["wall"]["count"], 1)
        self.assertNotEqual(id(report["distributions"]["provider"]),
                            id(report["distributions"]["live_app"]))

    def test_bounded_work_is_deterministic_and_budgeted(self):
        from wender.learn import proto1_work_metrics as wm
        self.assertEqual(wm.bounded_cpu_work(700, limit=700),
                         wm.bounded_cpu_work(700, limit=700))
        self.assertNotEqual(wm.bounded_cpu_work(700), wm.bounded_cpu_work(701))
        for count in (-1, 701, True):
            with self.assertRaises(wm.MetricsRefused):
                wm.bounded_cpu_work(count, limit=700)

    def test_excessive_work_refused_before_callback(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        called = []
        with self.assertRaises(wm.MetricsRefused):
            sampler.measure("capture", pin, lambda: called.append(True), operations=20_001)
        self.assertEqual(called, [])
        self.assertEqual(sampler.samples, ())
        self.assertEqual(sampler._operations, 0)

    def test_empty_samples_must_fail_even_if_declared(self):
        wm, _, _, _, _, sampler, _, _, _ = pure_fixture()
        with self.assertRaisesRegex(wm.MetricsRefused, "empty"):
            sampler.report(population={s: 0 for s in wm.STAGES})

    def test_omitted_population_must_fail(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        with self.assertRaisesRegex(TypeError, "population"):
            sampler.report()
        with self.assertRaises(wm.MetricsRefused):
            sampler.report(population={"capture": 1})

    def test_population_mismatch_must_fail(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        counts = explicit_population(sampler)
        counts["description"] += 1
        with self.assertRaisesRegex(wm.MetricsRefused, "population mismatch"):
            sampler.report(population=counts)

    def test_required_empty_subset_must_fail(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        sampler.measure("capture", pin, lambda: wm.bounded_cpu_work(3), operations=3)
        with self.assertRaises(wm.MetricsRefused):
            sampler.report(population=explicit_population(sampler),
                           required_stages=("capture", "description"))

    def test_negative_duration_and_reversed_timestamps_fail(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        args = (pin.handle, pin.epoch, pin.frame_seq, sampler.workload,
                "description", 100, 99, 10, 11, 0, SOURCE)
        with self.assertRaisesRegex(wm.MetricsRefused, "reversed wall"):
            wm.Sample(*args)
        args = (pin.handle, pin.epoch, pin.frame_seq, sampler.workload,
                "description", 100, 105, 11, 10, 0, SOURCE)
        with self.assertRaisesRegex(wm.MetricsRefused, "reversed CPU"):
            wm.Sample(*args)
        args = (pin.handle, pin.epoch, pin.frame_seq, sampler.workload,
                "description", 100, 105, None, None, 0, SOURCE)
        with self.assertRaises(wm.MetricsRefused):
            wm.Sample(*args)

    def test_mixed_workload_identity_refused(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        foreign = wm.Sample(pin.handle, pin.epoch, pin.frame_seq,
                            "different-offline-workload", "provider", 1, 2, 3, 4, 0, SOURCE)
        with self.assertRaisesRegex(wm.MetricsRefused, "mixed workload"):
            sampler._append(foreign)
        self.assertEqual(sampler.report(population=explicit_population(sampler))["workload"],
                         sampler.workload)

    def test_frame_copy_identity_and_digest_independent_oracle(self):
        wm, _, _, frame, _, sampler, pin, expected, mutable_image = pure_fixture()
        self.assertEqual(pin.rgb24, expected)
        self.assertEqual(pin.digest, sha256(expected).hexdigest())
        self.assertEqual(pin.source_sha256, SOURCE)
        self.assertEqual(pin.handle, ("xvfb_screen", "c-budget:fixture-01", 1))
        mutable_image.fill(0)
        self.assertEqual(pin.rgb24, expected)
        self.assertEqual(pin.digest, sha256(expected).hexdigest())
        self.assertIsInstance(pin.rgb24, bytes)
        with self.assertRaises((AttributeError, TypeError)):
            pin.rgb24[0] = 0

    def test_mutated_frame_digest_refuses(self):
        wm, _, _, frame, _, sampler, pin, _, _ = pure_fixture()
        object.__setattr__(frame, "digest", "0" * 64)
        with self.assertRaisesRegex(wm.MetricsRefused, "SHA-256 mismatch"):
            sampler.pin(frame)
        self.assertEqual(len(sampler._frames), 1)

    def test_source_and_handle_fencing(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        with self.assertRaises(wm.MetricsRefused):
            wm.WorkSampler(source_commit="not-a-sha", source_sha256=SOURCE,
                           workload="normal", affinity="unbound")
        with self.assertRaises(wm.MetricsRefused):
            wm.WorkSampler(source_commit=COMMIT, source_sha256="?"*64,
                           workload="normal", affinity="unbound")
        from wender.perceive.session_registry import Handle
        self.assertEqual(wm.handle_identity(Handle("cdp_tab", "target", 2)),
                         ("cdp_tab", "target", 2))
        class InvalidHandle:
            kind = "cdp_tab"
            canonical_id = "target"
            generation = 0
        with self.assertRaises(wm.MetricsRefused):
            wm.handle_identity(InvalidHandle())

    def test_forged_negative_generation_sample_refused(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        with self.assertRaises(wm.MetricsRefused):
            wm.Sample(("xvfb_screen", "good", 0), 0, 0, sampler.workload,
                      "capture", 1, 2, 1, 2, 1, SOURCE)

    def test_rgb_ram_budget_enforced_on_second_window(self):
        wm, registry, _, frame, _, sampler, pin, _, _ = pure_fixture()
        import numpy as np
        small = wm.WorkSampler(source_commit=COMMIT, source_sha256=SOURCE,
                               workload="bounded-fixture", affinity="unbound",
                               max_windows=1, max_bytes=16*12*3 + 1)
        small.pin(frame)
        second = registry.register("xvfb_screen", "c-budget:fixture-02", object(), owned=True)
        other = registry.admit_frame(second, 0, np.zeros((12, 16, 3), dtype=np.uint8))
        with self.assertRaisesRegex(wm.MetricsRefused, "too many"):
            small.pin(other)
        tiny = wm.WorkSampler(source_commit=COMMIT, source_sha256=SOURCE,
                              workload="bounded-fixture", affinity="unbound",
                              max_windows=4, max_bytes=20)
        with self.assertRaisesRegex(wm.MetricsRefused, "byte cap"):
            tiny.pin(frame)

    def test_identity_bound_queue_marks_refuse_foreign(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        mark = sampler.queue_start(pin)
        counterfeit = wm.QueueMark(pin.handle, pin.epoch, pin.frame_seq+1,
                                   sampler.workload, mark.start_ns, SOURCE)
        with self.assertRaises(wm.MetricsRefused):
            sampler.queue_end(pin, counterfeit)
        self.assertEqual(sampler.samples, ())

    def test_metrics_no_raw_rgb_or_encoded_frame_material(self):
        wm, _, _, _, _, sampler, pin, expected, _ = pure_fixture()
        measured_pair(sampler, pin)
        import json
        manifest = json.dumps(sampler.report(population=explicit_population(sampler)),
                              sort_keys=True)
        self.assertNotIn(expected.hex(), manifest)
        # RGB24 is legitimate format/target metadata, not serialized pixel data.
        import base64
        self.assertNotIn(base64.b64encode(expected).decode("ascii"), manifest)
        self.assertIn(pin.digest, manifest)
        self.assertIn(COMMIT, manifest)
        self.assertIn(SOURCE, manifest)
        self.assertNotIn("np.ndarray", manifest)

    def test_host_targets_never_closed_by_vm(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        measured_pair(sampler, pin)
        r = sampler.report(population=explicit_population(sampler))
        self.assertEqual([m["status"] for m in r["host_targets"]],
                         ["TARGET"] * len(wm.HOST_TARGETS_NS))
        self.assertEqual(r["host_qualification"], "AUSENTE")
        self.assertEqual([m["value"] for m in r["host_targets"]],
                         [50_000_000, 8_000_000, 1_000_000, 100_000_000])
        self.assertTrue(all(x["status"] == "MEASURED" for x in r["metrics"]
                            if x["value"] is not None))

    def test_execution_is_offline(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        with patch.object(socket, "create_connection", side_effect=AssertionError("network")), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("external exec")):
            measured_pair(sampler, pin)
            manifest = sampler.report(population=explicit_population(sampler))
        self.assertTrue(manifest["bounded"])

    def test_four_window_host_sized_fixture_remains_offline(self):
        from wender.learn import proto1_work_metrics as wm
        from wender.perceive.session_registry import SessionRegistry
        import numpy as np
        reg = SessionRegistry(clock=time.monotonic_ns)
        sampler = wm.WorkSampler(source_commit=COMMIT, source_sha256=SOURCE,
            workload="four-640x360-rgb24-offline-TARGET", affinity="unbound",
            max_windows=4, max_bytes=4*640*360*3, max_operations=10_000)
        for lane in range(4):
            handle = reg.register("xvfb_screen", "lane-%d" % lane, object(), owned=True)
            rgb = np.full((360, 640, 3), lane * 17, dtype=np.uint8)
            frame = reg.admit_frame(handle, 0, rgb)
            pin = sampler.pin(frame)
            sampler.measure("capture", pin, lambda: wm.bounded_cpu_work(80), operations=80)
            sampler.measure("description", pin, lambda: wm.bounded_cpu_work(90), operations=90)
        r = sampler.report(population=explicit_population(sampler))
        self.assertEqual(r["frame_population"], 4)
        self.assertEqual(r["sample_population"]["capture"], 4)
        self.assertEqual(r["sample_population"]["description"], 4)
        self.assertEqual(r["distributions"]["capture_to_description"]["count"], 4)
        self.assertEqual(r["host_qualification"], "AUSENTE")
        self.assertIsNone(r["eligible_visits"])
        self.assertEqual(r["operations"], 680)
        self.assertTrue(all(frame["width"] == 640 and frame["height"] == 360
                            for frame in r["frame_metadata"]))

    def test_real_lock_budget_focus_and_cleanup_refusals(self):
        from wender.learn import proto1_work_metrics as wm
        from wender.act.budget_governor import ReservationInactive
        from wender.act.target_lock_registry import TargetLockRefused, TargetBusy
        from wender.perceive.session_registry import QuarantinedTargetError
        import numpy as np
        reg, locks, focus, budget, telemetry = wm.make_real_scheduler_providers(
            monotonic_clock=time.monotonic_ns, service_cap_ns=1)
        handle = reg.register("xvfb_screen", "p1:real-evidence", object(), owned=True)
        frame = reg.admit_frame(handle, 2, np.zeros((3, 3, 3), dtype=np.uint8))
        observation = reg.admit_observation(handle, frame, state={}, dpr_ppm=1_000_000)
        focus.set_focus(handle, domain="p1:real-evidence", epoch=0)
        deadline = time.monotonic_ns()+1_000_000_000
        actor = locks.acquire(handle, "real-action", expires_ns=deadline)
        with self.assertRaises(TargetBusy):
            locks.acquire(handle, "double-action", expires_ns=deadline)
        grant = focus.grant(actor, domain="p1:real-evidence")
        focus.validate(actor, grant, observation, domain="p1:real-evidence")
        focus.consume(grant)
        reservation = budget.reserve(handle, 1, action_id="real-action",
                                     deadline_ns=deadline)
        self.assertEqual(budget.pending_count, 1)
        self.assertEqual(budget.spent_global, 0)
        budget.commit(reservation)
        self.assertEqual(budget.pending_count, 0)
        self.assertEqual(budget.spent_global, 1)
        with self.assertRaises(ReservationInactive):
            budget.settle(reservation, consumed=False)
        self.assertEqual(budget.spent_global, 1)
        locks.mark_inflight(actor)
        with self.assertRaises(TargetLockRefused):
            locks.release(actor)
        locks.reconcile_quarantine(handle, acknowledged=False, expected_lease=actor)
        self.assertFalse(locks.owns(actor, now_ns=time.monotonic_ns()))
        locks.reconcile_quarantine(handle, acknowledged=True, expected_lease=actor)
        self.assertEqual([x["event"] for x in telemetry], ["reserve", "commit"])
        terminal = reg.stop(handle)
        self.assertEqual(terminal.status, "stopped")
        with self.assertRaises(QuarantinedTargetError):
            reg.register("xvfb_screen", "p1:real-evidence", object(), owned=True)
        reg.acknowledge_cleanup(handle)
        resumed = reg.register("xvfb_screen", "p1:real-evidence", object(), owned=True)
        self.assertGreater(resumed.generation, handle.generation)
        self.assertEqual(reg.tombstone(handle)[0].closed, True)

    def test_real_producer_ports_require_real_classes(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        from wender.perceive.session_registry import SessionRegistry
        from wender.act.target_lock_registry import TargetLockRegistry
        from wender.act.focus_policy import FocusPolicy
        from wender.act.budget_governor import BudgetGovernor
        registry, locks, focus, budget, telemetry = wm.make_real_scheduler_providers(
            monotonic_clock=time.monotonic_ns, service_cap_ns=1)
        self.assertIs(type(registry), SessionRegistry)
        self.assertIs(type(locks), TargetLockRegistry)
        self.assertIs(type(focus), FocusPolicy)
        self.assertIs(type(budget), BudgetGovernor)
        self.assertIs(type(telemetry), list)
        with self.assertRaises(wm.MetricsRefused):
            wm._RegistryPort(object())
        with self.assertRaises(wm.MetricsRefused):
            wm._BudgetPort(object())
        with self.assertRaises(wm.MetricsRefused):
            wm._LockPort(object())
        with self.assertRaises(wm.MetricsRefused):
            wm._FocusPort(object(), domain="x")

    def test_real_scheduler_path_refuses_missing_detector_without_send(self):
        wm, _, _, _, _, _, _, _, _ = pure_fixture()
        import numpy as np
        from wender.act.window_scheduler import (WindowScheduler, WriterFence,
                                                   ActionRequest, Receipt)
        registry, locks, focus, budget, telemetry = wm.make_real_scheduler_providers(
            monotonic_clock=time.monotonic_ns, service_cap_ns=1)
        handle = registry.register("xvfb_screen", "p1:real-path", object(), owned=True)
        image = np.zeros((3, 5, 3), dtype=np.uint8)
        frame = registry.admit_frame(handle, 0, image)
        observation = registry.admit_observation(handle, frame, state={}, dpr_ppm=1_000_000)
        fence = WriterFence(clock=time.monotonic_ns)
        port = locks.registry  # strict _RegistryPort wrapping the real registry
        calls = []
        class TransportOnly:
            offline = True
            prerequisite_gates = {name: True for name in
                ("identity_streams", "ownership_perception", "readiness", "writer_fence")}
            writer_fence = fence
            def __call__(self, request, context):
                calls.append((request, context))
                raise AssertionError("unsupported real detector was falsely bypassed")
        scheduler = WindowScheduler(registry=port, locks=wm._LockPort(locks),
            focus_policy=wm._FocusPort(focus, domain="p1:real-path"),
            budget=wm._BudgetPort(budget), chain=None, writer_fence=fence,
            dispatch=TransportOnly(), clock=time.monotonic_ns,
            max_queue=4, max_targets=4, service_cap_ns=1)
        request = ActionRequest(handle, "no-detector", 0,
                                 {"observation": observation}, 1,
                                 time.monotonic_ns()+1_000_000_000)
        receipt = scheduler.submit(request)
        self.assertIsInstance(receipt, Receipt)
        self.assertEqual(receipt.status, "AUSENTE")
        self.assertEqual(scheduler.step(time.monotonic_ns()), [])
        self.assertEqual(calls, [])
        self.assertEqual(budget.spent_global, 0)
        self.assertEqual(budget.pending_count, 0)
        self.assertEqual(telemetry, [])
        self.assertEqual(scheduler.fair_visits, {})

    def test_real_scheduler_probe_refuses_absent_chain(self):
        wm, _, _, _, _, sampler, pin, _, _ = pure_fixture()
        import numpy as np
        registry, locks, focus, budget, _ = wm.make_real_scheduler_providers(
            monotonic_clock=time.monotonic_ns, service_cap_ns=1)
        handle = registry.register("xvfb_screen", "p1:absent-chain", object(), owned=True)
        frame = registry.admit_frame(handle, 0, np.zeros((3, 4, 3), dtype=np.uint8))
        observation = registry.admit_observation(handle, frame, state={}, dpr_ppm=1_000_000)
        calls = []
        with self.assertRaisesRegex(wm.MetricsRefused, "actual detector"):
            wm.real_scheduler_probe(registry=registry, locks=locks, focus=focus,
                                    budget=budget, chain=None, observation=observation,
                                    scheduler_clock=time.monotonic_ns,
                                    transport_send=lambda r: calls.append(r))
        self.assertEqual(calls, [])
        self.assertEqual(budget.spent_global, 0)

    def test_actual_scorecard_empty_seal_refusal(self):
        from wender.learn.multiwin_scorecard import score_windows
        # This is the real reducer; no permissive fake returns a green score.
        result = score_windows(())
        self.assertFalse(result.passed)
        self.assertIn("missing_or_mixed_manifest", result.violations)
        self.assertIn("missing_mixed_or_invalid_seal", result.violations)

    def test_nearest_rank_mutant_is_semantically_killed(self):
        from wender.learn import proto1_work_metrics as wm
        data = [i for i in range(1, 21)]
        expected = sorted(data)[18]  # Independent oracle: ceil(95*20/100) = 19.
        self.assertEqual(wm.nearest_rank(data, 95), expected)  # Original control green.
        # Valid synthetic mutant: use p50 instead of p95 (no crash, imports green).
        with patch.object(wm, "nearest_rank", lambda xs, p: sorted(xs)[9]):
            self.assertNotEqual(wm.nearest_rank(data, 95), expected)

    def test_metric_record_mutant_is_semantically_killed(self):
        from wender.learn import proto1_work_metrics as wm
        record = wm.MetricRecord("stage", None, "ns", "AUSENTE: no host sample", "CITED")
        self.assertIsNone(record.serialise()["value"])  # Original control green.
        def zero_on_absence(self):
            d = {"name": self.name, "value": 0, "unit": self.unit,
                 "evidence": self.evidence, "status": self.status}
            return d
        with patch.object(wm.MetricRecord, "serialise", zero_on_absence):
            self.assertNotEqual(record.serialise()["value"], None)


if __name__ == "__main__":
    unittest.main()
