"""Offline, deterministic lifecycle composition gates against real producers.

No X server, docker, physical network, frames on disk, or monkeypatched producer.
Tests import providers when executed, not at collection time.  A missing
provider/module fails loudly.  The injected channel is a fake external join
boundary whose results derive from observed teardown and outstanding sends.
"""
import hashlib
import threading
import unittest
from dataclasses import fields


class MemoryTransport:
    """Fake CDP transport: events, real send/close counters, and no network."""

    def __init__(self, target_id):
        self.target_id = target_id
        self.closed = False
        self.close_count = 0
        self.detach_ids = []
        self.sends = []
        self.active_sends = 0
        self._lock = threading.RLock()

    def send(self, method, params, session_id=None):
        with self._lock:
            self.active_sends += 1
        try:
            with self._lock:
                self.sends.append((method, dict(params), session_id))
                if method == "Target.detachFromTarget":
                    self.detach_ids.append(params["sessionId"])
                elif self.closed:
                    raise RuntimeError("transport already closed")
            return {"ok": True, "method": method, "session_id": session_id}
        finally:
            with self._lock:
                self.active_sends -= 1

    def close_target(self, canonical_id):
        with self._lock:
            if canonical_id != self.target_id or self.closed:
                raise RuntimeError("wrong or repeated external teardown")
            if self.active_sends:
                raise RuntimeError("close while command is still executing")
            self.closed = True
            self.close_count += 1


class JoinedFakeChannel:
    """Fake *trusted* external boundary; observable join, never ACK-only truth."""

    def __init__(self):
        self.calls = []

    def joined_target(self, handle, transport):
        self.calls.append(("target", handle, transport))
        return (handle.canonical_id == transport.target_id
                and transport.closed
                and transport.close_count == 1
                and transport.active_sends == 0)

    def joined_detachment(self, handle, transport, session_id):
        self.calls.append(("attachment", handle, transport))
        return (handle.canonical_id == transport.target_id
                and session_id in transport.detach_ids
                and not transport.closed
                and transport.active_sends == 0)

    def joined_quiescence(self, handle, transport):
        self.calls.append(("unowned", handle, transport))
        return (handle.canonical_id == transport.target_id
                and transport.active_sends == 0
                and not transport.closed)


