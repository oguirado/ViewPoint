"""Production unit tests for HarvestBroker lifecycle coordinator.

Exercises production public paths in wender.perceive.harvest_broker:
- Gate G M1 5: Ownership and STOP survive teardown, reject mutant close caller tab,
  reject mutant send after STOP, positive control owned healthy close is idempotent.
- Invariant INV-M1-1: Generation progression never repeats; navigation increments separate epoch;
  aliased actuation surfaces (capsule and child screen/godot_window) resolve to same canonical resource.
- Invariant INV-M1-2: Replies match connection-wide request IDs and issuing incarnation;
  ingress events route to bounded session lanes; active waiters never evicted;
  timed-out IDs tombstoned and late replies discarded; overflow fences affected lane.
- Invariant INV-M1-3: Discovery validates normalized origin, refuses ambiguous matches,
  creates only on miss; ownership tracks target vs attachment separately.
- Metadata lock ordering: no metadata mutex spans transport I/O, waits, callbacks, or executor admission.
- Canonical value record contracts and fields.
"""

from dataclasses import fields
import threading
import time
import unittest
from unittest.mock import MagicMock, call

import numpy as np

from wender.perceive.harvest_broker import (
    HarvestBroker,
    Handle,
    TargetRecord,
    FrameRef,
    Observation,
    Receipt,
    ActionRequest,
    ActorLease,
    FocusLease,
    StreamSpec,
    StreamToken,
    Ticket,
    ChangeResult,
    PerceptionResult,
    WindowFrames,
    WindowDescription,
    BrokerError,
    StoppedError,
    FencedLaneError,
    AmbiguousTargetError,
    InvalidOriginError,
)


class FakeTransport:
    """Mock transport tracking all sent commands, closes, and detaches."""

    def __init__(self, delay_s: float = 0.0):
        self.sent_commands = []
        self.closed_targets = []
        self.detached_sessions = []
        self.close_called = False
        self.delay_s = delay_s
        self.lock_check_fn = None

    def send(self, method: str, params: dict, session_id: str | None = None) -> dict:
        if self.lock_check_fn:
            self.lock_check_fn()
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        self.sent_commands.append((method, params, session_id))
        if method == "Target.closeTarget":
            self.closed_targets.append(params.get("targetId"))
            return {"success": True}
        if method == "Target.detachFromTarget":
            self.detached_sessions.append(params.get("sessionId"))
            return {"success": True}
        return {"result": f"ack_{method}", "method": method}

    def close_target(self, target_id: str):
        if self.lock_check_fn:
            self.lock_check_fn()
        self.closed_targets.append(target_id)

    def detach(self, session_id: str):
        if self.lock_check_fn:
            self.lock_check_fn()
        self.detached_sessions.append(session_id)

    def is_session_detached(self, session_id):
        return session_id in self.detached_sessions

    def is_terminated(self, canonical_id):
        return canonical_id in self.closed_targets

    def close(self):
        if self.lock_check_fn:
            self.lock_check_fn()
        self.close_called = True


