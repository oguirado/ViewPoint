"""Offline production-path contract: no browser, desktop, process, or frame files."""
import copy
import numpy as np
from wender.perceive.session_registry import FrameRef
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from wender.act.cdp_executor import ActionError, run_ax_action
from wender.act.focus_policy import (
    ActorLease, FocusClass, FocusPolicy, FocusRefused, Handle,
    Observation, classify,
)


class FakeClock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value


@dataclass(frozen=True)
class Record:
    handle: object
    epoch: int = 2
    session_id: str = "s1"
    closed: bool = False
    owned: bool = True


class FakeRegistry:
    def __init__(self, records):
        self.records = {r.handle.canonical_id: r for r in records}
        self.current = {}

    def get(self, handle):
        return self.records.get(handle.canonical_id)

    def validate_observation(self, observation):
        return self.current.get(observation.handle.canonical_id) is observation


class FakeLocks:
    def __init__(self, leases):
        self.issued = {v.nonce: v for v in leases}

    def owns(self, lease, *, now_ns):
        return self.issued.get(lease.nonce) is lease and now_ns < lease.expires_ns


class FakeFence:
    def __init__(self):
        self.busy = False
        self.acquisitions = 0
        self.releases = 0
        self.quarantines = 0

    def acquire(self, lease):
        if self.busy:
            return False
        self.busy = True
        self.acquisitions += 1
        return True

    def release(self, lease):
        if not self.busy:
            raise AssertionError("release without acquisition")
        self.busy = False
        self.releases += 1

    def quarantine(self, lease):
        self.busy = True
        self.quarantines += 1


class FakeCDP:
    def __init__(self, fail_method=None):
        self.calls = []
        self.fail_method = fail_method

    def send(self, method, params=None, session_id=None):
        self.calls.append((method, copy.deepcopy(params), session_id))
        if method == self.fail_method:
            raise RuntimeError("ack lost")
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "element-1"}}
        return {}


def node(*, box=None, attrs=(), backend=41, ordinal=0, ident="n1"):
    return {
        "id": ident, "role": "button", "name": "Do it", "depth": 0,
        "attrs": attrs, "ancestors": (), "ordinal": ordinal,
        "cardinality": 1, "backend_id": backend,
        "box": [10, 20, 20, 20] if box is None else box,
    }


