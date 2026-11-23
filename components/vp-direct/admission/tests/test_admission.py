"""Offline admission oracles and discriminating mutations."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock
import unittest
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
DIRECT = TESTS.parents[1]
sys.path.insert(0, str(DIRECT))

from admission import AdmissionError, AdmissionGate, AdmissionPlan, ResourceUsage, UsageSnapshot
from admission import gate as policy
from data import frames
from perception.baseline.inventory import collect
from perception.stream import PerceptionStream
from schemas.validation import ValidationError, validate_contract
from vadapt.x11_ramcap import SourceConfig


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.surfaces = {}
        self.other_resources = ()
        self.streams = ()
        self.memory = (1000, 250)
        self.scopes = [("owner", f"/user.slice/user-{os.getuid()}.slice/a.scope")]
        self.inventory = {"complete": True, "charged_bytes": 620,
                          "scopes": [{"current_bytes": 620, "processes": [{"pss_bytes": 270}]}],
                          "gpu": {"status": "unsupported"}}
        self.gate = AdmissionGate(lambda: UsageSnapshot(self.surfaces, self.other_resources, self.streams),
                                  scopes=self.scopes, read_memory=lambda: self.memory,
                                  inventory=lambda _: self.inventory)
        self.plan = AdmissionPlan("surface", "ACTIVE", "cdp", False)

    def decide(self, plan=None):
        with self.gate.reserve(plan or self.plan) as decision:
            validate_contract("vp.admission/v1", decision)
            return decision

    def assert_surface_ceiling(self):
        self.surfaces.update((f"incumbent-{index}", None) for index in range(15))
        sixteenth = self.decide(replace(self.plan, state="OFF"))
        self.assertTrue(sixteenth["admitted"])
        self.assertEqual(sixteenth["surface_count"], 16)
        self.surfaces[self.plan.surface_id] = None
        self.assertEqual(self.decide()["surface_count"], 16)
        refused = self.decide(replace(self.plan, surface_id="overflow"))
        self.assertEqual(refused["surface_count"], 17)
        self.assertFalse(refused["admitted"])

    def test_sixteenth_admits_seventeenth_refuses_and_off_still_counts(self):
        self.assert_surface_ceiling()

    def test_exact_margin_subtracts_only_incremental_commitments(self):
        admitted = self.decide(replace(self.plan, new_ring_bytes=60, other_new_bytes=40))
        self.assertTrue(admitted["admitted"])
        self.assertEqual(admitted["margin_percent"], 15)
        self.assertEqual(admitted["available_bytes"], 250)
        self.assertFalse(self.decide(replace(self.plan, new_ring_bytes=101))["admitted"])

    def test_gate_requires_at_least_one_owner_scope(self):
        with self.assertRaisesRegex(AdmissionError, "owner's cgroup scopes are required"):
            AdmissionGate(lambda: UsageSnapshot({}), scopes=[])

    def test_retained_charge_requires_admission_and_a_resting_state(self):
        self.surfaces[self.plan.surface_id] = ResourceUsage("ring", 80, 0, 20)
        for state, new_bytes, admitted, retained in (
            ("ACTIVE", 0, True, 20),
            ("ASLEEP", 101, False, 20),
            ("ASLEEP", 0, True, 80),
        ):
            with self.subTest(state=state, admitted=admitted):
                decision = self.decide(replace(self.plan, state=state, other_new_bytes=new_bytes))
                self.assertIs(decision["admitted"], admitted)
                self.assertEqual(decision["retained_bytes"], retained)

    def assert_resize_and_resident(self):
        self.surfaces[self.plan.surface_id] = ResourceUsage("old-ring", 80, 20, 80)
        self.other_resources = (ResourceUsage("pool", 40, 10, 0),)
        decision = self.decide(replace(self.plan, state="WAKING", backend="cdp", new_ring_bytes=70))
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["reserved_peak_bytes"], 150)
        self.assertEqual(decision["old_ring_bytes"], 80)
        self.assertEqual(decision["retained_bytes"], 80)
        self.assertEqual(decision["resident_bytes"], 620)
        self.assertEqual(decision["margin_percent"], 15)

    def test_resize_reserves_old_plus_new_and_reports_only_cgroup_resident(self):
        self.assert_resize_and_resident()

    def assert_hide_keeps_allocations(self):
        name = self.plan.surface_id
        ring = ResourceUsage("ring", 120, 80, 0)
        self.surfaces[name] = ring
        self.other_resources = (ResourceUsage("unsettled-file", 20, 0, 20),)
        for state in ("ASLEEP", "OFF", "QUARANTINED"):
            decision = self.decide(replace(self.plan, state=state))
            self.assertTrue(decision["admitted"])
            self.assertEqual(decision["retained_bytes"], 140)
        # The owner now reports the P3 ring as retained, with commitments intact.
        self.surfaces[name] = replace(ring, retained_bytes=120)
        second = replace(self.plan, surface_id="second", new_ring_bytes=21)
        self.assertFalse(self.decide(second)["admitted"])
        # A verified settlement removes the allocation but leaves the app open.
        self.surfaces[name] = None
        decision = self.decide(second)
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["surface_count"], 2)
        self.assertEqual(decision["retained_bytes"], 20)

    def test_sleep_off_and_quarantine_keep_retained_charges(self):
        self.assert_hide_keeps_allocations()

    def make_file_stream(self):
        binding = json.loads((DIRECT / "schemas/tests/examples/vp.surface_binding_v1.valid.json").read_text())
        owner = binding["owner"]
        scratch = TemporaryDirectory(dir=TESTS)
        self.addCleanup(scratch.cleanup)
        # This fixture reserves D3 capacity while the display producer is OFF.
        source = SourceConfig(scratch.name, "unused.xwd", 1, 1, "offline fixture")
        stream = PerceptionStream(binding, source, ring_slots=2, interval_ns=1_000,
                                  ttl_ns=1_000_000, max_frame_bytes=64, max_ring_bytes=8192)
        self.addCleanup(stream.close)
        return stream, scratch.name, owner

    def file_stream(self, *, sampled=True):
        stream, directory, owner = self.make_file_stream()
        if sampled:
            self.streams = (stream,)
        with self.gate.reserve(replace(self.plan, state="OFF", other_new_bytes=80)) as decision:
            self.assertTrue(decision["admitted"])
            sequence = stream.open_file_sequence(directory=directory, slots=2, max_frame_bytes=40)
        self.addCleanup(sequence.close, owner)
        return stream, sequence, owner

    def assert_files_still_charged(self):
        second = replace(self.plan, surface_id="second", other_new_bytes=20)
        decision = self.decide(second)
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["surface_count"], 2)
        self.assertEqual(decision["resident_bytes"], 620)
        self.assertEqual(decision["retained_bytes"], 80)
        self.assertEqual(decision["margin_percent"], 15)
        self.assertFalse(self.decide(replace(second, other_new_bytes=21))["admitted"])

    def test_d3_refuses_release_with_live_borrow_and_p6_keeps_charge(self):
        stream, sequence, owner = self.file_stream()
        frame = sequence.append(owner, b"frame")
        with sequence.borrow(owner, frame.index):
            stream.close()
            self.assertFalse(sequence.close(owner))
            self.assert_files_still_charged()
        self.assert_files_still_charged()
        self.assertTrue(sequence.close(owner))
        decision = self.decide(replace(self.plan, surface_id="second", other_new_bytes=21))
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["retained_bytes"], 0)
        self.assertEqual(decision["surface_count"], 2)

    def test_p6_retains_d3_commitment_after_failed_scrub(self):
        _, sequence, owner = self.file_stream()
        self.streams = ()
        self.surfaces[self.plan.surface_id] = None
        sequence.append(owner, b"frame")
        with patch.object(frames, "_scrub", side_effect=frames.FrameSequenceError("scrub failed")):
            with self.assertRaisesRegex(frames.FrameSequenceError, "scrub failed"):
                sequence.close(owner)
            self.assert_files_still_charged()
        self.assertTrue(sequence.close(owner))
        self.assertEqual(self.decide()["retained_bytes"], 0)

    def test_p6_tracks_unsampled_off_stream_until_drain_and_verified_close(self):
        stream, sequence, owner = self.file_stream(sampled=False)
        frame = sequence.append(owner, b"frame")
        with sequence.borrow(owner, frame.index) as borrow:
            stream.close()
            self.assertFalse(sequence.close(owner))
            self.assertEqual(borrow.read(), b"frame")
            self.assert_files_still_charged()
        self.assert_files_still_charged()

        self.assertTrue(sequence.close(owner))
        decision = self.decide(replace(self.plan, surface_id="second", other_new_bytes=21))
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["retained_bytes"], 0)
        self.assertEqual(decision["surface_count"], 1)

    def test_file_sequence_refuses_wrong_surface_or_refused_reservation(self):
        stream, directory, _ = self.make_file_stream()
        for plan in (replace(self.plan, surface_id="other", other_new_bytes=80),
                     replace(self.plan, other_new_bytes=101)):
            with (self.subTest(plan=plan), self.gate.reserve(plan),
                  patch.object(frames, "TemporaryFile") as storage,
                  self.assertRaisesRegex(AdmissionError, "admitted reservation for its surface")):
                stream.open_file_sequence(directory=directory, slots=2, max_frame_bytes=40)
            storage.assert_not_called()

    def test_mutant_skipping_enrollment_loses_omitted_off_stream_charge(self):
        with patch("perception.stream.stream._track_file_stream") as mutant:
            _, sequence, owner = self.file_stream(sampled=False)
        mutant.assert_called_once()
        frame = sequence.append(owner, b"frame")
        with sequence.borrow(owner, frame.index):
            self.assertFalse(sequence.close(owner))
            with self.assertRaises(AssertionError):
                self.assert_files_still_charged()
            # The mutant admits exactly the competing request the oracle forbids.
            decision = self.decide(replace(self.plan, surface_id="second", other_new_bytes=21))
            self.assertTrue(decision["admitted"])
            self.assertEqual(decision["retained_bytes"], 0)

    def test_p6_refuses_duplicate_accounting_for_linked_file_sequence(self):
        stream, _, _ = self.file_stream()
        self.other_resources = stream.accounting().file_sequences
        with self.assertRaisesRegex(AdmissionError, "overlapping resource reservations"):
            self.decide()
        self.other_resources = ()
        self.surfaces[self.plan.surface_id] = None
        with self.assertRaisesRegex(AdmissionError, "overlapping surface accounting"):
            self.decide()

    def test_mutant_drops_p3_file_charge_with_outstanding_borrow(self):
        stream, sequence, owner = self.file_stream()
        frame = sequence.append(owner, b"frame")
        account = stream.accounting

        def forget_borrows():
            snapshot = account()
            if snapshot.outstanding_file_borrows:
                return replace(snapshot, file_sequences=())
            return snapshot

        with sequence.borrow(owner, frame.index):
            self.assertFalse(sequence.close(owner))
            with patch.object(stream, "accounting", side_effect=forget_borrows) as mutant:
                with self.assertRaisesRegex(AssertionError, "0 != 80"):
                    self.assert_files_still_charged()
                mutant.assert_called_once_with()
            self.assert_files_still_charged()

    def test_unknown_gpu_refuses_gpu_expansion_and_preserves_cpu_path(self):
        for status in ("unsupported", "unaccounted", "AUSENTE", None):
            with self.subTest(status=status):
                self.inventory["gpu"] = {"status": status}
                gpu = self.decide(replace(self.plan, backend="gpu", gpu_dependent=True))
                self.assertFalse(gpu["admitted"])
                self.assertFalse(gpu["gpu_accounted"])
                self.assertTrue(self.decide()["admitted"])

    def test_missing_or_invalid_host_and_scope_measurements_fail_closed(self):
        for value in ((1000, None), (1000, 1001), (0, 0), (1000, True)):
            self.memory = value
            with self.subTest(memory=value), self.assertRaises(AdmissionError):
                self.decide()
        self.memory = (1000, 250)
        self.inventory["complete"] = False
        with self.assertRaisesRegex(AdmissionError, "complete cgroup"):
            self.decide()
        self.inventory["complete"] = True
        self.inventory["charged_bytes"] = None
        with self.assertRaisesRegex(AdmissionError, "complete cgroup"):
            self.decide()

    def test_missing_memavailable_refuses_instead_of_using_memfree(self):
        with patch.object(Path, "read_text", return_value="MemTotal: 42 kB\nMemFree: 31 kB\n"):
            with self.assertRaisesRegex(AdmissionError, "incomplete host memory"):
                policy._read_host_memory()

    def test_duplicate_resource_or_overlapping_p1_scope_is_refused(self):
        same = ResourceUsage("ring", 40, 10, 0)
        self.surfaces[self.plan.surface_id] = same
        self.other_resources = (same,)
        with self.assertRaisesRegex(AdmissionError, "overlapping resource"):
            self.decide()
        self.other_resources = ()
        path = self.scopes[0][1]
        for overlap in (path, path + "/child"):
            self.gate = AdmissionGate(lambda: UsageSnapshot(self.surfaces),
                                      scopes=[*self.scopes, ("other", overlap)],
                                      read_memory=lambda: self.memory)
            with self.assertRaisesRegex(AdmissionError, "host or cgroup memory evidence") as error:
                self.decide()
            self.assertIn("overlap", str(error.exception.__cause__))

    def test_p1_disjoint_scope_measurements_exclude_per_process_pss(self):
        baseline = DIRECT / "perception/baseline/tests/recorded_inventory.json"
        snapshot = json.loads(baseline.read_text())
        uid = str(os.getuid())
        slice_path = f"/user.slice/user-{uid}.slice"
        with TemporaryDirectory(dir=TESTS) as directory:
            roots = {name: Path(directory) / name for name in ("proc", "cgroup")}
            for name, files in snapshot.items():
                for path, text in files.items():
                    file = roots[name] / path.replace("@SLICE@", slice_path).lstrip("/")
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(text.replace("@SLICE@", slice_path).replace("@UID@", uid))
            self.gate = AdmissionGate(lambda: UsageSnapshot({}),
                                      scopes=[("alpha", f"{slice_path}/alpha.scope"),
                                              ("beta", f"{slice_path}/beta.scope")],
                                      read_memory=lambda: self.memory,
                                      inventory=partial(collect, proc_root=roots["proc"],
                                                        cgroup_root=roots["cgroup"]))
            decision = self.decide()
        self.assertTrue(decision["admitted"])
        self.assertEqual(decision["resident_bytes"], 8192 + 7168)
        self.assertEqual(decision["margin_percent"], 25)

    def test_competing_allocations_see_the_first_reservation_after_owner_update(self):
        self.surfaces.update((f"incumbent-{index}", None) for index in range(15))
        contender_waiting = Event()
        lock = Lock()

        class ObservedLock:
            def __enter__(self):
                if not lock.acquire(blocking=False):
                    contender_waiting.set()
                    lock.acquire()

            def __exit__(self, *_):
                lock.release()

        def competing_decision():
            return self.decide(replace(self.plan, surface_id="other", new_ring_bytes=50))

        with patch.object(self.gate, "_lock", ObservedLock()), ThreadPoolExecutor(max_workers=1) as worker:
            with self.gate.reserve(replace(self.plan, new_ring_bytes=60)) as first:
                pending = worker.submit(competing_decision)
                self.assertTrue(contender_waiting.wait(5))
                self.assertTrue(first["admitted"])
                self.surfaces[self.plan.surface_id] = ResourceUsage("pending-ring", 60, 60, 0)
            second = pending.result(timeout=5)
        self.assertEqual(second["surface_count"], 17)
        self.assertEqual(second["margin_percent"], 14)
        self.assertFalse(second["admitted"])

    def test_retry_after_allocation_error_uses_unsettled_owner_accounting(self):
        resize = replace(self.plan, new_ring_bytes=60)
        with self.assertRaisesRegex(RuntimeError, "partially allocated"):
            with self.gate.reserve(resize) as decision:
                self.assertTrue(decision["admitted"])
                self.other_resources = (ResourceUsage("unsettled", 60, 60, 60),)
                raise RuntimeError("partially allocated")
        self.assertFalse(self.decide(resize)["admitted"])

    def test_mutant_raised_ceiling_conflicts_with_frozen_refusal(self):
        with patch.object(policy, "MAX_SURFACES", 17), self.assertRaises(ValidationError):
            self.assert_surface_ceiling()

    def test_mutant_free_on_hide_fails_retained_commitment_oracle(self):
        def ignore_retained(resources):
            return sum(resource.pending_bytes for resource in resources if not resource.retained_bytes)

        with patch.object(policy, "_pending_bytes", ignore_retained), self.assertRaises(AssertionError):
            self.assert_hide_keeps_allocations()

    def test_mutants_double_counting_cgroup_or_pss_fail_resize_oracle(self):
        pending_bytes = policy._pending_bytes
        for duplicate in (620, 270):
            def double_count(resources):
                return pending_bytes(resources) + duplicate

            with (self.subTest(duplicate=duplicate), patch.object(policy, "_pending_bytes", double_count),
                  self.assertRaises(AssertionError)):
                self.assert_resize_and_resident()