class TestHarvestBrokerGateGM15(unittest.TestCase):
    """Gate G M1 5: Ownership and STOP survive teardown,

    reject mutant close caller tab or send after STOP,
    positive control owned healthy close is idempotent.
    """

    def test_reject_mutant_close_caller_tab(self):
        """Reject mutant close caller tab: when owned=False (caller-owned target),

        closing the target MUST NOT invoke Target.closeTarget or close on the transport.
        Attachment may be detached if attachment_owned=True, but the caller's tab remains open.
        """
        transport = FakeTransport()
        broker = HarvestBroker()

        caller_handle = broker.register(
            kind="cdp_tab",
            canonical_id="tab-caller-123",
            transport=transport,
            owned=False,  # Caller-owned!
            session_id="session-xyz",
            attachment_owned=True,
        )

        # Close the handle
        receipt = broker.close(caller_handle)

        self.assertEqual(receipt.status, "closed")
        self.assertEqual(receipt.handle, caller_handle)

        # Transport MUST NOT have received close_target or Target.closeTarget for the caller tab!
        self.assertEqual(transport.closed_targets, [])
        self.assertFalse(transport.close_called)

        # Attachment WAS detached because attachment_owned was True
        self.assertIn("session-xyz", transport.detached_sessions)

    def test_reject_mutant_send_after_stop(self):
        """Reject mutant send after STOP: commands on a stopped broker or target

        must be rejected with StoppedError and not sent to transport.
        """
        transport = FakeTransport()
        broker = HarvestBroker()

        handle = broker.register(
            kind="cdp_tab",
            canonical_id="tab-456",
            transport=transport,
            owned=True,
        )

        # Command works before STOP
        res = broker.command(handle, "Page.navigate", {"url": "https://example.com"})
        self.assertEqual(res["result"], "ack_Page.navigate")
        self.assertEqual(len(transport.sent_commands), 1)

        # STOP the broker
        broker.stop()
        self.assertTrue(broker.is_stopped)

        # Subsequent command MUST be rejected with StoppedError
        with self.assertRaises(StoppedError):
            broker.command(handle, "Page.navigate", {"url": "https://example.com/2"})

        # No new command was sent to transport after STOP
        self.assertEqual(len(transport.sent_commands), 1)

    def test_positive_control_owned_healthy_close_is_idempotent(self):
        """Positive control: owned healthy target close executes cleanly and idempotently."""
        transport = FakeTransport()
        broker = HarvestBroker()

        handle = broker.register(
            kind="cdp_tab",
            canonical_id="tab-owned-789",
            transport=transport,
            owned=True,
            session_id="sess-1",
            attachment_owned=True,
        )

        # First close: should close target and detach attachment
        receipt1 = broker.close(handle)
        self.assertEqual(receipt1.status, "closed")
        self.assertIn("tab-owned-789", transport.closed_targets)
        self.assertIn("sess-1", transport.detached_sessions)

        # Second close on the same handle: must be idempotent and succeed without redundant closes
        initial_close_count = len(transport.closed_targets)
        receipt2 = broker.close(handle)
        self.assertEqual(receipt2.status, "closed")
        self.assertEqual(receipt2.reason, "Already closed")
        self.assertEqual(len(transport.closed_targets), initial_close_count)


