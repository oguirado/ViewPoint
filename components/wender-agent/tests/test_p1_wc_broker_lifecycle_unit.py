"""Deterministic offline C-port/C-lane/STOP contract tests (no external transport)."""
import hashlib
import threading
import unittest
from dataclasses import fields

import numpy as np


class FakeTransport:
    """Only emulates external I/O; identity and state use the real registry."""

    def __init__(self, block=False):
        self.lock = threading.Lock()
        self.sends = []
        self.closes = []
        self.detaches = []
        self.terminated = set()
        self.detached = set()
        self.entered = threading.Barrier(2, timeout=5) if block else None
        self.release = threading.Barrier(2, timeout=5) if block else None
        self.check_io = None

    def send(self, method, params, session_id=None):
        if self.check_io:
            self.check_io()
        with self.lock:
            self.sends.append((method, dict(params), session_id))
        if self.entered is not None:
            self.entered.wait()
            self.release.wait()
        return {"ack": method, "session": session_id}

    def close_target(self, target_id):
        if self.check_io:
            self.check_io()
        with self.lock:
            self.closes.append(target_id)
            self.terminated.add(target_id)

    def detach_session(self, session_id):
        if self.check_io:
            self.check_io()
        with self.lock:
            self.detaches.append(session_id)
            self.detached.add(session_id)

    def is_terminated(self, target_id):
        with self.lock:
            return target_id in self.terminated

    def is_session_detached(self, session_id):
        with self.lock:
            return session_id in self.detached


