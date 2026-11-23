"""Exercise the owner through frozen call arguments, a real broker, P2 and P3."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from threading import Barrier, Event
import time
import unittest
from unittest.mock import Mock, patch

DIRECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DIRECT))
from bus.broker import Broker
from human import FreshObservation
from perception.demand import DemandCoordinator, DemandError
from perception.lifecycle import (
    LifecycleError, PerceptionLifecycle, RecoverableDemandCoordinator, RuntimeEvidence, TransitionEvidence,
)
from perception.lifecycle.machine import _ReapEvidence
from perception.stream import PerceptionStream
from perception.stream.tests.test_stream import put_screen
from privacy import PrivacyBinding, PrivacyMetadata, PrivacySentinel, PrivacyState
from rewind.machine import GoalMachine
from schemas.validation import canonical_bytes, validate_contract
from vadapt import x11_ramcap


def example(name):
    path = DIRECT / "schemas/tests/examples" / (name + "_v1.valid.json")
    return json.loads(path.read_bytes())


class ReapEvidenceTests(unittest.TestCase):
    def test_observation_requires_at_most_one_raw_reap_reply(self):
        reply = {"reaped": ["fixture-ring"], "retained": []}
        capture = Mock()
        capture.Reap.return_value = reply
        evidence = _ReapEvidence(capture)

        def reap_twice():
            evidence.Reap()
            evidence.Reap()

        self.assertEqual(evidence.observe(lambda: "idle"), ("idle", None))
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            evidence.observe(reap_twice)
        self.assertEqual(evidence.observe(evidence.Reap), (reply, reply))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ["P4_TEST_RAM"])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.b = Broker(self.root / "rt")
        self.addCleanup(self.b.close)
        self.binding = example("vp.surface_binding")
        self.owner = self.binding["owner"]
        self.b.bind("session", self.binding, lease_state="AGENT")
        self.demand = dict(example("vp.demand"), demand_id="agent-demand", consumer=self.owner,
                           purpose="agent-perception")
        self.recording = dict(self.demand, demand_id="recorder", consumer="recorder", purpose="recording")
        self.src = DemandCoordinator(self.binding["surface_id"], interests=["VERIFIED", "DISPLAY"],
                                     authorize=lambda row: row in (self.demand, self.recording), drain=lambda: True)
        put_screen(self.root / "screen", b"red" * 32)
        self.replace_stream()
        self.sample = RuntimeEvidence(self.binding["incarnation"], (), 40)
        self.qualified = True
        self.drain = lambda: True
        self.p = self.provision()

    def replace_stream(self):
        config = x11_ramcap.SourceConfig(str(self.root), "screen", 32, 1, "synthetic fixture")
        self.stream = PerceptionStream(self.binding, config, ring_slots=2, interval_ns=1000,
                                       ttl_ns=10**10, max_frame_bytes=4096, max_ring_bytes=8192,
                                       workspace_root=self.root / "rings")
        self.addCleanup(self.stream.close)

    def provision(self, **options):
        return PerceptionLifecycle(
            self.b, "session", self.binding, self.src, self.demand, self.stream,
            qualified=lambda binding: self.qualified, runtime=lambda: self.sample,
            drain_agent=lambda binding: self.drain(), **options)

    def call(self, op):
        result = self.p.vp_perception_call(dict(op=op, reason="fixture", surface_id=self.binding["surface_id"]))
        validate_contract("vp_perception_call." + op + ".result/v1", result)
        return result

    def state(self):
        return self.b.perception_state(self.binding["surface_id"])["state"]

    def goal(self, obs):
        ckpt = example("vp.rewind_receipt")
        ckpt.update(goal_id="g", op="checkpoint", confirmed_tick=0, authority_epoch=self.binding["authority_epoch"])
        ckpt["budget"].update(attempts_limit=10, rewinds_limit=3, attempts_used=0, rewinds_used=0)
        goal = GoalMachine.create(self.root / "goal", goal_id="g", session_id="session",
                                  binding=self.binding, checkpoint=ckpt, score=100, steps=2,
                                  duplicates=3, epsilon=1, action_quantum="0.01",
                                  observation_id=obs, generation=1)
        self.p.bind_goal(goal)
        return goal

    def test_full_cycle_preserves_app_session_and_retires_observation(self):
        initial = self.b.lease("session")
        active = self.call("wake")
        old = active["fresh_observation_id"]
        self.assertEqual(self.p.read_frame(old), b"red" * 32)
        asleep = self.call("sleep")
        self.assertEqual(self.state(), "ASLEEP")
        self.assertEqual(asleep["charges"]["reserved_bytes"], active["charges"]["reserved_bytes"])
        self.assertTrue(asleep["tokens_retired"])
        awake = self.call("wake")
        self.assertNotEqual(old, awake["fresh_observation_id"])
        with self.assertRaisesRegex(LifecycleError, "STALE_FRAME"):
            self.p.run_agent("act", old, lambda: self.fail("stale act reached executor"))
        self.assertEqual(self.p.run_agent("act", awake["fresh_observation_id"], lambda: "done"), "done")
        off = self.call("off")
        self.assertTrue(off["complete"])
        self.assertEqual(off["charges"]["reserved_bytes"], 0)
        self.assertEqual(self.state(), "OFF")
        self.assertEqual(self.b.lease("session"), initial)
        self.assertTrue(self.call("wake")["fresh_observation_id"])
        self.assertEqual(self.call("status")["to"], "ACTIVE")

    def test_60_seconds_after_ack_no_agent_work_or_frame_capture(self):
        obs = self.call("wake")["fresh_observation_id"]
        counts = Counter()
        kinds = ("acquisition", "analysis", "ocr", "inference", "decision")
        for kind in kinds:
            self.p.run_agent(kind, obs, lambda: counts.update([kind]))
        self.assertEqual(counts, dict.fromkeys(kinds, 1))
        counts.clear()
        with patch.object(self.stream._capture, "capture_once", wraps=self.stream._capture.capture_once) as capture:
            self.call("sleep")
            start = time.monotonic()
            while True:
                for kind in kinds:
                    with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                        self.p.run_agent(kind, obs, lambda: counts.update([kind]))
                remaining = 60 - (time.monotonic() - start)
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
            self.assertEqual(counts, {})
            capture.assert_not_called()
        self.assertEqual(self.state(), "ASLEEP")

    def test_no_sleep_ack_until_inflight_operation_finishes(self):
        obs = self.call("wake")["fresh_observation_id"]
        running, finish = Event(), Event()
        def analyze():
            running.set()
            self.assertTrue(finish.wait(4))
        with ThreadPoolExecutor(2) as pool:
            item = pool.submit(self.p.run_agent, "analysis", obs, analyze)
            self.assertTrue(running.wait(2))
            pending = pool.submit(self.call, "sleep")
            try:
                with self.p._admission:
                    self.assertTrue(self.p._admission.wait_for(lambda: not self.p._admitting, 2))
                self.assertFalse(pending.done())
                self.assertEqual(self.state(), "ACTIVE")
                with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                    self.p.run_agent("analysis", obs, self.fail)
            finally:
                finish.set()
            item.result()
            self.assertEqual(pending.result()["to"], "ASLEEP")

    def test_privacy_hold_rejects_late_ram_read_and_fences_wake_without_changing_lease(self):
        active = self.call("wake")
        obs = active["fresh_observation_id"]
        lease = self.b.lease("session")
        running, finish = Event(), Event()
        read = self.stream.read

        def read_with_barrier(*args):
            pixels = read(*args)
            running.set()
            self.assertTrue(finish.wait(4))
            return pixels

        with self.src.register(self.recording), patch.object(self.stream, "read", read_with_barrier):
            with ThreadPoolExecutor(1) as pool:
                late = pool.submit(self.p.read_frame, obs)
                try:
                    self.assertTrue(running.wait(2))
                    self.p.run_agent("analysis", obs, self.p.hold_for_privacy)
                    with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                        self.p.run_agent("act", obs, self.fail)
                    with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                        self.call("wake")
                    with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                        self.p.wake_for_verified()
                finally:
                    finish.set()
                with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                    late.result()

            self.assertEqual(self.b.lease("session"), lease)
            self.assertEqual(self.stream.status()["reserved_bytes"], active["charges"]["reserved_bytes"])
            self.assertEqual(self.call("sleep")["to"], "ASLEEP")
            self.assertIn(self.recording["demand_id"], [d["demand_id"] for d in self.src.snapshot()["demands"]])
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.call("wake")

    def test_transition_hook_exports_committed_demand_and_raw_reap(self):
        self.b = Broker(self.root / "rt-with-publisher")
        self.addCleanup(self.b.close)
        self.b.bind("session", self.binding, lease_state="AGENT")
        publications = []
        reap_replies = []
        capture = self.stream._capture
        reap = capture.Reap

        def capture_raw_reap(*args, **kwargs):
            reply = reap(*args, **kwargs)
            reap_replies.append(deepcopy(reply))
            return reply

        def publish(receipt, evidence):
            publications.append((receipt, evidence, self.src.snapshot(), self.b.perception_state(self.p.surface)))

        with patch.object(capture, "Reap", side_effect=capture_raw_reap):
            self.p = self.provision(on_transition=publish)
            self.call("wake")
            with self.src.register(self.recording):
                off = self.call("off")

        woke, stopped = publications
        self.assertIsNone(woke[1].reap)
        receipt, evidence, snapshot, state = stopped
        self.assertEqual(receipt, off)
        self.assertIsInstance(evidence, TransitionEvidence)
        self.assertEqual(evidence.broker_records[-1]["perception"], state)
        self.assertEqual(evidence.demand_snapshot, snapshot)
        self.assertEqual(off["retained"], [self.recording["demand_id"]])
        self.assertEqual(off["demand_snapshot_sha256"], sha256(canonical_bytes(snapshot)).hexdigest())
        raw_reap, = reap_replies
        self.assertEqual(raw_reap, {"reaped": off["scrub"]["buffers"], "retained": []})
        self.assertEqual(evidence.reap, raw_reap, "raw P3 Reap missing from P4 transition evidence")

    def test_mutant_drops_raw_reap_from_transition_evidence(self):
        with patch.object(PerceptionLifecycle, "_observe_reap", lambda self, operation: (operation(), None)):
            with self.assertRaisesRegex(AssertionError, "raw P3 Reap missing"):
                self.test_transition_hook_exports_committed_demand_and_raw_reap()

    def test_demand_arriving_at_final_ack_survives_and_wakes_once(self):
        obs = self.call("wake")["fresh_observation_id"]
        goal = self.goal(obs)
        waiting, ack = Event(), Event()
        def drain():
            waiting.set()
            return ack.wait(4)
        self.drain = drain
        with self.src.register(self.recording) as recorder, ThreadPoolExecutor(2) as pool:
            pending = pool.submit(self.call, "sleep")
            self.assertTrue(waiting.wait(2))
            try:
                self.src.publish(recorder, 1, "external-frame")
                external = pool.submit(self.p.wake_for_verified)
                self.assertEqual(self.state(), "ACTIVE")
                self.assertEqual(self.src.snapshot()["queued"]["VERIFIED"], 1)
            finally:
                ack.set()
            rest = pending.result()
            self.assertEqual(rest["retained"], ["recorder"])
            work, active = external.result()
            self.assertEqual(work.reference, "external-frame")
            self.assertNotEqual(active["fresh_observation_id"], obs)
            self.assertEqual(goal.replay()["q"], 1)
            with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                self.p.run_agent("act", active["fresh_observation_id"], self.fail)
            self.assertEqual(self.call("wake")["fresh_observation_id"], active["fresh_observation_id"])
            replayed = GoalMachine(self.root / "goal", "g").replay()
            self.assertEqual((replayed["q"], replayed["generation"], replayed["t"]), (0, 2, 1))
            self.assertEqual(list(replayed["work"].values()), ["consumed"])
            with patch.object(goal, "settle_external_work", side_effect=OSError("ledger unavailable")):
                with self.assertRaises(OSError):
                    self.p.complete_verified(work)
            self.assertEqual(self.src.snapshot()["claimed"], 0)
            self.p.complete_verified(work)
            self.assertEqual(list(goal.replay()["work"].values()), ["settled"])
            self.assertIsNone(self.p.wake_for_verified())

    def test_active_recording_keeps_its_credit_and_borrow_during_off(self):
        self.call("wake")
        with self.src.register(self.recording) as handle:
            self.src.publish(handle, 1, "frame")
            work, = self.src.claim("VERIFIED", budget=1, timeout=0)
            with self.src.borrow(work, 97):
                receipt = self.call("off")
                snapshot = self.src.snapshot()
                self.assertEqual(snapshot["borrowed_bytes"], 97)
                self.assertEqual(receipt["demand_snapshot_sha256"], sha256(canonical_bytes(snapshot)).hexdigest())
                self.assertEqual(receipt["retained"], ["recorder"])
            self.src.complete(work)
            self.src.publish(handle, 2, "another-frame")
            self.src.complete(self.src.claim("VERIFIED", budget=1, timeout=0)[0])

    def test_negative_timeout_stop_and_missing_ack_never_prove_sleep(self):
        self.call("wake")
        for ack in (None, False, "STOP", "wait", "timeout"):
            self.drain = lambda: ack
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("sleep")
            self.assertEqual(self.state(), "ACTIVE")
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("status")
        self.drain = lambda: True
        self.assertTrue(self.call("sleep")["complete"])

    def test_negative_off_needs_app_witness_and_empty_exclusive_scope(self):
        self.call("wake")
        for sample in (RuntimeEvidence("closed", (), 0), RuntimeEvidence("inc", (123,), 0), None):
            self.sample = sample
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("off")
            self.assertNotEqual(self.state(), "OFF")
        self.sample = RuntimeEvidence("inc", (), 0)
        self.assertTrue(self.call("off")["complete"])

    def test_negative_identity_and_capability_rechecked_before_active(self):
        original = self.stream.wake
        def replaced():
            self.qualified = False
            return original()
        with patch.object(self.stream, "wake", side_effect=replaced):
            with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                self.call("wake")
        self.assertEqual(self.state(), "OFF")
        self.assertIsNone(self.stream.status()["ring_id"])
        self.qualified = True
        real_binding = self.b.binding
        for key, changed in (("owner", "foreign"), ("incarnation", "old"), ("authority_epoch", 2)):
            with patch.object(self.b, "binding", return_value=dict(real_binding("surface"), **{key: changed})):
                with self.assertRaisesRegex(LifecycleError, "identity_mismatch"):
                    self.call("wake")
        self.b.human_lease("session", "HUMAN", 1, "operator")
        with self.assertRaises(LifecycleError):
            self.call("wake")

    def test_two_failed_wakes_quarantine_then_goal_charged_reopen(self):
        obs = self.call("wake")["fresh_observation_id"]
        goal = self.goal(obs)
        self.call("sleep")
        with patch.object(self.stream, "wake", return_value=None) as producer:
            for _ in range(2):
                with self.assertRaisesRegex(LifecycleError, "STALE_FRAME"):
                    self.call("wake")
            self.assertEqual(producer.call_count, 2)
        self.assertEqual(self.state(), "QUARANTINED")
        self.assertFalse(self.call("status")["complete"])
        with self.assertRaisesRegex(LifecycleError, "HOLD"):
            self.call("wake")
        self.assertEqual(goal.replay()["wake_failures"], 2)
        self.assertNotEqual(obs, self.p.reopen_after_quarantine()["fresh_observation_id"])
        self.assertEqual(goal.replay()["r"], 1)

    def test_late_producer_never_publishes_active_or_competes_with_retry(self):
        entered, release = Event(), Event()
        original = self.stream.wake
        def delayed():
            entered.set()
            if not release.wait(6):
                raise AssertionError("fixture timed out")
            return original()
        with patch.object(self.stream, "wake", side_effect=delayed):
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.call("wake")
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - started, 4.5)
                self.assertEqual(self.state(), "WAKING")
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.call("wake")
            finally:
                release.set()
            end = time.monotonic() + 2
            while self.state() == "WAKING" and time.monotonic() < end:
                time.sleep(.005)
        self.assertEqual(self.state(), "OFF")
        events = [json.loads(row) for row in (self.root / "rt/events.jsonl").read_bytes().splitlines()]
        self.assertNotIn("ACTIVE", [r["perception"]["state"] for r in events if r.get("op") == "perception"])
        self.assertTrue(self.call("wake")["complete"])


    def test_agent_first_after_broker_restart_claims_external_delivery_once(self):
        old = self.call("wake")["fresh_observation_id"]
        goal = self.goal(old)
        with self.src.register(self.recording) as handle:
            self.call("sleep")
            self.src.publish(handle, 1, "queued-before-restart")
            old_record = self.b.perception_state(self.binding["surface_id"])
            self.b.close()
            replacement = Broker(self.root / "rt")
            self.addCleanup(replacement.close)
            restored_goal = GoalMachine(self.root / "goal", goal.goal_id)
            self.p.reconnect_broker(replacement, restored_goal)
            self.assertEqual(replacement.perception_state(self.binding["surface_id"]), old_record)
            with patch.object(self.stream, "wake", wraps=self.stream.wake) as capture:
                acquired = self.call("wake")["fresh_observation_id"]
                work, receipt = self.p.wake_for_verified()
                self.assertEqual(receipt["fresh_observation_id"], acquired)
                self.assertEqual(work.reference, "queued-before-restart")
                self.p.complete_verified(work)
                self.assertIsNone(self.p.wake_for_verified())
                with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                    self.call("wake")
                capture.assert_called_once()
            ledger = restored_goal.replay()
            self.assertEqual((ledger["q"], ledger["d"], ledger["t"], ledger["generation"]), (0, 1, 1, 2))
            self.assertEqual(list(ledger["work"].values()), ["settled"])

    def test_failed_verified_wake_returns_claim_for_explicit_settlement(self):
        self.qualified = False
        with self.src.register(self.recording) as handle:
            self.src.publish(handle, 1, "unavailable-source")
            with self.assertRaisesRegex(LifecycleError, "NO_LEASE") as refusal:
                self.p.wake_for_verified()
            self.assertEqual(self.src.snapshot()["claimed"], 1)
            self.assertIsNone(self.p.wake_for_verified())
            self.p.complete_verified(refusal.exception.work)
            self.assertEqual(self.src.snapshot()["claimed"], 0)

    def test_control_handler_checks_context_and_dry_run_never_wakes(self):
        request = example("vp.control.req")
        request.update(op="vp_perception_call", args=dict(op="wake", reason="fixture", session_id="session"))
        request["context"].update(session_id="session", incarnation="inc", agent_instance="owner")
        handler = self.p.control_handler()
        preview = handler.preview(request)
        self.assertEqual((preview.ok, preview.code), (True, "OK"))
        self.assertFalse(preview.result["complete"])
        self.assertEqual(self.state(), "OFF")
        request["dry_run"] = False
        for key, value in (("session_id", "wrong"), ("agent_instance", "other"), ("authority_epoch", 2)):
            denied = handler.execute(dict(request, context=dict(request["context"], **{key: value})))
            self.assertEqual((denied.ok, denied.code), (False, "NO_LEASE"))
        outcome = handler.execute(request)
        self.assertEqual((outcome.ok, outcome.code), (True, "OK"))
        validate_contract("vp_perception_call.wake.result/v1", outcome.result)
        self.assertEqual(outcome.result["to"], "ACTIVE")

    def test_negative_wire_target_and_signed_out_session(self):
        with self.assertRaisesRegex(LifecycleError, "identity_mismatch"):
            self.p.vp_perception_call(dict(op="wake", reason="fixture", session_id="other"))
        self.sample = None
        with self.assertRaisesRegex(LifecycleError, "HOLD"):
            self.call("status")

    def test_no_notification_or_unreconciled_restart_can_wake(self):
        self.assertIsNone(self.p.wake_for_verified())
        self.assertEqual(self.state(), "OFF")
        with self.assertRaisesRegex(LifecycleError, "HOLD"):
            self.provision()

    def durable_source(self, name="p2.sqlite"):
        source = RecoverableDemandCoordinator(
            self.root / name, self.binding, interests=["VERIFIED", "DISPLAY"],
            authorize=lambda row: row in (self.demand, self.recording), drain=lambda: True)
        self.addCleanup(source.retire_owner)
        return source

    def start_durable_goal(self, **options):
        # The original fixture has only provisioned OFF. Use a separate broker session.
        self.b.close()
        self.b = Broker(self.root / "persistent")
        self.addCleanup(self.b.close)
        self.b.bind("session", self.binding, lease_state="AGENT")
        self.src = self.durable_source()
        self.p = self.provision(**options)
        return self.goal(self.call("wake")["fresh_observation_id"])

    def restart_owners(self):
        self.src.retire_owner()
        self.b.close()
        self.b = Broker(self.root / "persistent")
        self.addCleanup(self.b.close)
        self.src = self.durable_source()
        return GoalMachine(self.root / "goal", "g")

    def start_recoverable_active_goal(self):
        self.start_durable_goal()
        self.call("sleep")
        self.call("wake")

    def assert_recovery_refuses_privacy_hold(self, **ports):
        ledger = GoalMachine(self.root / "goal", "g").replay()
        broker_path = self.root / "persistent/events.jsonl"
        prefix = broker_path.read_bytes()
        goal = self.restart_owners()
        readback = []

        def recover_observation():
            readback.append(True)
            return self.p._observation

        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision(recover_goal=goal, recover_observation=recover_observation, **ports)
        self.assertEqual(readback, [], "readback reached a protected producer")
        self.assertEqual(goal.replay(), ledger)
        self.assertEqual(broker_path.read_bytes(), prefix)

    def test_privacy_latch_survives_replacing_active_owner(self):
        self.start_recoverable_active_goal()
        self.p.hold_for_privacy()
        self.p.hold_for_privacy()
        self.assert_recovery_refuses_privacy_hold()
        rows = (self.root / "persistent/events.jsonl").read_bytes().splitlines()
        notices = [row for row in map(json.loads, rows) if row["rec"] == "HUMAN_NEEDED"]
        self.assertEqual([(row["session_id"], row["goal_id"], row["reason"]) for row in notices],
                         [("session", "g", "login")])

    def test_login_notice_fences_recovery_before_vision_can_latch(self):
        self.start_recoverable_active_goal()
        self.b.human_needed(self.owner, "session", "g", "login")
        unused = Mock(return_value=True)
        self.assert_recovery_refuses_privacy_hold(confirm_privacy_clear=unused)
        unused.assert_not_called()

    def prepare_privacy_handback(self):
        goal = self.start_durable_goal()
        self.p.hold_for_privacy()
        taken = self.b.fence("session", self.binding["authority_epoch"], "login", {})
        lease = self.b.prepare_return("session", taken["lease"]["epoch_after"])
        returned = self.b.record_return(lease)
        self.binding = self.b.binding(self.binding["surface_id"])
        fresh = FreshObservation("session", self.binding["surface_id"], self.binding["incarnation"],
                                 lease["epoch_after"], "fresh", time.monotonic_ns(), 2)
        return goal, returned, fresh

    def assert_handback_requires_fresh_a2(self):
        goal, returned, fresh = self.prepare_privacy_handback()
        self.b.finish_return(returned, fresh)
        self.src.retire_owner()
        self.src = self.durable_source("handback.sqlite")
        readbacks = []

        def clear(binding):
            readbacks.append(binding)
            return False

        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision(recover_goal=goal, confirm_privacy_clear=clear)
        self.assertEqual(readbacks, [self.binding])

        # A clear A2 check releases only the privacy gate. The old goal and
        # stream still require explicit reconciliation at the new epoch.
        footprint = PrivacyBinding(self.binding["surface_id"], self.binding["incarnation"],
                                   self.binding["authority_epoch"], "fixture", "bound-window")
        sentinel = PrivacySentinel(
            footprint, lambda bound: PrivacyMetadata(bound, 1, PrivacyState.CLEAR),
            record_gap=self.fail, request_human=self.fail)

        def check_metadata(binding):
            self.assertEqual(binding, self.binding)
            return sentinel.observe(lambda _: True, lambda clear: clear)

        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.provision(recover_goal=goal, confirm_privacy_clear=check_metadata)

    def test_unfinished_operator_handback_refuses_before_a2_readback(self):
        goal, _, _ = self.prepare_privacy_handback()
        self.src.retire_owner()
        self.src = self.durable_source("handback.sqlite")
        clear = Mock(return_value=True)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision(recover_goal=goal, confirm_privacy_clear=clear)
        clear.assert_not_called()

    def test_operator_readback_for_another_surface_refuses_before_a2_readback(self):
        goal, returned, fresh = self.prepare_privacy_handback()
        self.b.finish_return(returned, replace(fresh, surface_id="different-surface"))
        self.src.retire_owner()
        self.src = self.durable_source("handback.sqlite")
        clear = Mock(return_value=True)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision(recover_goal=goal, confirm_privacy_clear=clear)
        clear.assert_not_called()

    def test_finished_operator_handback_requires_fresh_clear_a2(self):
        self.assert_handback_requires_fresh_a2()

    def test_mutant_bypassing_clear_a2_after_operator_handback_is_killed(self):
        recover_privacy = PerceptionLifecycle._recover_privacy

        def skip_clear(owner, confirm_clear):
            def always_clear(binding):
                confirm_clear(binding)
                return True
            return recover_privacy(owner, always_clear)

        with patch.object(PerceptionLifecycle, "_recover_privacy", skip_clear):
            with self.assertRaisesRegex(AssertionError, 'does not match "HOLD"'):
                self.assert_handback_requires_fresh_a2()

    def assert_off_privacy_fenced(self):
        self.call("wake")
        self.p.hold_for_privacy()
        self.stream.close()
        metadata = Mock(return_value=False)
        with patch.object(self.stream, "wake", side_effect=AssertionError), patch.object(
                self.stream, "read", side_effect=AssertionError):
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.provision(recover_goal=None, confirm_privacy_clear=metadata)
            metadata.assert_not_called()
            taken = self.b.fence("session", self.binding["authority_epoch"], "login", {})
            lease = self.b.prepare_return("session", taken["lease"]["epoch_after"])
            returned = self.b.record_return(lease)
            self.binding = self.b.binding(self.binding["surface_id"])
            fresh = FreshObservation("session", self.binding["surface_id"], self.binding["incarnation"],
                                     lease["epoch_after"], "returned", time.monotonic_ns(), 1)
            self.b.finish_return(returned, fresh)
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.provision(recover_goal=None, confirm_privacy_clear=metadata)
            metadata.assert_called_once_with(self.binding)

    def test_off_start_before_and_after_handback_requires_clear_a2(self):
        self.assert_off_privacy_fenced()

    def test_mutant_off_start_skips_privacy_recovery(self):
        with patch.object(PerceptionLifecycle, "_recover_privacy", return_value=None):
            with self.assertRaisesRegex(AssertionError, 'does not match "HOLD"'):
                self.assert_off_privacy_fenced()

    def assert_reconnect_privacy_fenced(self):
        goal = self.goal(self.call("wake")["fresh_observation_id"])
        self.call("sleep")
        self.b.close()
        replacement = Broker(self.root / "rt")
        self.addCleanup(replacement.close)
        replacement.human_needed(self.owner, "session", "g", "login")
        with patch.object(self.stream, "read", side_effect=AssertionError), patch.object(
                self.stream, "wake", side_effect=AssertionError):
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.p.reconnect_broker(replacement, goal)
            for operation in (lambda: self.call("wake"), self.p.wake_for_verified, lambda: self.p.read_frame("old"),
                              lambda: self.p.run_agent("decision", "old", self.fail),
                              lambda: self.p.bind_goal(goal)):
                with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                    operation()

    def test_reconnect_reads_replayed_session_notice_before_admission(self):
        self.assert_reconnect_privacy_fenced()

    def test_mutant_reconnect_skips_privacy_recovery(self):
        with patch.object(PerceptionLifecycle, "_recover_privacy", return_value=None):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.assert_reconnect_privacy_fenced()

    def test_handback_cannot_discard_unacknowledged_publication(self):
        goal = self.start_durable_goal(on_transition=Mock())
        self.call("sleep")
        with patch.object(self.p, "_on_transition", side_effect=OSError("lost acknowledgement")):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("wake")
        self.p.hold_for_privacy()
        take = self.b.fence("session", self.binding["authority_epoch"], "login", {})
        goal.human_lease(take)
        lease = self.b.prepare_return("session", take["lease"]["epoch_after"])
        returned = self.b.record_return(lease)
        self.binding = self.b.binding(self.binding["surface_id"])
        fresh = FreshObservation("session", self.binding["surface_id"], self.binding["incarnation"],
                                 lease["epoch_after"], "returned", time.monotonic_ns(), goal.status()["generation"])
        goal.human_lease(returned, observation_id=fresh.observation_id, generation=fresh.generation)
        self.b.finish_return(returned, fresh)
        self.src.retire_owner()
        self.src = self.durable_source("after-return.sqlite")
        readback = Mock(side_effect=AssertionError("unacknowledged publication bypassed"))
        publish = Mock()
        for requested in (None, goal):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.provision(recover_goal=requested, confirm_privacy_clear=lambda _: True,
                               recover_observation=readback, on_transition=publish)
        readback.assert_not_called()
        publish.assert_not_called()
        with patch.object(self.src, "publication_acknowledged", return_value=True):
            with self.assertRaisesRegex(AssertionError, "unacknowledged publication bypassed"):
                self.provision(recover_goal=goal, confirm_privacy_clear=lambda _: True,
                               recover_observation=readback, on_transition=publish)

    def test_pre_goal_handback_adopts_verified_new_stream_and_attaches_ready_goal(self):
        old = self.call("wake")["fresh_observation_id"]
        self.p.hold_for_privacy()
        self.stream.close()
        take = self.b.fence("session", self.binding["authority_epoch"], "login", {})
        lease = self.b.prepare_return("session", take["lease"]["epoch_after"])
        self.binding = self.b.binding(self.binding["surface_id"])
        self.replace_stream()
        obs = self.stream.wake()
        self.assertNotEqual(old, obs.observation_id)
        fresh = FreshObservation("session", self.binding["surface_id"], self.binding["incarnation"],
                                 lease["epoch_after"], obs.observation_id, time.monotonic_ns(), obs.stream_generation)
        self.b.finish_return(self.b.record_return(lease), fresh)
        self.src = self.durable_source("returned.sqlite")
        readback = Mock(return_value=obs)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision(confirm_privacy_clear=lambda _: False, recover_observation=readback)
        readback.assert_not_called()
        self.p = self.provision(confirm_privacy_clear=lambda _: True, recover_observation=readback)
        readback.assert_called_once_with()
        self.assertEqual(self.call("status")["fresh_observation_id"], fresh.observation_id)
        self.assertEqual(self.goal(fresh.observation_id).replay()["phase"], "READY")
        self.assertEqual(self.p.read_frame(fresh.observation_id), b"red" * 32)

    def test_prior_principal_notice_fences_only_its_session(self):
        # The current bind API fixes the owner for the life of a session.
        # Its replay reader also accepts notices written by an earlier principal.
        self.b._write("HUMAN_NEEDED", session_id="elsewhere", goal_id="g", reason="login", raised_by="prior")
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.provision()
        self.b._write("HUMAN_NEEDED", session_id="session", goal_id="g", reason="login", raised_by="prior")
        self.b.close()
        self.b = Broker(self.root / "rt")
        self.addCleanup(self.b.close)
        with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
            self.provision()

    def test_open_timeline_gap_survives_gap_end_without_handback(self):
        from recorder.timeline import TimelinePosition, TimelineRecorder
        self.b = Broker(self.root / "recorder-only")
        self.addCleanup(self.b.close)
        self.b.bind("session", self.binding, lease_state="AGENT")
        recorder = TimelineRecorder(surface_id=self.binding["surface_id"], capsule_id="capsule",
                                    incarnation=self.binding["incarnation"], register=self.src.register,
                                    verify_ram_frame=self.fail)
        def position():
            return TimelinePosition(time.monotonic_ns(), "capsule", self.binding["incarnation"])
        with recorder.recording(self.recording):
            recorder.set_gap(position(), "privacy", active=True)
            recorder.set_gap(position(), "privacy", active=False)
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.provision(privacy_timeline=recorder.snapshot, confirm_privacy_clear=self.fail)

    def complete_off_handback(self):
        self.p.hold_for_privacy()
        take = self.b.fence("session", self.binding["authority_epoch"], "login", {})
        lease = self.b.prepare_return("session", take["lease"]["epoch_after"])
        returned = self.b.record_return(lease)
        self.binding = self.b.binding(self.binding["surface_id"])
        fresh = FreshObservation("session", self.binding["surface_id"], self.binding["incarnation"],
                                 lease["epoch_after"], "returned", time.monotonic_ns(), 1)
        self.b.finish_return(returned, fresh)

    def test_clear_handback_permits_rebinding_never_started_off_surface(self):
        self.complete_off_handback()
        metadata = Mock(return_value=True)
        self.p = self.provision(confirm_privacy_clear=metadata)
        self.assertEqual(self.call("status")["to"], "OFF")
        metadata.assert_called_once_with(self.binding)

    def test_goal_free_rebind_requires_handback_and_a_newer_authority_epoch(self):
        self.complete_off_handback()
        prior = self.b.perception_state(self.binding["surface_id"])
        metadata = Mock(return_value=True)
        with patch.object(PerceptionLifecycle, "_recover_privacy", return_value=None):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.provision(confirm_privacy_clear=metadata)
        metadata.assert_not_called()

        same_epoch = dict(prior, authority_epoch=self.binding["authority_epoch"])
        with patch.object(self.b, "perception_state", return_value=same_epoch):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.provision(confirm_privacy_clear=metadata)
        metadata.assert_called_once_with(self.binding)
        self.assertEqual(self.b.perception_state(self.binding["surface_id"]), prior)

    def test_new_surface_in_held_session_refuses_initial_off(self):
        self.p.hold_for_privacy()
        self.binding = dict(self.binding, surface_id="next-surface", endpoint="next-endpoint")
        self.b.bind("session", self.binding)
        self.demand = dict(self.demand, surface_id=self.binding["surface_id"])
        self.src = DemandCoordinator(self.binding["surface_id"], interests=["VERIFIED"],
                                     authorize=self.fail, drain=self.fail)
        with patch.object(self.stream, "status", side_effect=AssertionError):
            with self.assertRaisesRegex(LifecycleError, "^HUMAN_NEEDED$"):
                self.provision(confirm_privacy_clear=self.fail)

    def test_unrelated_human_notice_does_not_fence_recovery(self):
        self.start_recoverable_active_goal()
        self.b.human_needed(self.owner, "session", "g", "stuck")
        live = self.p._observation
        goal = self.restart_owners()
        self.p = self.provision(recover_goal=goal, recover_observation=lambda: live)
        self.assertEqual(self.call("status")["fresh_observation_id"], live.observation_id)

    def test_mutant_volatile_privacy_latch_is_killed_on_replacement(self):
        with patch.object(Broker, "human_needed", return_value=None):
            with self.assertRaisesRegex(AssertionError, "LifecycleError not raised"):
                self.test_privacy_latch_survives_replacing_active_owner()

    def test_restart_replays_unpublished_sleep_before_next_wake(self):
        delivered = []
        def publish(receipt, proof):
            delivered.append((receipt, proof))

        goal = self.start_durable_goal(on_transition=publish)
        original = []
        def interrupted(receipt, proof):
            original.append((receipt, proof))
            raise OSError("before append")

        with patch.object(self.p, "_on_transition", interrupted):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("sleep")
        ledger = goal.replay()
        broker_path = self.root / "persistent/events.jsonl"
        prefix = broker_path.read_bytes()
        self.assertEqual(self.state(), "ASLEEP")
        self.sample = replace(self.sample, resident_bytes=self.sample.resident_bytes + 100)
        with self.src.register(self.recording):
            goal = self.restart_owners()
            with patch.object(self.stream, "pause", side_effect=AssertionError("drain repeated")):
                self.p = self.provision(recover_goal=goal, on_transition=publish)
            self.assertEqual(delivered[1:], original, "unpublished sleep evidence was lost")
            self.assertNotEqual(self.src.snapshot(), original[0][1].demand_snapshot)
            self.assertEqual(goal.replay(), ledger)
            self.assertEqual(broker_path.read_bytes(), prefix)
            self.assertTrue(self.call("status")["complete"])
            goal = self.restart_owners()
            self.p = self.provision(recover_goal=goal, on_transition=publish)
            self.call("wake")
            self.assertEqual([r["to"] for r, _ in delivered], ["ACTIVE", "ASLEEP", "ACTIVE"])

    def test_mutant_volatile_publication_loses_sleep_on_restart(self):
        with patch.object(PerceptionLifecycle, "_recover_publication", return_value=None):
            with self.assertRaisesRegex(AssertionError, "unpublished sleep evidence was lost"):
                self.test_restart_replays_unpublished_sleep_before_next_wake()

    def test_recover_off_between_goal_and_broker_commits(self):
        delivered = []
        def publish(receipt, proof):
            delivered.append((receipt, proof))

        goal = self.start_durable_goal(on_transition=publish)
        with patch.object(self.b, "perception_transition", side_effect=OSError("before broker append")):
            with self.assertRaises(OSError):
                self.call("off")
        self.assertEqual(self.state(), "ACTIVE")
        ledger = goal.replay()
        self.assertEqual(ledger["perception"], "OFF")
        goal = self.restart_owners()
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.provision(recover_goal=goal)
        self.assertEqual(self.state(), "ACTIVE")
        with patch.object(self.stream, "close", side_effect=AssertionError("Reap repeated")):
            self.p = self.provision(recover_goal=goal, on_transition=publish)
        self.assertEqual(goal.replay(), ledger)
        receipt, proof = delivered[-1]
        self.assertEqual((receipt["from"], receipt["to"]), ("ACTIVE", "OFF"))
        self.assertEqual(proof.reap, {"reaped": receipt["scrub"]["buffers"], "retained": []})
        self.assertEqual(proof.broker_records[-1]["perception"], self.b.perception_state(self.p.surface))

    def test_restart_after_p7_append_before_lifecycle_ack_reuses_reference(self):
        from bus.journal import Journal
        from perception.receipts import append_transition

        references = []
        def publish(receipt, proof):
            ref = append_transition(Journal(self.root), receipt, proof, agent="broker")
            references.append(ref)

        goal = self.start_durable_goal(on_transition=publish)
        with patch.object(self.src, "acknowledge_publication", side_effect=OSError("lost P4 ack")):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("sleep")
        raw = (self.root / "lineage.jsonl").read_bytes()
        first, second = raw.splitlines(keepends=True)
        self.assertEqual(references[-1], dict(lamport=2, prev_hash=sha256(first).hexdigest(),
                                             event_hash=sha256(second).hexdigest()))
        ledger = goal.replay()
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.call("wake")
        goal = self.restart_owners()
        self.p = self.provision(recover_goal=goal, on_transition=publish)
        self.assertEqual(len(references), 3)
        self.assertEqual(references[-1], references[-2])
        self.assertEqual((self.root / "lineage.jsonl").read_bytes(), raw)
        self.assertEqual(goal.replay(), ledger)
        self.call("wake")
        self.assertEqual([e["payload"]["to"] for e in Journal(self.root).verify()],
                         ["ACTIVE", "ASLEEP", "ACTIVE"])

    def test_pending_wake_requires_publisher_and_exact_surviving_observation(self):
        delivered = []
        def publish(receipt, proof):
            delivered.append((receipt, proof))

        self.start_durable_goal(on_transition=publish)
        self.call("sleep")
        with patch.object(self.p, "_on_transition", side_effect=OSError("publisher unavailable")):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.call("wake")
        original = self.p._observation
        goal = self.restart_owners()
        with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
            self.provision(recover_goal=goal, recover_observation=lambda: original)
        replaced = replace(original, frame_sha256="0" * 64)
        with self.assertRaisesRegex(LifecycleError, "^STALE_FRAME$"):
            self.provision(recover_goal=goal, recover_observation=lambda: replaced, on_transition=publish)
        with patch.object(self.src, "acknowledge_publication", side_effect=OSError("ack unavailable")):
            with self.assertRaisesRegex(LifecycleError, "^HOLD$"):
                self.provision(recover_goal=goal, recover_observation=lambda: original, on_transition=publish)
        with patch.object(self.stream, "wake", side_effect=AssertionError("acquisition repeated")):
            self.p = self.provision(recover_goal=goal, recover_observation=lambda: original,
                                    on_transition=publish)
        self.assertEqual(delivered[-1][1].observation, original)
        self.assertEqual(self.call("status")["fresh_observation_id"], original.observation_id)

    def test_external_publish_during_drain_survives_p2_restart_and_racing_wakes(self):
        self.start_durable_goal()
        draining, release = Event(), Event()
        self.drain = lambda: draining.set() or release.wait(5)
        old_source = self.src
        with old_source.register(self.recording) as old_handle, ThreadPoolExecutor(2) as pool:
            sleeping = pool.submit(self.call, "sleep")
            self.assertTrue(draining.wait(2))
            try:
                old_source.publish(old_handle, 1, "external-before-crash")
            finally:
                release.set()
            self.assertEqual(sleeping.result()["retained"], ["recorder"])
            goal = self.restart_owners()
            self.p = self.provision(recover_goal=goal)
            with self.assertRaisesRegex(DemandError, "NO_LEASE"):
                old_source.publish(old_handle, 2, "stale")
            with self.assertRaisesRegex(DemandError, "NO_LEASE"):
                self.src.publish(old_handle, 2, "foreign")
            with self.src.resume_subscription(self.recording), patch.object(
                    self.stream, "wake", wraps=self.stream.wake) as capture:
                barrier = Barrier(2)
                def raced(op):
                    barrier.wait(3)
                    return op()
                agent = pool.submit(raced, lambda: self.call("wake"))
                external = pool.submit(raced, self.p.wake_for_verified)
                work, receipt = external.result()
                self.assertEqual(work.reference, "external-before-crash")
                self.assertEqual(agent.result()["fresh_observation_id"], receipt["fresh_observation_id"])
                self.p.complete_verified(work)
                capture.assert_called_once()
                self.assertIsNone(self.p.wake_for_verified())
            ledger = goal.replay()
            self.assertEqual((ledger["t"], ledger["q"], ledger["flight"]), (1, 0, 1))
            self.assertEqual(list(ledger["work_flights"].values()), [1])
            self.assertEqual(list(ledger["work"].values()), ["settled"])

    def test_restart_between_external_and_agent_wake_keeps_delivery_and_credit(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as source:
            self.call("sleep")
            self.src.publish(source, 1, "delivered-once")
            work, receipt = self.p.wake_for_verified()
            token = self.src.work_token(work)
            live_obs = self.p._observation
            goal = self.restart_owners()
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.provision(recover_goal=goal)
            with patch.object(self.stream, "wake", side_effect=AssertionError("second acquisition")):
                self.p = self.provision(recover_goal=goal, recover_observation=lambda: live_obs)
                with self.assertRaisesRegex(LifecycleError, "NO_LEASE"):
                    self.p.run_agent("act", live_obs.observation_id, self.fail)
                self.assertIsNone(self.p.wake_for_verified())
                self.assertEqual(self.call("wake")["fresh_observation_id"], receipt["fresh_observation_id"])
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.p.reconcile_verified(token, lambda w, f: False)
                self.assertEqual(self.p.reconcile_verified(token, lambda w, f:
                                 w.reference == work.reference and f == 1), "SETTLED")
                self.assertEqual(self.p.reconcile_verified(token, self.fail), "SETTLED")
            self.assertEqual(goal.replay()["work"], {token: "settled"})
            self.assertEqual(self.src.snapshot()["claimed"], 0)

    def test_mutant_dropped_queued_work_fails_restart_delivery_oracle(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as source:
            self.call("sleep")
            self.src.publish(source, 1, "must-survive")
            original = RecoverableDemandCoordinator._restore
            def drop_queue(owner, saved):
                saved["queues"]["VERIFIED"].clear()
                original(owner, saved)
            with patch.object(RecoverableDemandCoordinator, "_restore", autospec=True,
                              side_effect=drop_queue) as mutant:
                with self.assertRaisesRegex(DemandError, "HOLD"):
                    self.restart_owners()
                mutant.assert_called_once()

    def test_restart_rejects_substitute_source_and_unsettled_borrow(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as handle:
            self.src.publish(handle, 1, "borrowed")
            work, = self.src.claim("VERIFIED", budget=1, timeout=0)
            with self.src.borrow(work, 11):
                self.call("sleep")
                original = self.src
                self.src = self.durable_source("foreign.sqlite")
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.provision(recover_goal=goal)
                original.retire_owner()
                with self.assertRaisesRegex(DemandError, "HOLD"):
                    self.durable_source()
        self.assertEqual(goal.replay()["q"], 1)

    def test_two_exact_deadline_expirations_quarantine_without_publishing(self):
        goal = self.start_durable_goal()
        self.call("sleep")
        now = self.p._clock()
        original = self.stream.wake
        def at_deadline():
            nonlocal now
            obs = original()
            now += 3_000_000_000
            return obs
        with patch.object(self.p, "_clock", side_effect=lambda: now), patch.object(
                self.stream, "wake", side_effect=at_deadline) as capture:
            for _ in range(2):
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.call("wake")
            self.assertEqual(capture.call_count, 2)
        self.assertEqual(self.state(), "QUARANTINED")
        self.assertEqual(goal.replay()["wake_failures"], 2)

    def test_frozen_goal_off_replays_missed_broker_ack_without_second_charge(self):
        goal = self.start_durable_goal()
        self.call("sleep")
        before = goal.replay()
        with patch.object(self.b, "perception_transition", side_effect=OSError("broker interrupted")):
            with self.assertRaises(OSError):
                self.call("off")
        self.assertEqual(goal.replay()["perception"], "OFF")
        goal = self.restart_owners()
        self.p = self.provision(recover_goal=goal)
        self.assertEqual(self.state(), "OFF")
        for key in ("t", "r", "d", "q", "a", "flight"):
            self.assertEqual(goal.replay()[key], before[key])
        self.assertTrue(self.call("wake")["fresh_observation_id"])

    def test_published_work_survives_abrupt_publisher_process_exit(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording):
            self.call("sleep")
            self.src.retire_owner()
            command = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
from perception.lifecycle import RecoverableDemandCoordinator
binding, demand = map(json.loads, sys.argv[3:])
owner = RecoverableDemandCoordinator(sys.argv[2], binding, interests=["VERIFIED", "DISPLAY"],
                                     authorize=lambda row: row == demand, drain=lambda: True)
handle = owner.resume_subscription(demand)
owner.publish(handle.__enter__(), 1, "published-before-exit")
os._exit(0)
"""
            child = subprocess.run([sys.executable, "-I", "-B", "-c", command, str(DIRECT),
                                    str(self.root / "p2.sqlite"), json.dumps(self.binding),
                                    json.dumps(self.recording)], check=False, timeout=10)
            self.assertEqual(child.returncode, 0)
            goal = self.restart_owners()
            self.p = self.provision(recover_goal=goal)
            work, _ = self.p.wake_for_verified()
            self.assertEqual(work.reference, "published-before-exit")
            self.p.complete_verified(work)
            self.assertIsNone(self.p.wake_for_verified())
            self.assertEqual(list(goal.replay()["work"].values()), ["settled"])

    def test_restart_between_p2_credit_release_and_goal_settlement(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as publisher:
            self.call("sleep")
            self.src.publish(publisher, 1, "acknowledged-by-consumer")
            work, _ = self.p.wake_for_verified()
            token = self.src.work_token(work)
            obs = self.p._observation
            with patch.object(goal, "settle_external_work", side_effect=OSError("owner exited")):
                with self.assertRaises(OSError):
                    self.p.complete_verified(work)
            self.assertEqual(self.src.snapshot()["claimed"], 0)
            goal = self.restart_owners()
            self.p = self.provision(recover_goal=goal, recover_observation=lambda: obs)
            self.assertEqual(self.p.reconcile_verified(token, self.fail), "SETTLED")
            self.assertEqual(goal.replay()["work"], {token: "settled"})
            self.assertIsNone(self.p.wake_for_verified())

    def test_mutant_extended_deadline_admits_an_expired_observation(self):
        now = self.p._clock()
        original = self.stream.wake
        def expired():
            nonlocal now
            obs = original()
            now += 3_000_000_000
            return obs
        with patch.object(self.p, "_clock", side_effect=lambda: now), patch.object(
                self.stream, "wake", side_effect=expired) as capture, patch(
                "perception.lifecycle.machine.WAKE_NS", 3_000_000_001):
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.call("wake")
            capture.assert_called_once()

    def test_restart_inside_drain_keeps_external_work_and_finishes_original_sleep(self):
        goal = self.start_durable_goal()
        captured = self.p._observation
        with self.src.register(self.recording) as handle:
            def interrupted():
                self.src.publish(handle, 1, "during-drain")
                raise ConnectionError("drain owner exited")
            self.drain = interrupted
            with self.assertRaises(ConnectionError):
                self.call("sleep")
            self.assertEqual(goal.replay()["t"], 0)
            self.assertIn("pending_rest", goal.replay())
            goal = self.restart_owners()
            self.drain = lambda: True
            self.p = self.provision(recover_goal=goal, recover_observation=lambda: captured)
            self.assertEqual(self.state(), "ASLEEP")
            self.assertNotIn("pending_rest", goal.replay())
            self.assertEqual(goal.replay()["t"], 1)
            with patch.object(self.stream, "wake", wraps=self.stream.wake) as capture:
                work, active = self.p.wake_for_verified()
                self.assertEqual(work.reference, "during-drain")
                self.assertEqual(self.call("wake")["fresh_observation_id"], active["fresh_observation_id"])
                self.p.complete_verified(work)
                capture.assert_called_once()

    def test_restart_after_p2_claim_before_goal_admission_restores_only_unissued_work(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as source:
            self.call("sleep")
            self.src.publish(source, 1, "unissued")
            with patch.object(goal, "claim_external_wake", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    self.p.wake_for_verified()
            self.assertEqual(self.src.snapshot()["claimed"], 1)
            goal = self.restart_owners()
            self.p = self.provision(recover_goal=goal)
            work, _ = self.p.wake_for_verified()
            self.assertEqual(work.reference, "unissued")
            self.p.complete_verified(work)
            self.assertIsNone(self.p.wake_for_verified())
            self.assertEqual(list(goal.replay()["work_flights"].values()), [1])

    def test_interrupted_wake_needs_quiescence_and_is_charged_once_without_redispatch(self):
        goal = self.start_durable_goal()
        with self.src.register(self.recording) as source:
            self.call("sleep")
            self.src.publish(source, 1, "external")
            with patch.object(self.src, "register", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    self.p.wake_for_verified()
            ledger = goal.replay()
            self.assertEqual(ledger["perception"], "WAKING")
            token, = ledger["work"]
            goal = self.restart_owners()
            with patch.object(self.stream, "wake", side_effect=AssertionError("replayed acquisition")):
                with self.assertRaisesRegex(LifecycleError, "HOLD"):
                    self.provision(recover_goal=goal, confirm_quiescent=lambda *_: False)
                self.assertEqual(goal.replay(), ledger)
                self.p = self.provision(recover_goal=goal, confirm_quiescent=lambda flight, binding:
                                        flight == ledger["flight"] and binding == self.binding)
                failed = goal.replay()
                self.assertEqual((failed["perception"], failed["t"], failed["wake_failures"]),
                                 ("ASLEEP", ledger["t"] + 1, 1))
                goal = self.restart_owners()
                self.p = self.provision(recover_goal=goal)
                self.assertEqual(goal.replay(), failed)
                self.assertEqual(self.p.reconcile_verified(token, lambda w, f: w.reference == "external"
                                                           and f == ledger["flight"]), "SETTLED")
            self.assertTrue(self.call("wake")["fresh_observation_id"])

    def test_failed_publication_keeps_goal_generation_recoverable(self):
        self.start_durable_goal()
        self.call("sleep")
        with patch.object(self.p, "_receipt", side_effect=LifecycleError("HOLD")):
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("wake")
        goal = self.restart_owners()
        self.p = self.provision(recover_goal=goal)
        self.assertEqual(self.state(), "ASLEEP")
        self.assertEqual(goal.replay()["generation"], 1)
        self.assertTrue(self.call("wake")["fresh_observation_id"])

    def test_negative_reused_observation(self):
        old = self.call("wake")["fresh_observation_id"]
        self.call("sleep")
        self.call("wake")
        self.call("sleep")
        original = self.stream.wake
        def stale():
            obs = replace(original(), observation_id=old)
            # A broken producer agrees with its own recycled ID. Its valid
            # frame and fresh generation cannot make an old caller's ID fresh.
            self.stream._observation = obs
            return obs
        with patch.object(self.stream, "wake", side_effect=stale):
            with self.assertRaisesRegex(LifecycleError, "STALE_FRAME"):
                self.call("wake")
        self.assertNotEqual(self.state(), "ACTIVE")

    def test_mutant_css_only_sleep(self):
        self.call("wake")
        with patch.object(self.stream, "pause", return_value=True) as css_only:
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("sleep")
            css_only.assert_called_once()
        self.assertEqual(self.state(), "ACTIVE")

    def test_mutant_wake_without_observation(self):
        with patch.object(self.stream, "wake", return_value=None) as no_observation:
            with self.assertRaisesRegex(LifecycleError, "STALE_FRAME"):
                self.call("wake")
            no_observation.assert_called_once()
        self.assertEqual(self.state(), "OFF")

    def test_mutant_close_application_on_off(self):
        self.call("wake")
        original = self.stream.close
        def close_both():
            original()
            self.sample = None
        with patch.object(self.stream, "close", side_effect=close_both) as close_app:
            with self.assertRaisesRegex(LifecycleError, "HOLD"):
                self.call("off")
            close_app.assert_called_once()
        self.assertNotEqual(self.state(), "OFF")

    def test_mutant_session_state_changed_by_sleep(self):
        self.call("wake")
        original = self.b.perception_transition

        def read_session():
            session = example("vp.session")
            lease = self.b.lease("session")
            session.update(session_id="session", incarnation=self.binding["incarnation"], state=lease["state"])
            session["lease"] = dict(authority_epoch=lease["authority_epoch"],
                                    holder=lease["holder"] if lease["state"] == "AGENT" else None)
            validate_contract("vp.session/v1", session)
            return session

        def mutate_session(record):
            row = original(record)
            if record["state"] == "ASLEEP":
                self.b.human_lease("session", "HUMAN", 1, "operator")
            return row

        def independent_oracle():
            before = read_session()
            self.call("sleep")
            self.assertEqual(read_session(), before)

        with patch.object(self.b, "perception_transition", side_effect=mutate_session):
            with self.assertRaises(AssertionError):
                independent_oracle()
        self.assertEqual(read_session()["state"], "HUMAN")