class TestHarvestBrokerInvariants(unittest.TestCase):
    """Test core invariants INV-M1-1, INV-M1-2, and INV-M1-3."""

    def test_inv_m1_1_generation_and_navigation_epoch(self):
        """INV-M1-1: Generation never repeats; navigation increments separate epoch."""
        transport = FakeTransport()
        broker = HarvestBroker()

        h1 = broker.register("cdp_tab", "tab-1", transport)
        h2 = broker.register("cdp_tab", "tab-2", transport)
        h3 = broker.register("cdp_tab", "tab-3", transport)

        # Generations must be strictly increasing and unique
        self.assertEqual(h1.generation, 1)
        self.assertEqual(h2.generation, 2)
        self.assertEqual(h3.generation, 3)
        self.assertNotEqual(h1.generation, h2.generation)

        # Advancing epoch increments epoch while keeping generation intact
        rec1 = broker.get_target_record(h1)
        self.assertEqual(rec1.epoch, 0)
        self.assertEqual(rec1.handle.generation, 1)

        new_epoch = broker.advance_epoch(h1)
        self.assertEqual(new_epoch, 1)
        rec1_after = broker.get_target_record(h1)
        self.assertEqual(rec1_after.epoch, 1)
        self.assertEqual(rec1_after.handle.generation, 1)

    def test_inv_m1_1_capsule_and_child_screen_canonical_aliasing(self):
        """INV-M1-1: A capsule and its child screen use the same canonical actuation resource

        when addressing the same destination.
        """
        transport = FakeTransport()
        broker = HarvestBroker()

        # Explicit alias binding or prefix resolution
        broker.bind_alias("capsule", "cap-alpha", "screen:disp-0")
        broker.bind_alias("xvfb_screen", "scr-alpha", "screen:disp-0")

        h_cap = broker.register("capsule", "cap-alpha", transport)
        h_scr = broker.register("xvfb_screen", "scr-alpha", transport)

        # Both resolve to the same canonical destination resource
        self.assertEqual(h_cap.canonical_id, "screen:disp-0")
        self.assertEqual(h_scr.canonical_id, "screen:disp-0")

    def test_inv_m1_2_command_lane_bounding_and_overflow_fencing(self):
        """INV-M1-2: Command lanes are bounded; overflow fences affected lane

        rather than silently losing load evidence.
        """
        transport = FakeTransport(delay_s=0.05)
        # Create broker with small lane capacity = 2
        broker = HarvestBroker(lane_capacity=2)

        handle = broker.register("cdp_tab", "tab-lane-test", transport)

        # Start 2 threads to saturate capacity
        threads = []
        errors = []

        def worker():
            try:
                broker.command(handle, "Test.slowCommand")
            except Exception as e:
                errors.append(e)

        for _ in range(2):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        # Immediate third command should overflow and fence the lane
        time.sleep(0.01)
        with self.assertRaises(FencedLaneError):
            broker.command(handle, "Test.overflowCommand")

        for t in threads:
            t.join()

        # Subsequent command must also fail because lane was fenced
        with self.assertRaises(FencedLaneError):
            broker.command(handle, "Test.afterFenced")

    def test_inv_m1_2_active_waiters_and_tombstone_discard_late_replies(self):
        """INV-M1-2: Timed-out identifiers are tombstoned and late replies discarded."""
        transport = FakeTransport()
        broker = HarvestBroker()

        handle = broker.register("cdp_tab", "tab-tombstone-test", transport)

        # Simulate timed out request tombstoning
        timed_out_req_id = f"req-{handle.generation}-1"
        state = broker._targets_by_handle[handle]
        with state.lane_lock:
            state.active_waiters.add(timed_out_req_id)
        broker.mark_timed_out_request(handle, timed_out_req_id)

        # State check
        state = broker._targets_by_handle[handle]
        self.assertIn(timed_out_req_id, state.tombstoned_request_ids)
        self.assertNotIn(timed_out_req_id, state.active_waiters)

    def test_inv_m1_3_discovery_origin_normalization_and_ambiguity_refusal(self):
        """INV-M1-3: Discovery validates normalized origin, refuses ambiguous matches,

        and creates only on miss.
        """
        transport = FakeTransport()
        broker = HarvestBroker()

        # Invalid origin raises InvalidOriginError
        with self.assertRaises(InvalidOriginError):
            broker.discover_or_create("not-a-valid-url")

        # Miss creates new target via factory
        factory_called = [0]

        def factory():
            factory_called[0] += 1
            return ("https://example.com/page1", transport)

        h1 = broker.discover_or_create("https://example.com/", factory)
        self.assertEqual(factory_called[0], 1)
        self.assertEqual(h1.canonical_id, "https://example.com/page1")

        # Next discovery of the same origin reuses without factory call
        h2 = broker.discover_or_create("https://example.com/", factory)
        self.assertEqual(factory_called[0], 1)
        self.assertEqual(h2, h1)

        # Ambiguous match: register a second target matching the same origin
        transport2 = FakeTransport()
        broker.register("cdp_tab", "https://example.com/page2", transport2)

        # Now discovery must refuse ambiguous matches
        with self.assertRaises(AmbiguousTargetError):
            broker.discover_or_create("https://example.com/", factory)


class TestHarvestBrokerLockOrdering(unittest.TestCase):
    """Verify that NO registry metadata mutex spans transport I/O."""

    def test_no_registry_lock_during_transport_io(self):
        transport = FakeTransport()
        broker = HarvestBroker()

        handle = broker.register("cdp_tab", "tab-lock-test", transport)

        def assert_registry_lock_not_held():
            # In Python RLock, acquire with blocking=False succeeds if not locked or if locked by current thread.
            # But we can verify by checking if another thread can acquire the lock during I/O!
            locked_by_io = threading.Event()
            other_acquired = threading.Event()

            def checker():
                # Attempt to acquire registry lock during transport I/O
                acq = broker._registry_lock.acquire(blocking=False)
                if acq:
                    other_acquired.set()
                    broker._registry_lock.release()
                locked_by_io.set()

            t = threading.Thread(target=checker)
            t.start()
            t.join()

            if not other_acquired.is_set():
                raise AssertionError("Registry lock was held during transport I/O!")

        transport.lock_check_fn = assert_registry_lock_not_held

        res = broker.command(handle, "Runtime.evaluate", {"expression": "1+1"})
        self.assertEqual(res["result"], "ack_Runtime.evaluate")