class BrokerLifecycleTests(unittest.TestCase):
    def setUp(self):
        # No producer mocks or masked imports; import lazily in undecorated setup.
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.harvest_broker import HarvestBroker
        self.registry = SessionRegistry()
        self.broker = HarvestBroker(self.registry, lane_capacity=1)

    def spawn(self, function):
        results, errors = [], []

        def run():
            try:
                results.append(function())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results, errors

    def joined(self, thread, errors):
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "worker was not joined")
        if errors:
            raise errors[0]

    def test_positive_two_real_registry_targets_and_sibling_survival(self):
        from wender.perceive.harvest_broker import StoppedError
        from wender.perceive.session_registry import Handle
        a, b = FakeTransport(), FakeTransport()
        ha = self.broker.register("cdp_tab", "target-a", a, owned=True)
        hb = self.broker.register("cdp_tab", "target-b", b, owned=True)
        self.assertIsInstance(ha, Handle)
        self.assertNotEqual(ha, hb)
        self.assertEqual(self.registry.resolve(ha).epoch, 0)
        self.assertEqual(self.broker.get_target_record(hb).epoch, 0)
        ra = self.broker.command(ha, "Page.enable")
        rb = self.broker.command(hb, "Runtime.evaluate", {"expression": "1+1"})
        self.assertEqual((ra["ack"], rb["ack"]), ("Page.enable", "Runtime.evaluate"))
        self.assertNotEqual(ra["broker_request_id"], rb["broker_request_id"])
        self.assertEqual([x[0] for x in a.sends], ["Page.enable"])
        self.assertEqual([x[0] for x in b.sends], ["Runtime.evaluate"])
        self.assertEqual(self.broker._targets_by_handle[ha].inflight_count, 0)
        self.broker.stop(ha)
        with self.assertRaises(StoppedError):
            self.broker.command(ha, "Page.reload")
        self.assertEqual(self.broker.command(hb, "Page.reload")["ack"], "Page.reload")
        self.assertEqual(len(a.sends), 1)
        self.assertEqual(len(b.sends), 2)
        self.assertTrue(self.registry.tombstone(ha)[2])

    def test_structural_records_attachment_and_generation(self):
        from wender.perceive.session_registry import Handle, TargetRecord, RegistryError
        self.assertEqual([field.name for field in fields(Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual([field.name for field in fields(TargetRecord)],
                         ["handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"])
        with self.assertRaises(RegistryError):
            Handle("cdp_tab", "x", 0)
        transport = FakeTransport()
        handle = self.broker.register("cdp_tab", "owned-session", transport,
                                      owned=False, session_id="session-1", attachment_owned=True)
        record = self.registry.resolve(handle)
        self.assertEqual((record.session_id, record.attachment_owned, record.epoch),
                         ("session-1", True, 0))
        self.assertIs(record.transport, transport)
        self.assertEqual(self.broker.register("cdp_tab", "owned-session", transport,
                                              owned=False, session_id="session-1", attachment_owned=True), handle)
        without_session = self.broker.register("cdp_tab", "without-session", FakeTransport())
        self.assertFalse(self.registry.resolve(without_session).attachment_owned)

    def test_strict_registry_registration_failures_visible_and_no_ghost_handle(self):
        from wender.perceive.harvest_broker import HarvestBroker
        from wender.perceive.session_registry import SessionRegistry, RegistryError

        class RefusingRegistry(SessionRegistry):
            def register(self, kind, canonical_id, transport, *, owned):
                raise RegistryError("simulated producer refusal")

        registry = RefusingRegistry()
        broker = HarvestBroker(registry)
        with self.assertRaisesRegex(RegistryError, "simulated producer refusal"):
            broker.register("cdp_tab", "refused", FakeTransport())
        self.assertEqual(broker._targets_by_handle, {})
        self.assertEqual(broker._action_counter, 1)
        with self.assertRaises(RegistryError):
            broker.register("cdp_tab", "bad-owned", FakeTransport(), owned=1)

    def test_capacity_contention_exactly_one_send_and_no_double_release(self):
        from wender.perceive.harvest_broker import FencedLaneError
        t = FakeTransport(block=True)
        h = self.broker.register("cdp_tab", "capacity", t)
        # A second worker tries admission while the first occupies the sole slot.
        first, first_results, first_errors = self.spawn(lambda: self.broker.command(h, "Test.send"))
        t.entered.wait()
        second, second_results, second_errors = self.spawn(lambda: self.broker.command(h, "Test.send"))
        second.join(timeout=5)
        self.assertFalse(second.is_alive())
        self.assertEqual(second_results, [])
        self.assertEqual(len(second_errors), 1)
        self.assertIsInstance(second_errors[0], FencedLaneError)
        t.release.wait()
        self.joined(first, first_errors)
        self.assertEqual(len(first_results), 1)
        self.assertEqual(first_results[0].get("status"), "discarded_late_reply")
        self.assertEqual(len(t.sends), 1)
        state = self.broker._targets_by_handle[h]
        self.assertEqual(state.inflight_count, 0)
        self.assertEqual(len(state.active_waiters), 0)
        self.assertEqual(state.request_incarnations, {})
        self.assertEqual(self.broker._action_counter, 2)
        with self.assertRaises(FencedLaneError):
            self.broker.command(h, "Test.after-overflow")
        self.assertEqual(len(t.sends), 1)

    def test_stop_after_preparation_before_send_fences_without_sequence_or_history(self):
        from wender.perceive.harvest_broker import StoppedError
        t = FakeTransport()
        h = self.broker.register("cdp_tab", "prepare", t)
        prepared, release = threading.Barrier(2, timeout=5), threading.Barrier(2, timeout=5)

        def hook(handle, method):
            self.assertEqual(handle, h)
            prepared.wait()
            release.wait()

        self.broker._before_transport_send = hook
        state = self.broker._targets_by_handle[h]
        before = (self.broker._action_counter, len(state.ingress_events), len(state.active_waiters))
        thread, results, errors = self.spawn(lambda: self.broker.command(h, "Test.must-not-send"))
        prepared.wait()
        self.assertEqual(state.inflight_count, 1)
        self.broker.stop(h)
        release.wait()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StoppedError)
        self.assertEqual((self.broker._action_counter, len(state.ingress_events),
                          len(state.active_waiters)), before)
        self.assertEqual(state.inflight_count, 0)
        self.assertEqual(state.request_incarnations, {})
        self.assertEqual(t.sends, [])
        self.assertTrue(self.registry.tombstone(h)[2])
        self.assertFalse(self.broker.reconcile_cleanup(h))
        self.assertEqual(self.broker.deferred_cleanup_count, 1)

    def test_epoch_transition_drops_old_inflight_reply_and_new_epoch_survives(self):
        t = FakeTransport(block=True)
        h = self.broker.register("cdp_tab", "epoch", t)
        thread, results, errors = self.spawn(lambda: self.broker.command(h, "Page.old"))
        t.entered.wait()
        self.assertEqual(self.broker.advance_epoch(h), 1)
        self.assertEqual(self.registry.resolve(h).epoch, 1)
        t.release.wait()
        self.joined(thread, errors)
        self.assertEqual(results[0].get("status"), "discarded_epoch_mismatch")
        state = self.broker._targets_by_handle[h]
        self.assertEqual((state.inflight_count, len(state.active_waiters)), (0, 0))
        self.assertEqual(state.pending_replies, {})
        t.entered = None
        t.release = None
        self.assertEqual(self.broker.command(h, "Page.new")["ack"], "Page.new")
        self.assertEqual([x[0] for x in t.sends], ["Page.old", "Page.new"])

    def test_close_drains_reply_then_requires_observed_termination_before_rebind(self):
        from wender.perceive.harvest_broker import StoppedError
        old = FakeTransport(block=True)
        h = self.broker.register("cdp_tab", "rebind", old)
        thread, results, errors = self.spawn(lambda: self.broker.command(h, "Page.inflight"))
        old.entered.wait()
        receipt = self.broker.close(h)
        self.assertEqual(receipt.status, "cleanup_pending")
        self.assertTrue(self.registry.tombstone(h)[2])
        with self.assertRaises(StoppedError):
            self.broker.command(h, "Page.after-close")
        with self.assertRaises(StoppedError):
            self.broker.register("cdp_tab", "rebind", FakeTransport())
        self.assertEqual(old.closes, [])
        old.release.wait()
        self.joined(thread, errors)
        self.assertEqual(results[0].get("status"), "discarded_late_reply")
        self.assertEqual(old.closes, ["rebind"])
        self.assertFalse(self.registry.tombstone(h)[2])
        self.assertEqual(self.broker.deferred_cleanup_count, 0)
        new = FakeTransport()
        h2 = self.broker.register("cdp_tab", "rebind", new)
        self.assertGreater(h2.generation, h.generation)
        self.assertEqual(self.broker.command(h2, "Page.new")["ack"], "Page.new")
        self.assertEqual(len(new.sends), 1)
        self.assertEqual(len(old.sends), 1)
        self.assertEqual(self.broker._targets_by_handle[h].inflight_count, 0)
        self.assertEqual(self.broker._targets_by_handle[h2].inflight_count, 0)

    def test_owned_attachment_on_caller_tab_detaches_without_closing(self):
        caller = FakeTransport()
        h = self.broker.register("cdp_tab", "caller", caller, owned=False,
                                  session_id="sess", attachment_owned=True)
        self.assertEqual(self.broker.close(h).status, "closed")
        self.assertEqual(caller.detaches, ["sess"])
        self.assertEqual(caller.closes, [])
        self.assertEqual(caller.sends, [])
        self.assertFalse(self.registry.tombstone(h)[2])
        self.assertEqual(self.broker.close(h).status, "closed")
        self.assertEqual(caller.detaches, ["sess"])

    def test_caller_owned_attachment_not_detached_and_quarantine_preserved(self):
        caller = FakeTransport()
        h = self.broker.register("cdp_tab", "external", caller, owned=False,
                                  session_id="external-sess", attachment_owned=False)
        self.assertEqual(self.broker.close(h).status, "cleanup_pending")
        self.assertEqual(caller.detaches, [])
        self.assertEqual(caller.closes, [])
        self.assertTrue(self.registry.tombstone(h)[2])
        caller.terminated.add("external")
        self.assertTrue(self.broker.reconcile_cleanup(h))
        self.assertFalse(self.registry.tombstone(h)[2])

    def test_global_stop_persists_and_rejects_registration(self):
        from wender.perceive.harvest_broker import StoppedError
        t = FakeTransport()
        h = self.broker.register("cdp_tab", "all", t)
        state = self.broker._targets_by_handle[h]
        self.broker.stop()
        self.broker.stop()
        self.assertTrue(self.broker.is_stopped)
        self.assertEqual(self.broker.deferred_cleanup_count, 1)
        before = (self.broker._action_counter, state.inflight_count,
                  len(state.active_waiters), len(state.ingress_events))
        with self.assertRaises(StoppedError):
            self.broker.command(h, "Page.bad")
        with self.assertRaises(StoppedError):
            self.broker.register("cdp_tab", "new", FakeTransport())
        with self.assertRaises(StoppedError):
            self.broker.advance_epoch(h)
        self.assertEqual((self.broker._action_counter, state.inflight_count,
                          len(state.active_waiters), len(state.ingress_events)), before)
        self.assertEqual(t.sends, [])

    def test_ingress_overflow_fences_lane_without_new_publication(self):
        from wender.perceive.harvest_broker import FencedLaneError
        h = self.broker.register("cdp_tab", "ingress", FakeTransport())
        self.broker.route_ingress_event(h, {"seq": 1})
        state = self.broker._targets_by_handle[h]
        before = self.broker._action_counter
        with self.assertRaises(FencedLaneError):
            self.broker.route_ingress_event(h, {"seq": 2})
        self.assertEqual(state.ingress_events, [{"seq": 1}])
        with self.assertRaises(FencedLaneError):
            self.broker.command(h, "Page.bad")
        self.assertEqual(self.broker._action_counter, before)
        self.assertEqual(state.inflight_count, 0)

    def test_metadata_mutexes_free_during_external_sends_and_cleanup(self):
        t = FakeTransport()
        h = self.broker.register("cdp_tab", "locks", t)

        def check():
            acquired = []

            def prove():
                got = self.broker._registry_lock.acquire(blocking=False)
                acquired.append(got)
                if got:
                    self.broker._registry_lock.release()

            thread = threading.Thread(target=prove, daemon=True)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(acquired, [True])

        t.check_io = check
        self.assertEqual(self.broker.command(h, "Page.good")["ack"], "Page.good")
        self.assertEqual(self.broker.close(h).status, "closed")

    def test_real_rgb24_frame_provenance_and_immutable_ownership(self):
        from wender.perceive.session_registry import FrameRef, FrameValidationError
        t = FakeTransport()
        h = self.broker.register("x11_window", "rgb", t)
        # Independent byte/hash oracle; source pixels never written to disk.
        raw = bytes((3, 9, 27, 81, 4, 10, 28, 82, 5, 11, 29, 83))
        expected_digest = hashlib.sha256(raw).hexdigest()
        pixels = np.frombuffer(bytearray(raw), dtype=np.uint8).reshape(2, 2, 3)
        f = self.registry.admit_frame(h, 1, pixels, epoch=0)
        self.assertEqual(f.digest, expected_digest)
        self.assertEqual((f.handle, f.epoch, f.frame_seq, f.width, f.height),
                         (h, 0, 1, 2, 2))
        self.assertFalse(f.rgb.flags.writeable)
        pixels[0, 0, 0] = 255
        self.assertEqual(f.rgb.tobytes(), raw)
        o = self.registry.admit_observation(h, f, state={"screen": "unsettled"}, dpr_ppm=1_000_000)
        self.assertEqual((o.epoch, o.frame_seq, o.frame.digest), (0, 1, expected_digest))
        with self.assertRaises(FrameValidationError):
            FrameRef(h, 0, 1, 2, 2, pixels, expected_digest)
        self.broker.advance_epoch(h)
        with self.assertRaises(FrameValidationError):
            self.registry.admit_observation(h, f, state={}, dpr_ppm=1_000_000)

    def test_capacity_argument_refusals(self):
        from wender.perceive.harvest_broker import HarvestBroker
        for capacity in (0, -1, 1.0, True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                HarvestBroker(self.registry, lane_capacity=capacity)


if __name__ == "__main__":
    unittest.main()