class DeadTargetsUnitTests(unittest.TestCase):
    """Positive controls first (test_00..), then explicit negative controls."""

    def setUp(self):
        # Real producers, lazy-imported inside an undecorated test hook. No
        # ModuleNotFoundError catch, no success based on hasattr, no fallbacks.
        from wender.perceive.session_registry import (
            SessionRegistry, TargetRecord, Handle, FrameRef, Observation,
            QuarantinedTargetError, FrameValidationError, StaleHandleError,
        )
        from wender.perceive.harvest_broker import HarvestBroker
        from wender.act.target_lock_registry import (
            TargetLockRegistry, TargetLockRefused, TargetBusy,
        )
        from wender.perceive.proto1_dead_targets import (
            DeadTargetLifecycle, LifecycleRefused, ProductionDefect,
            TerminationProof, CommandReply,
        )
        self.SessionRegistry = SessionRegistry
        self.TargetRecord = TargetRecord
        self.Handle = Handle
        self.FrameRef = FrameRef
        self.Observation = Observation
        self.QuarantinedTargetError = QuarantinedTargetError
        self.FrameValidationError = FrameValidationError
        self.StaleHandleError = StaleHandleError
        self.TargetLockRefused = TargetLockRefused
        self.TargetBusy = TargetBusy
        self.TerminationProof = TerminationProof
        self.CommandReply = CommandReply
        self.LifecycleRefused = LifecycleRefused
        self.ProductionDefect = ProductionDefect
        self.now = [100]
        self.registry = SessionRegistry(clock=lambda: self.now[0])
        self.broker = HarvestBroker(self.registry, lane_capacity=2)
        self.locks = TargetLockRegistry(self.registry, clock=lambda: self.now[0])
        self.channel = JoinedFakeChannel()
        self.composer = DeadTargetLifecycle(self.registry, self.broker, self.locks,
                                            self.channel, clock=lambda: self.now[0])

    def owned(self, name="alpha"):
        t = MemoryTransport(name)
        h = self.composer.register("cdp_tab", name, t, owned=True)
        return h, t

    def stop_and_join(self, h):
        r = self.composer.stop(h)
        self.assertEqual(r.status, "stopped")
        close = self.composer.close(h)
        self.assertEqual(close.status, "closed")
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)

    def test_00_positive_owned_lifecycle_and_rebind(self):
        h, transport = self.owned()
        self.assertGreaterEqual(h.generation, 1)
        self.assertEqual([x.name for x in fields(self.TargetRecord)],
                         ["handle", "transport", "owned", "epoch", "session_id",
                          "attachment_owned", "closed"])
        record = self.registry.resolve(h)
        self.assertFalse(record.closed)
        self.assertEqual(record.epoch, 0)
        lease = self.composer.acquire_actor(h, "original", expires_ns=600)
        reply = self.composer.issue(h, lease, "Input.dispatchMouseEvent", {"x": 2})
        self.assertEqual(reply.epoch, 0)
        self.assertEqual(reply.sequence, 1)
        self.assertEqual(self.composer.publish(reply)["ok"], True)
        self.assertEqual(len(transport.sends), 1)
        stopped = self.composer.stop(h)
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(transport.close_count, 0)
        tombstone, _, pending = self.registry.tombstone(h)
        self.assertTrue(tombstone.closed)
        self.assertTrue(pending)
        with self.assertRaises(self.QuarantinedTargetError):
            self.registry.register("cdp_tab", "alpha", MemoryTransport("alpha"), owned=True)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.prove_termination(h)
        with self.assertRaises(self.TargetLockRefused):
            self.locks.release(lease)
        close = self.composer.close(h)
        self.assertEqual(close.status, "closed")
        self.assertEqual(close.handle, h)
        self.assertEqual(close.generation, h.generation)
        self.assertEqual(close.actions_done, ("close_target",))
        self.assertEqual(close.final_state, "target_closed")
        self.assertEqual(transport.close_count, 1)
        self.assertTrue(self.registry.tombstone(h)[2])
        proof = self.composer.prove_termination(h)
        self.assertIsInstance(proof, self.TerminationProof)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])
        self.assertFalse(self.composer.snapshot(h)["registry_quarantined"])
        successor_transport = MemoryTransport("alpha")
        successor = self.composer.register("cdp_tab", "alpha", successor_transport, owned=True)
        self.assertGreater(successor.generation, h.generation)
        self.assertNotEqual(successor, h)
        self.assertIs(self.registry.resolve(successor).transport, successor_transport)
        with self.assertRaises(self.StaleHandleError):
            self.registry.resolve(h)

    def test_01_positive_caller_owned_target_detaches_owned_session_only(self):
        t = MemoryTransport("borrowed")
        h = self.composer.register("cdp_tab", "borrowed", t, owned=False,
                                   session_id="borrowed-session", attachment_owned=True)
        record = self.registry.resolve(h)
        self.assertFalse(record.owned)
        self.assertTrue(record.attachment_owned)
        self.assertEqual(record.session_id, "borrowed-session")
        self.composer.stop(h)
        self.assertEqual(t.sends, [])
        self.assertFalse(t.closed)
        self.composer.close(h)
        self.assertEqual(t.detach_ids, ["borrowed-session"])
        self.assertEqual([x[0] for x in t.sends], ["Target.detachFromTarget"])
        self.assertFalse(t.closed)
        self.assertEqual(t.close_count, 0)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])
        self.assertEqual([x[0] for x in self.channel.calls], ["attachment"])

    def test_closer_receipt_detachment_and_broker_stop_remains_absolute(self):
        t = MemoryTransport("borrowed")
        h = self.composer.register("cdp_tab", "borrowed", t, owned=False,
                                   session_id="owned-session", attachment_owned=True)
        self.composer.stop(h)
        self.assertEqual(self.broker.close(h).status, "cleanup_pending")
        self.assertEqual(t.sends, [])
        receipt = self.composer.close(h)
        self.assertEqual(receipt.handle, h)
        self.assertEqual(receipt.generation, h.generation)
        self.assertEqual(receipt.actions_done, ("detach_session",))
        self.assertEqual(receipt.final_state, "session_detached")
        self.assertIs(self.composer.close(h), receipt)
        self.assertEqual(t.detach_ids, ["owned-session"])
        self.assertFalse(t.closed)
        self.assertFalse(self.broker.reconcile_acknowledged_cleanup(h))
        self.assertEqual(self.broker.deferred_cleanup_count, 1)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertEqual(self.broker.deferred_cleanup_count, 0)
        self.assertEqual(t.detach_ids, ["owned-session"])

    def test_closer_refuses_before_stop_without_external_io(self):
        h, t = self.owned()
        with self.assertRaises(self.LifecycleRefused):
            self.composer.close(h)
        self.assertEqual(t.sends, [])
        self.assertEqual(t.close_count, 0)

    def test_closer_preserves_caller_owned_unattached_target(self):
        t = MemoryTransport("borrowed")
        h = self.composer.register("cdp_tab", "borrowed", t, owned=False)
        self.composer.stop(h)
        receipt = self.composer.close(h)
        self.assertEqual(receipt.actions_done, ())
        self.assertEqual(receipt.final_state, "caller_owned_target_preserved")
        self.assertEqual(t.sends, [])
        self.assertFalse(t.closed)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])

    def test_closer_failed_teardown_cannot_emit_success_or_retry(self):
        class FailingTransport(MemoryTransport):
            def close_target(self, canonical_id):
                self.close_count += 1
                raise RuntimeError("external close failed")

        t = FailingTransport("broken")
        h = self.composer.register("cdp_tab", "broken", t, owned=True)
        self.composer.stop(h)
        with self.assertRaisesRegex(RuntimeError, "external close failed"):
            self.composer.close(h)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.close(h)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.prove_termination(h)
        self.assertEqual(t.close_count, 1)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertFalse(self.broker.reconcile_acknowledged_cleanup(h))

    def test_02_positive_live_sibling_continues_while_target_quarantined(self):
        target, ta = self.owned("alpha")
        sibling, tb = self.owned("beta")
        self.composer.stop(target)
        self.assertEqual(ta.sends, [])
        self.assertTrue(self.registry.tombstone(target)[2])
        lease = self.composer.acquire_actor(sibling, "sibling", expires_ns=500)
        reply = self.composer.issue(sibling, lease, "Runtime.evaluate", {"expression": "1"})
        self.assertTrue(self.composer.publish(reply)["ok"])
        self.assertEqual([x[0] for x in tb.sends], ["Runtime.evaluate"])
        self.assertEqual(tb.close_count, 0)
        self.assertEqual(tb.detach_ids, [])
        self.assertIs(self.registry.resolve(sibling).transport, tb)
        self.composer.close(target)
        proof = self.composer.prove_termination(target)
        self.composer.acknowledge_cleanup(target, proof)
        self.assertEqual(ta.close_count, 1)
        self.assertFalse(tb.closed)

    def test_03_positive_capture_provenance_is_immutable_rgb24_in_ram(self):
        import numpy as np  # Already used by the real SessionRegistry.
        h, _ = self.owned()
        rgb = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
        original_bytes = bytes(rgb.tobytes(order="C"))
        original_digest = hashlib.sha256(original_bytes).hexdigest()  # independent oracle
        frame = self.composer.capture(h, 7, rgb, epoch=0)
        self.assertIsInstance(frame, self.FrameRef)
        self.assertEqual((frame.height, frame.width, frame.frame_seq), (4, 5, 7))
        self.assertEqual(frame.digest, original_digest)
        rgb[:, :, :] = 0
        self.assertEqual(frame.rgb.tobytes(order="C"), original_bytes)
        self.assertFalse(frame.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            frame.rgb.flags.writeable = True
        obs = self.composer.observe(h, frame, state={"found": True}, dpr_ppm=1000000)
        self.assertIsInstance(obs, self.Observation)
        self.assertEqual(obs.handle, h)
        self.assertEqual(obs.frame_seq, 7)
        self.assertEqual(obs.frame.digest, original_digest)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.capture(h, 8, rgb, epoch=1)
        self.assertEqual(self.composer.advance_epoch(h, validated=True), 1)
        with self.assertRaises(self.FrameValidationError):
            self.composer.observe(h, frame, state={}, dpr_ppm=1000000)

    def test_10_refuses_forged_termination_evidence_and_wrong_generation(self):
        h, t = self.owned()
        self.composer.stop(h)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.acknowledge_cleanup(h, self.TerminationProof(h, object()))
        self.assertTrue(self.registry.tombstone(h)[2])
        self.composer.close(h)
        proof = self.composer.prove_termination(h)
        forged = self.TerminationProof(h, proof._seal)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.acknowledge_cleanup(h, forged)
        wrong = self.Handle(h.kind, h.canonical_id, h.generation + 1)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.acknowledge_cleanup(wrong, proof)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertEqual(t.close_count, 1)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])

    def test_11_refuses_expired_inflight_without_real_ack(self):
        h, t = self.owned()
        lease = self.composer.acquire_actor(h, "agent", expires_ns=200)
        self.assertEqual(self.composer.publish(self.composer.issue(h, lease, "act", {}))["ok"], True)
        self.now[0] = 300
        self.assertFalse(self.locks.owns(lease, now_ns=300))
        with self.assertRaises(self.TargetLockRefused):
            self.locks.release(lease)
        self.composer.stop(h)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertEqual(t.close_count, 0)
        self.composer.close(h)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.acknowledge_cleanup(h, None)
        self.assertTrue(self.registry.tombstone(h)[2])
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])
        self.assertEqual(t.close_count, 1)

    def test_12_refuses_duplicate_close_and_does_not_teardown_twice(self):
        h, t = self.owned()
        self.composer.stop(h)
        c1 = self.composer.close(h)
        c2 = self.composer.close(h)
        self.assertIs(c1, c2)
        self.assertEqual(t.close_count, 1)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.stop(h)
        self.assertEqual(t.close_count, 1)
        self.assertTrue(self.registry.tombstone(h)[2])

    def test_13_refuses_send_and_capture_after_stop_with_no_effects(self):
        import numpy as np
        h, t = self.owned()
        lease = self.composer.acquire_actor(h, "agent", expires_ns=900)
        self.composer.stop(h)
        before = tuple(t.sends)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.issue(h, lease, "forbidden", {})
        with self.assertRaises(self.LifecycleRefused):
            self.composer.capture(h, 20, np.zeros((2, 2, 3), dtype=np.uint8), epoch=0)
        self.assertEqual(tuple(t.sends), before)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertEqual(self.composer.snapshot(h)["pending_publications"], 0)

    def test_14_refuses_delayed_old_reply_after_rebind_with_event_barrier(self):
        h, t = self.owned()
        lease = self.composer.acquire_actor(h, "agent", expires_ns=600)
        ticket = self.composer.issue(h, lease, "Runtime.evaluate", {"expression": "8"})
        ready, proceed = threading.Event(), threading.Event()
        outcomes = []
        published = []

        def worker():
            ready.set()
            if not proceed.wait(2):
                outcomes.append("barrier-timeout")
                return
            try:
                self.composer.publish(ticket, lambda x: published.append(x))
            except self.LifecycleRefused:
                outcomes.append("refused")
            else:
                outcomes.append("published")

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(ready.wait(2))
        self.composer.stop(h)
        self.composer.close(h)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        replacement = self.composer.register("cdp_tab", "alpha", MemoryTransport("alpha"), owned=True)
        self.assertGreater(replacement.generation, h.generation)
        proceed.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ["refused"])
        self.assertEqual(published, [])
        self.assertEqual([x[0] for x in t.sends], ["Runtime.evaluate"])
        self.assertEqual(t.close_count, 1)

    def test_15_rejects_undocumented_broker_successor_lane_producer_defect(self):
        h, old = self.owned()
        self.stop_and_join(h)
        new = MemoryTransport("alpha")
        h2 = self.composer.register("cdp_tab", "alpha", new, owned=True)
        lease = self.composer.acquire_actor(h2, "new", expires_ns=500)
        # The unchanged broker retains canonical STOP even after registering a
        # newer generation. This MUST be reported as a PRODUCTION_DEFECT,
        # rather than silently labeling the rebind operational.
        with self.assertRaises(self.ProductionDefect):
            self.composer.issue(h2, lease, "Runtime.evaluate", {"expression": "2"})
        self.assertEqual(new.sends, [])
        self.assertFalse(new.closed)
        self.assertEqual(old.close_count, 1)

    def test_declared_successor_references_closer_receipt_and_can_send(self):
        h, old = self.owned()
        self.composer.stop(h)
        receipt = self.composer.close(h)
        self.assertTrue(receipt.receipt_id)
        self.assertIs(self.composer.close(h), receipt)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        new = MemoryTransport("alpha")
        h2 = self.composer.register("cdp_tab", "alpha", new, owned=True,
                                    successor_handoff=receipt)
        self.assertGreater(h2.generation, h.generation)
        lease = self.composer.acquire_actor(h2, "successor", expires_ns=500)
        self.composer.issue(h2, lease, "Runtime.evaluate", {"expression": "2"})
        self.assertEqual([entry[0] for entry in new.sends], ["Runtime.evaluate"])
        self.assertEqual(old.close_count, 1)
        with self.assertRaises(self.ProductionDefect):
            self.composer.register("cdp_tab", "other", MemoryTransport("other"),
                                    owned=True, successor_handoff=receipt)

    def test_successor_handoff_rejects_forged_receipt_before_registration(self):
        from dataclasses import replace
        h, old = self.owned()
        self.composer.stop(h)
        receipt = self.composer.close(h)
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        for forged in (replace(receipt, receipt_id="forged"),
                       replace(receipt, generation=receipt.generation + 1),
                       replace(receipt)):
            new = MemoryTransport("alpha")
            with self.assertRaises(self.ProductionDefect):
                self.composer.register("cdp_tab", "alpha", new, owned=True,
                                        successor_handoff=forged)
            self.assertEqual(new.sends, [])
            self.assertFalse(new.closed)
        from wender.perceive.harvest_broker import StoppedError
        for declaration in ((h, h.generation, "forged"),
                            (h, h.generation + 1, receipt.receipt_id)):
            with self.assertRaises(StoppedError):
                self.broker.register("cdp_tab", "alpha", MemoryTransport("alpha"),
                                     successor_handoff=declaration)
        self.assertEqual(old.close_count, 1)

    def test_16_refuses_lease_forgery_while_broker_send_count_stays_zero(self):
        h, t = self.owned()
        lease = self.composer.acquire_actor(h, "legitimate", expires_ns=400)
        from dataclasses import replace
        forged = replace(lease, owner="attacker")
        with self.assertRaises(self.LifecycleRefused):
            self.composer.issue(h, forged, "forged", {})
        self.assertEqual(t.sends, [])
        self.assertTrue(self.locks.owns(lease, now_ns=100))
        self.composer.stop(h)
        self.assertEqual(t.sends, [])
        self.assertTrue(self.registry.tombstone(h)[2])

    def test_17_refuses_false_external_termination_even_after_close(self):
        h, transport = self.owned()
        self.composer.stop(h)
        self.composer.close(h)
        real = self.channel

        class RefusingChannel:
            def __init__(self, delegate):
                self.delegate = delegate
            def joined_target(self, handle, trans):
                return False

        self.composer.channel = RefusingChannel(real)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.prove_termination(h)
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertEqual(transport.close_count, 1)
        self.composer.channel = real
        proof = self.composer.prove_termination(h)
        self.composer.acknowledge_cleanup(h, proof)
        self.assertFalse(self.registry.tombstone(h)[2])


    def test_18_refuses_tampered_issued_reply_without_publication(self):
        h, transport = self.owned()
        lease = self.composer.acquire_actor(h, "actor", expires_ns=500)
        reply = self.composer.issue(h, lease, "Runtime.evaluate", {"expression": "4"})
        published = []
        # Frozen dataclasses are not a security boundary against object
        # mutation; the actual issuance must have a separate saved identity.
        object.__setattr__(reply, "sequence", reply.sequence + 1)
        with self.assertRaises(self.LifecycleRefused):
            self.composer.publish(reply, lambda payload: published.append(payload))
        self.assertEqual(published, [])
        self.assertEqual(self.composer.snapshot(h)["pending_publications"], 0)
        self.assertEqual([x[0] for x in transport.sends], ["Runtime.evaluate"])


if __name__ == "__main__":
    unittest.main()