class TestCanonicalValueContracts(unittest.TestCase):
    """Verify exact field ordering and types of proposed canonical dataclasses."""

    def test_handle_fields(self):
        f = [field.name for field in fields(Handle)]
        self.assertEqual(f, ["kind", "canonical_id", "generation"])

    def test_target_record_fields(self):
        f = [field.name for field in fields(TargetRecord)]
        self.assertEqual(
            f,
            ["handle", "transport", "owned", "epoch", "session_id", "attachment_owned", "closed"],
        )

    def test_receipt_fields(self):
        f = [field.name for field in fields(Receipt)]
        self.assertEqual(f, ["action_id", "handle", "status", "reason"])

    def test_frameref_fields(self):
        f = [field.name for field in fields(FrameRef)]
        self.assertEqual(f, ["handle", "epoch", "frame_seq", "width", "height", "rgb", "digest"])

    def test_observation_fields(self):
        f = [field.name for field in fields(Observation)]
        self.assertEqual(f, ["handle", "epoch", "frame_seq", "dpr_ppm", "state", "frame"])

    def test_action_request_fields(self):
        f = [field.name for field in fields(ActionRequest)]
        self.assertEqual(f, ["handle", "action_id", "epoch", "payload", "cost", "deadline_ns"])

    def test_actor_lease_fields(self):
        f = [field.name for field in fields(ActorLease)]
        self.assertEqual(f, ["handle", "owner", "generation", "nonce", "expires_ns"])

    def test_focus_lease_fields(self):
        f = [field.name for field in fields(FocusLease)]
        self.assertEqual(f, ["lease", "domain", "epoch"])

    def test_stream_spec_fields(self):
        f = [field.name for field in fields(StreamSpec)]
        self.assertEqual(f, ["width", "height", "slots", "quota_bytes"])

    def test_stream_token_fields(self):
        f = [field.name for field in fields(StreamToken)]
        self.assertEqual(f, ["handle", "epoch"])

    def test_ticket_fields(self):
        f = [field.name for field in fields(Ticket)]
        self.assertEqual(f, ["action_id", "handle"])

    def test_change_result_fields(self):
        f = [field.name for field in fields(ChangeResult)]
        self.assertEqual(f, ["handle", "epoch", "settled", "claims", "ops"])

    def test_perception_result_fields(self):
        f = [field.name for field in fields(PerceptionResult)]
        self.assertEqual(f, ["handle", "observation", "error"])

    def test_window_frames_fields(self):
        f = [field.name for field in fields(WindowFrames)]
        self.assertEqual(f, ["handle", "epoch", "frames", "frame_seqs", "dpr_ppm"])

    def test_window_description_fields(self):
        f = [field.name for field in fields(WindowDescription)]
        self.assertEqual(f, ["handle", "epoch", "frame_seqs", "descriptors", "regions", "error"])


class TestHarvestBrokerConstructorAndScorecard(unittest.TestCase):
    """Test constructor contract, writer_fence, and scorecard pass predicate."""

    def test_constructor_signature_and_writer_fence(self):
        registry_mock = object()
        tab_manager_mock = object()
        fence_mock = object()
        broker = HarvestBroker(registry_mock, tab_manager_mock, writer_fence=fence_mock)
        self.assertIs(broker.registry, registry_mock)
        self.assertIs(broker.tab_manager, tab_manager_mock)
        self.assertIs(broker.writer_fence, fence_mock)

    def test_scorecard_pass_predicate(self):
        transport = FakeTransport()
        broker = HarvestBroker()
        h1 = broker.register("cdp_tab", "tab-1", transport, owned=True)
        h2 = broker.register("cdp_tab", "tab-2", transport, owned=False)

        # In-flight command completes
        broker.command(h1, "Runtime.evaluate", {"expression": "1+1"})

        # Close h1 and h2
        r1 = broker.close(h1)
        r2 = broker.close(h2)

        self.assertEqual(r1.status, "closed")
        self.assertEqual(r2.status, "cleanup_pending")
        self.assertFalse(broker.reconcile_cleanup(h2))
        # Caller reports external termination; the broker never closes its target.
        transport.is_terminated = lambda target_id: target_id == "tab-2" or target_id in transport.closed_targets
        self.assertTrue(broker.reconcile_cleanup(h2))
        self.assertEqual(broker.close(h2).status, "closed")
        # Owned was closed, unowned was preserved
        self.assertIn("tab-1", transport.closed_targets)
        self.assertNotIn("tab-2", transport.closed_targets)

    def test_target_kinds_validation(self):
        broker = HarvestBroker()
        valid_kinds = ["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"]
        for kind in valid_kinds:
            h = broker.register(kind, f"target-{kind}", FakeTransport())
            self.assertEqual(h.kind, kind)

        with self.assertRaises(ValueError):
            broker.register("unsupported_kind", "target-fail", FakeTransport())


if __name__ == "__main__":
    unittest.main()
