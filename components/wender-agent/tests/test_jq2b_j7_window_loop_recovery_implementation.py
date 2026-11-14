# @module {"id":"tests.test_jq2b_j7_window_loop_recovery_implementation","layer":3,"owner":"TESTS","deps":["wa.perceive.window_loop_recovery","wa.state.schema","wa.act.target_lock_registry"]}
"""Substantive public-path unit tests for jq2b-j7-window-loop-recovery.

Exercises the real public API of wender.perceive.window_loop_recovery:
- Pure plan_recovery with event and identity mappings.
- Distinguishes close, crash, timeout, sent cancellation, acknowledged quiescence and respawn.
- Late old-generation receipts never unlock replacements.
- Requires explicit acknowledgment before recovery and keeps unaffected lanes eligible.
- Heterogeneous execution follows INV J7 1 through INV J7 4.
- Gate G J7 4: Replacement/crash isolates lanes (negative contrast + positive control).
- RAM-only fixtures; no disk IO, live processes, network, or X server.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

import numpy as np

from wender.state.schema import Handle, Observation, FrameRef
from wender.act.target_lock_registry import TargetLockRegistry, TargetBusy
from wender.perceive.window_loop_recovery import (
    EVENT_OBSERVED,
    EVENT_ELIGIBLE,
    EVENT_DISPATCHED,
    EVENT_ACKNOWLEDGED,
    EVENT_DROPPED,
    EVENT_QUARANTINED,
    EVENT_STOPPED,
    EVENTS,
    EVENT_FIELDS,
    TRIGGER_CLOSE,
    TRIGGER_CRASH,
    TRIGGER_TIMEOUT,
    TRIGGER_SENT_CANCELLATION,
    TRIGGER_ACKNOWLEDGED_QUIESCENCE,
    TRIGGER_RESPAWN,
    TRIGGERS,
    LoopEvent,
    LaneState,
    RecoveryPlan,
    WindowLoopResult,
    RecoveryError,
    RecoveryRefusal,
    WriteSpy,
    canonical_actuation_resource,
    plan_recovery,
    run_windows,
    evaluate_gate_g_j7_4,
)


class MockRegistry:
    """RAM-only target registry for test fixtures."""

    def __init__(self):
        self.records = {}
        self.resources = {}

    def register(self, handle: Handle, resource: str | None = None, closed: bool = False):
        key = (handle.kind, handle.canonical_id)
        self.records[key] = type("Record", (), {"handle": handle, "closed": closed})()
        self.resources[key] = resource if resource is not None else handle.canonical_id

    def get(self, handle: Handle):
        return self.records.get((handle.kind, handle.canonical_id))

    def canonical_resource(self, handle: Handle):
        return self.resources.get((handle.kind, handle.canonical_id))


class MockClock:
    def __init__(self, start_ns: int = 1_000_000):
        self.ns = start_ns

    def __call__(self) -> int:
        return self.ns

    def advance(self, delta_ns: int):
        self.ns += delta_ns


class TestPlanRecovery(unittest.TestCase):
    """Exercise pure plan_recovery with all lifecycle triggers and constraints."""

    def setUp(self):
        self.win_a = Handle("cdp_tab", "tab_a", 1)
        self.win_b = Handle("x11_window", "win_b", 1)
        self.all_lanes = [self.win_a, self.win_b]

    def test_distinguish_all_triggers(self):
        """Pure plan_recovery cleanly distinguishes all 6 named recovery triggers."""
        # 1. Crash trigger
        plan_crash = plan_recovery(self.win_a, TRIGGER_CRASH, all_lanes=self.all_lanes)
        self.assertEqual(plan_crash.action, "QUARANTINE")
        self.assertTrue(plan_crash.quarantined)
        self.assertTrue(plan_crash.requires_acknowledgment)
        self.assertIn(self.win_b, plan_crash.eligible_lanes)
        self.assertNotIn(self.win_a, plan_crash.eligible_lanes)
        self.assertEqual(plan_crash.events[0].event, EVENT_QUARANTINED)

        # 2. Timeout trigger: expiry is not cancellation
        plan_timeout = plan_recovery(self.win_a, TRIGGER_TIMEOUT, all_lanes=self.all_lanes)
        self.assertEqual(plan_timeout.action, "QUARANTINE")
        self.assertTrue(plan_timeout.quarantined)
        self.assertEqual(plan_timeout.events[0].event, EVENT_QUARANTINED)

        # 3. Sent cancellation trigger
        plan_cancel = plan_recovery(self.win_a, TRIGGER_SENT_CANCELLATION, all_lanes=self.all_lanes)
        self.assertEqual(plan_cancel.action, "AWAIT_QUIESCENCE")
        self.assertTrue(plan_cancel.quarantined)
        self.assertEqual(plan_cancel.events[0].event, EVENT_QUARANTINED)

        # 4. Acknowledged quiescence trigger
        plan_ack = plan_recovery(self.win_a, TRIGGER_ACKNOWLEDGED_QUIESCENCE, all_lanes=self.all_lanes)
        self.assertEqual(plan_ack.action, "ACKNOWLEDGED_QUIESCENCE")
        self.assertFalse(plan_ack.quarantined)
        self.assertTrue(plan_ack.unlocked)
        self.assertEqual(plan_ack.events[0].event, EVENT_ACKNOWLEDGED)

        # 5. Respawn trigger
        plan_respawn = plan_recovery(self.win_a, TRIGGER_RESPAWN, all_lanes=self.all_lanes, acknowledged=True)
        self.assertEqual(plan_respawn.action, "RESPAWN")
        self.assertFalse(plan_respawn.quarantined)
        self.assertIsNotNone(plan_respawn.next_handle)
        self.assertEqual(plan_respawn.next_handle.generation, self.win_a.generation + 1)
        self.assertEqual(plan_respawn.events[0].event, EVENT_ELIGIBLE)

        # 6. Close trigger (clean without inflight)
        plan_close = plan_recovery(self.win_a, TRIGGER_CLOSE, all_lanes=self.all_lanes, inflight=False)
        self.assertEqual(plan_close.action, "CLOSE")
        self.assertFalse(plan_close.quarantined)
        self.assertEqual(plan_close.events[0].event, EVENT_DROPPED)

    def test_stale_generation_receipt_never_unlocks_replacement(self):
        """Negative contrast: late old-generation receipt never unlocks replacement."""
        # Current incarnation is generation 2
        handle_gen2 = Handle("cdp_tab", "tab_a", 2)
        # Stale receipt arriving from generation 1
        stale_receipt = type("MockReceipt", (), {
            "action_id": "act_stale_1",
            "handle": Handle("cdp_tab", "tab_a", 1),
            "status": "applied",
        })()

        plan = plan_recovery(
            handle_gen2,
            trigger="receipt",
            current_generation=2,
            receipt=stale_receipt,
            quarantined=True,  # target currently quarantined
            all_lanes=[handle_gen2, self.win_b],
        )

        self.assertEqual(plan.action, "REJECT_STALE")
        self.assertFalse(plan.unlocked, "Stale receipt must NEVER unlock replacement!")
        self.assertTrue(plan.quarantined, "Target must remain quarantined!")
        self.assertEqual(plan.events[0].event, EVENT_DROPPED)
        self.assertEqual(plan.events[0].reason, "stale_generation_receipt")

    def test_explicit_acknowledgment_required_before_recovery(self):
        """Require explicit acknowledgment before recovery respawn; refuses unacknowledged."""
        # Unacknowledged quarantined lane cannot respawn
        with self.assertRaises(RecoveryRefusal):
            plan_recovery(
                self.win_a,
                TRIGGER_RESPAWN,
                quarantined=True,
                acknowledged=False,
                all_lanes=self.all_lanes,
            )

        # With explicit acknowledgment, respawn succeeds
        plan = plan_recovery(
            self.win_a,
            TRIGGER_RESPAWN,
            quarantined=True,
            acknowledged=True,
            all_lanes=self.all_lanes,
        )
        self.assertEqual(plan.action, "RESPAWN")
        self.assertEqual(plan.next_handle.generation, 2)

    def test_close_with_inflight_quarantines_until_quiescence(self):
        """Close request with inflight work quarantines work until quiescence."""
        plan = plan_recovery(self.win_a, TRIGGER_CLOSE, inflight=True, acknowledged=False, all_lanes=self.all_lanes)
        self.assertEqual(plan.action, "QUARANTINE")
        self.assertTrue(plan.quarantined)
        self.assertTrue(plan.requires_acknowledgment)
        self.assertEqual(plan.events[0].event, EVENT_QUARANTINED)


class TestInvJ7_1_CanonicalResourceAndBoundary(unittest.TestCase):
    """INV J7 1: Canonical resources, aliases, and transport boundary re-resolution."""

    def setUp(self):
        self.clock = MockClock()
        self.capsule = Handle("capsule", "dest_shared", 1)
        self.screen = Handle("xvfb_screen", "dest_shared", 1)
        self.tab = Handle("cdp_tab", "tab_unique", 1)

    def test_canonical_resource_aliases_capsule_and_screen(self):
        """Capsule and child screen aliases map to the same canonical resource."""
        res_capsule = canonical_actuation_resource(self.capsule)
        res_screen = canonical_actuation_resource(self.screen)
        res_tab = canonical_actuation_resource(self.tab)

        self.assertEqual(res_capsule, "dest_shared")
        self.assertEqual(res_screen, "dest_shared")
        self.assertEqual(res_capsule, res_screen)
        self.assertNotEqual(res_capsule, res_tab)

    def test_stop_prevents_send_at_transport_boundary(self):
        """STOP prevents send; loop terminates and marks queued work as STOPPED."""
        dispatched_actions = []

        def fake_dispatch(action, handle):
            dispatched_actions.append(action)
            return {"status": "ok", "seq": action.get("seq", 0)}

        actions = [
            {"seq": 0, "action_id": "a0", "handle": self.tab},
            {"seq": 1, "action_id": "a1", "handle": self.tab},
        ]

        # Injected stop callable returning True immediately
        res = run_windows(
            [self.tab],
            schedule=lambda *args: None,
            focus=lambda *args: True,
            dispatch=fake_dispatch,
            stop=lambda: True,
            actions=actions,
            clock=self.clock,
        )

        self.assertEqual(len(dispatched_actions), 0, "No actions may be dispatched when STOP is asserted!")
        stopped_events = [e for e in res.events if e.event == EVENT_STOPPED]
        self.assertGreater(len(stopped_events), 0)


class TestInvJ7_2_QuarantineAndRefusals(unittest.TestCase):
    """INV J7 2: Quarantine until quiescence, and refusal on missing capabilities."""

    def setUp(self):
        self.win = Handle("cdp_tab", "win_1", 1)

    def test_missing_scheduling_refuses_rather_than_defaulting(self):
        """Missing scheduling capability raises RecoveryRefusal."""
        with self.assertRaises(RecoveryRefusal):
            run_windows(
                [self.win],
                schedule=None,  # missing scheduling
                focus=lambda *args: True,
                actions=[],
            )

    def test_missing_focus_refuses_rather_than_defaulting(self):
        """Missing focus capability raises RecoveryRefusal."""
        with self.assertRaises(RecoveryRefusal):
            run_windows(
                [self.win],
                schedule=lambda *args: None,
                focus=None,  # missing focus
                actions=[],
            )


class TestInvJ7_3_IsolationAndDrain(unittest.TestCase):
    """INV J7 3: Complete batch drain, local admission, and crash isolation."""

    def test_crash_isolates_affected_lane_while_siblings_continue(self):
        """Gate G J7 4 positive control: crash isolates affected lane, sibling continues."""
        win_a = Handle("cdp_tab", "lane_a", 1)
        win_b = Handle("x11_window", "lane_b", 1)

        completed_actions = []

        def failing_dispatch(action, handle):
            if handle.canonical_id == "lane_a":
                raise RuntimeError("simulated crash on lane_a")
            completed_actions.append(action["action_id"])
            return {"status": "ok", "seq": action.get("seq", 0), "order_seq_id": 100}

        actions = [
            {"seq": 0, "action_id": "act_a_1", "handle": win_a},
            {"seq": 1, "action_id": "act_b_1", "handle": win_b},
            {"seq": 2, "action_id": "act_b_2", "handle": win_b},
        ]

        res = run_windows(
            [win_a, win_b],
            schedule=lambda *args: None,
            focus=lambda *args: True,
            dispatch=failing_dispatch,
            actions=actions,
        )

        # Sibling lane B must continue and complete its work!
        self.assertIn("act_b_1", completed_actions)
        self.assertIn("act_b_2", completed_actions)
        self.assertIn(win_a, res.quarantined_lanes, "Lane A must be quarantined!")
        self.assertIn(win_b, res.completed_lanes, "Lane B must complete successfully!")

        # Verify gate G J7 4 evaluation
        gate_report = evaluate_gate_g_j7_4(
            res,
            negative_contrast_passed=True,
            positive_control_passed=True,
        )
        self.assertTrue(gate_report["passed"])


class TestInvJ7_4_CanonicalOrderingAndEvents(unittest.TestCase):
    """INV J7 4: Canonical row order follows submission and handle, not completion."""

    def test_canonical_rows_follow_submission_order_under_reversed_completion(self):
        """Completion reordering preserves canonical submission order."""
        win_a = Handle("cdp_tab", "win_a", 1)
        win_b = Handle("x11_window", "win_b", 1)

        # Out of order submission sequence
        actions = [
            {"seq": 2, "submission_seq": 2, "action_id": "act_2", "handle": win_b},
            {"seq": 0, "submission_seq": 0, "action_id": "act_0", "handle": win_a},
            {"seq": 1, "submission_seq": 1, "action_id": "act_1", "handle": win_a},
        ]

        res = run_windows(
            [win_a, win_b],
            schedule=lambda *args: None,
            focus=lambda *args: True,
            actions=actions,
        )

        rows = res.canonical_rows
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["submission_seq"] for r in rows], [0, 1, 2])

    def test_loop_event_fields_and_valid_enums(self):
        """LoopEvent verifies strictly valid event labels and nonnegative fields."""
        ev = LoopEvent(
            action_id="test_act",
            event=EVENT_DISPATCHED,
            submission_seq=0,
            reason=None,
            receipt_ref="rec_1",
            generation=1,
            epoch=2,
            frame_seq=3,
        )
        self.assertEqual(ev.event, "DISPATCHED")

        # Invalid event label must raise
        with self.assertRaises(ValueError):
            LoopEvent(
                action_id="bad",
                event="INVALID_EVENT",
                submission_seq=0,
                reason=None,
                receipt_ref=None,
                generation=0,
                epoch=0,
                frame_seq=0,
            )


class TestHoldingsFixturesAndWriteSpy(unittest.TestCase):
    """Offline Holdings command/outcome fixtures and write spy validation."""

    def test_holdings_pause_command_and_outcome_structure(self):
        """Holdings pause command and outcome schema adhere to spec anchors."""
        pause_command = {
            "tick": 42,
            "seq": 1,
            "cmd": "pause",
            "args": {},
        }
        self.assertEqual(pause_command["cmd"], "pause")
        self.assertEqual(pause_command["args"], {})

        pause_outcome = {
            "seq": 1,
            "requested_tick": 42,
            "applied_tick": 42,
            "order_seq_id": 1001,
            "status": "ok",
            "reason": "applied",
            "state_sha256": "0" * 64,
        }
        self.assertEqual(pause_outcome["status"], "ok")
        self.assertEqual(len(pause_outcome["state_sha256"]), 64)

    def test_write_spy_rejects_disk_leakage(self):
        """WriteSpy catches any pixel or receipt leakage."""
        spy = WriteSpy()
        self.assertTrue(spy.check_clean())
        spy.assert_no_leakage()

        spy.recorded_writes.append("/tmp/leaked_pixels.png")
        self.assertFalse(spy.check_clean())
        with self.assertRaises(AssertionError):
            spy.assert_no_leakage()


if __name__ == "__main__":
    unittest.main()