class TestFocusPolicy(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.a = Handle("cdp_tab", "a", 3)
        self.b = Handle("cdp_tab", "b", 3)
        self.lease = ActorLease(self.a, "alice", 3, b"genuine-nonce", 500)
        self.registry = FakeRegistry([Record(self.a), Record(self.b)])
        self.locks = FakeLocks([self.lease])
        self.policy = FocusPolicy(self.registry, self.locks, clock=self.clock)

    def test_explicit_grant_and_never_steal_focus(self):
        with self.assertRaises(FocusRefused):
            self.policy.grant(self.lease, domain="cdp:s1")
        self.policy.set_focus(self.a, domain="cdp:s1", epoch=2)
        grant = self.policy.grant(self.lease, domain="cdp:s1")
        self.assertEqual(grant.epoch, 2)
        self.assertEqual(grant.lease.nonce, b"genuine-nonce")
        self.policy.consume(grant)
        with self.assertRaises(FocusRefused):
            self.policy.grant(self.lease, domain="cdp:s1")

    def test_modal_and_wrong_target_block_grant(self):
        self.policy.set_focus(self.b, domain="cdp:s1", epoch=2)
        with self.assertRaises(FocusRefused):
            self.policy.grant(self.lease, domain="cdp:s1")
        self.policy.set_modal(self.b, domain="cdp:s1")
        with self.assertRaises(FocusRefused):
            self.policy.set_focus(self.a, domain="cdp:s1", epoch=2)
        with self.assertRaises(FocusRefused):
            self.policy.grant(self.lease, domain="cdp:s1")

    def test_expired_forged_and_stale_lease_refused(self):
        self.policy.set_focus(self.a, domain="cdp:s1", epoch=2)
        self.clock.value = 500
        with self.assertRaises(FocusRefused):
            self.policy.grant(self.lease, domain="cdp:s1")
        self.clock.value = 100
        fake = ActorLease(self.a, "alice", 3, b"forged", 500)
        with self.assertRaises(FocusRefused):
            self.policy.grant(fake, domain="cdp:s1")
        with self.assertRaises(FocusRefused):
            self.policy.set_focus(self.a, domain="cdp:s1", epoch=1)

    def test_classification_is_conservative(self):
        self.assertEqual(classify("api", "call"), FocusClass.BACKGROUND_SAFE)
        self.assertEqual(classify("ax", "click"), FocusClass.SESSION_SCOPED)
        self.assertEqual(classify("coordinates", "click"), FocusClass.FOCUS_REQUIRED)
        self.assertEqual(classify("ax", "press"), FocusClass.FOCUS_REQUIRED)
        self.assertEqual(classify("other", "unknown"), FocusClass.REFUSE)


class TestAuthorizedExecutor(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.a = Handle("cdp_tab", "a", 3)
        self.b = Handle("cdp_tab", "b", 3)
        self.lease = ActorLease(self.a, "owner", 3, b"n1", 500)
        self.registry = FakeRegistry([Record(self.a), Record(self.b)])
        self.locks = FakeLocks([self.lease])
        self.policy = FocusPolicy(self.registry, self.locks, clock=self.clock)
        self.policy.set_focus(self.a, domain="cdp:s1", epoch=2)
        self.focus = self.policy.grant(self.lease, domain="cdp:s1")
        self.fence = FakeFence()
        self.cdp = FakeCDP()
        self.original = node()
        self.after = node(attrs=(("selected", "true"),))
        frame = FrameRef.from_rgb(self.a, 2, 7, np.zeros((600, 800, 3), dtype=np.uint8))
        self.obs = Observation(
            self.a, 2, 7, 1000000,
            {"handle": self.a, "epoch": 2, "frame_seq": 7,
             "viewport": {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0},
             "nodes": [copy.deepcopy(self.original)], "focused_node_id": "n1"}, frame)
        self.registry.current["a"] = self.obs
        self.action = {"op": "click", "target": "n1",
                       "expect": {"target_states": {"selected": "true"}}}

    def run_action(self, read_nodes=None, action=None, **changes):
        args = dict(lease=self.lease, focus=self.focus, observation=self.obs,
                    registry=self.registry, focus_policy=self.policy,
                    writer_fence=self.fence, clock=self.clock)
        args.update(changes)
        snapshots = iter(([self.original], [self.after]))
        return run_ax_action(self.cdp, "s1", [self.original],
                             self.action if action is None else action,
                             read_nodes or (lambda: next(snapshots)),
                             retries=3, wait_ms=0, sleep=lambda _: None, **args)

    def test_positive_fresh_unique_backend_dispatches_once(self):
        receipt = self.run_action()
        self.assertEqual((receipt["ok"], receipt["attempts"], receipt["verified"]), (1, 1, 1))
        self.assertEqual(self.fence.releases, 1)
        self.assertIn("Runtime.callFunctionOn", [m for m, _, _ in self.cdp.calls])
        with self.assertRaises(ActionError):
            self.run_action()

    def test_missing_authority_and_wrong_target_focus_refuse_before_send(self):
        with self.assertRaises(ActionError):
            self.run_action(focus=None)
        fake_focus = SimpleNamespace(lease=ActorLease(self.b, "owner", 3, b"n2", 500),
                                     domain="cdp:s1", epoch=2)
        with self.assertRaises(ActionError):
            self.run_action(focus=fake_focus)
        self.assertEqual(self.cdp.calls, [])

    def test_modal_revocation_refuses_before_send(self):
        self.policy.set_modal(self.b, domain="cdp:s1")
        with self.assertRaises(ActionError):
            self.run_action()
        self.assertEqual(self.cdp.calls, [])

    def test_stop_iteration_or_unreadable_fresh_gate_never_sends(self):
        with self.assertRaises(ActionError):
            self.run_action(read_nodes=lambda: next(iter(())))
        self.assertEqual(self.cdp.calls, [])
        self.assertEqual(self.fence.releases, 1)
        self.assertEqual(self.fence.quarantines, 0)

    def test_fresh_tie_refused_without_transport(self):
        tied = [node(), node()]
        with self.assertRaises(ActionError):
            self.run_action(read_nodes=lambda: tied)
        self.assertEqual(self.cdp.calls, [])

    def test_stale_observation_and_sibling_viewport_refused(self):
        self.registry.current.clear()
        with self.assertRaises(ActionError):
            self.run_action()
        self.assertEqual(self.cdp.calls, [])
        self.registry.current["a"] = self.obs
        pointer = dict(self.action, op="hover", viewport=[320, 240])
        with self.assertRaises(ActionError):
            self.run_action(action=pointer)
        self.assertEqual(self.cdp.calls, [])

    def test_keyboard_must_match_focused_element(self):
        self.obs.state["focused_node_id"] = "sibling"
        with self.assertRaises(ActionError):
            self.run_action(action={"op": "press", "target": "n1", "key": "Enter"})
        self.assertEqual(self.cdp.calls, [])

    def test_positive_same_target_pointer_uses_planner_and_no_other_viewport(self):
        pointer = dict(self.action, path="coordinates", op="click", viewport=[800, 600])
        receipt = self.run_action(action=pointer, allow_coordinates=True)
        self.assertEqual(receipt["ok"], 1)
        events = [params for method, params, sid in self.cdp.calls
                  if method == "Input.dispatchMouseEvent"]
        self.assertEqual([(e["x"], e["y"]) for e in events], [(20, 30)] * 3)
        self.assertEqual([e["type"] for e in events],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertTrue(all(sid == "s1" for _, _, sid in self.cdp.calls))

    def test_irreversible_transport_ambiguity_quarantines_no_fallback_or_retry(self):
        self.cdp = FakeCDP(fail_method="Runtime.callFunctionOn")
        action = dict(self.action, irreversible=True)
        receipt = self.run_action(action=action, allow_coordinates=True)
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["attempts"], 1)
        self.assertEqual(self.fence.quarantines, 1)
        self.assertEqual(len([m for m, _, _ in self.cdp.calls if m == "Runtime.callFunctionOn"]), 1)
        self.assertFalse(any(m == "Input.dispatchMouseEvent" for m, _, _ in self.cdp.calls))

    def test_busy_writer_fence_refuses_without_send(self):
        self.fence.busy = True
        with self.assertRaises(ActionError):
            self.run_action()
        self.assertEqual(self.cdp.calls, [])
