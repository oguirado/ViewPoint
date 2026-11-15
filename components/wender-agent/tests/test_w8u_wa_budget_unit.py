"""W8U budget owner contract: offline positive and MUST-FAIL controls.

This gate imports the merged production BudgetGovernor and WindowScheduler.
Dependencies that belong to other wave owners are injected as simple in-RAM
capabilities. All clocks, capacities and fixtures are TARGET values, not host
measurements. Tests do not start servers, browsers or subprocesses.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from threading import Barrier, Thread
from unittest.mock import patch
import unittest

from wender.act.budget_governor import (
    BudgetGovernor, BudgetExhausted, ConfigurationRequired, DeadlineInfeasible,
    Reservation, ReservationInactive, StaleHandle,
)
from wender.act.window_scheduler import (
    ActionRequest, ActorLease, Completion, FocusLease, Handle, Receipt,
    TargetRecord, Ticket, WindowScheduler, WriterFence, PREREQUISITE_GATES,
)


class Clock:
    def __init__(self, now=100):
        self.now = now

    def __call__(self):
        return self.now


class BudgetContract(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.handle = Handle("xvfb_screen", "target", 1)

    def governor(self, global_budget=5, per_target_budget=5, **config):
        config.setdefault("clock", self.clock)
        config.setdefault("telemetry", self.events)
        config.setdefault("service_cap_ns", 10)
        return BudgetGovernor(global_budget, per_target_budget, **config)

    def reserve(self, governor, cost=1, *, action_id="action", handle=None,
                deadline_ns=120, **extra):
        return governor.reserve(handle or self.handle, cost, action_id=action_id,
                                deadline_ns=deadline_ns, **extra)

    def test_frozen_legacy_fields_and_action_binding(self):
        self.assertEqual([f.name for f in fields(Reservation)],
                         ["handle", "cost", "deadline_ns", "nonce"])
        g = self.governor()
        r = self.reserve(g, 3, action_id="step-1")
        self.assertEqual((r.cost, r.handle.generation), (3, 1))
        with self.assertRaises(FrozenInstanceError):
            r.cost = 1
        self.assertEqual(self.events[0],
                         {"event": "reserve", "action_id": "step-1",
                          "handle": ("xvfb_screen", "target", 1),
                          "cost": 3, "consumed": None})
        g.settle(r, consumed=True)
        self.assertEqual((g.spent_global, g.remaining_global, g.pending_count),
                         (3, 2, 0))
        self.assertEqual(self.events[-1]["consumed"], True)
        self.assertEqual([e["event"] for e in self.events],
                         ["reserve", "settle"])

    def test_required_telemetry_clock_service_cap_and_target_limit(self):
        with self.assertRaisesRegex(ConfigurationRequired,
                                    "CONFIG_REQUIRED: telemetry is absent"):
            self.reserve(self.governor(telemetry=None))
        with self.assertRaisesRegex(ConfigurationRequired,
                                    "CONFIG_REQUIRED: clock is absent"):
            self.reserve(self.governor(clock=None))
        with self.assertRaisesRegex(ConfigurationRequired,
                                    "CONFIG_REQUIRED: service_cap_ns is absent"):
            self.reserve(self.governor(service_cap_ns=None))
        with self.assertRaisesRegex(ConfigurationRequired,
                                    "CONFIG_REQUIRED: missing budget"):
            self.reserve(self.governor(per_target_budget={}))
        with self.assertRaisesRegex(ValueError,
                                    "global_budget must be a nonnegative int"):
            self.governor(global_budget=None)
        self.assertEqual(self.events, [])

    def test_bool_noninteger_zero_and_negative_costs_are_not_admitted(self):
        g = self.governor()
        for bad in (True, False, 1.0, "1", 0, -1):
            with self.subTest(cost=bad):
                with self.assertRaisesRegex(ValueError,
                                            "^cost must be a positive int$"):
                    self.reserve(g, bad)
        self.assertEqual((g.remaining_global, g.pending_count, len(self.events)),
                         (5, 0, 0))

    def test_action_identifier_validation_before_side_effect(self):
        g = self.governor()
        for bad in ("", " a", "a ", "a" + chr(0) + "b", True, 1):
            with self.subTest(action_id=repr(bad)):
                with self.assertRaisesRegex(ValueError,
                                            "^action_id must be a nonempty canonical string$"):
                    self.reserve(g, action_id=bad)
        self.assertEqual((g.pending_count, len(self.events)), (0, 0))

    def test_injected_deadline_and_explicit_now_ns(self):
        g = self.governor()
        with self.assertRaisesRegex(DeadlineInfeasible,
                                    "DEADLINE_INFEASIBLE: cannot fit service cap"):
            self.reserve(g, deadline_ns=109)
        with self.assertRaises(DeadlineInfeasible):
            self.reserve(g, deadline_ns=119, now_ns=110)
        with self.assertRaisesRegex(ValueError,
                                    "deadline_ns must be a nonnegative int"):
            self.reserve(g, deadline_ns=True)
        self.assertEqual(self.events, [])
        r = self.reserve(g, deadline_ns=110, now_ns=100)
        g.settle(r, consumed=False)
        self.assertEqual((g.remaining_global, g.spent_global), (5, 0))
        self.assertEqual([e["consumed"] for e in self.events], [None, False])

    def test_exact_integer_credit_and_one_release(self):
        exact = 10 ** 50
        g = self.governor(exact, exact)
        r = self.reserve(g, exact)
        self.assertEqual(g.remaining_global, 0)
        g.settle(r, consumed=False)
        self.assertEqual(g.remaining_global, exact)
        self.assertEqual(g.spent_global, 0)
        with self.assertRaisesRegex(ReservationInactive,
                                    "^RESERVATION_INACTIVE: not a pending issued token$"):
            g.settle(r, consumed=False)
        self.assertEqual(len(self.events), 2)

    def test_global_and_target_overcommit_are_atomic(self):
        g = self.governor(global_budget=2, per_target_budget=2)
        a = self.reserve(g, 2, action_id="a")
        other = Handle("cdp_tab", "other", 1)
        with self.assertRaisesRegex(BudgetExhausted,
                                    "BUDGET_EXHAUSTED: global credits"):
            self.reserve(g, 1, action_id="b", handle=other)
        self.assertEqual(g.remaining_for(other), 2)
        self.assertEqual(len(self.events), 1)
        g.settle(a, consumed=True)
        p = self.governor(global_budget=5, per_target_budget=1)
        t = self.reserve(p, 1, action_id="t")
        with self.assertRaisesRegex(BudgetExhausted,
                                    "BUDGET_EXHAUSTED: per-target credits"):
            self.reserve(p, 1, action_id="t-next")
        self.assertEqual((p.remaining_global, p.remaining_for(self.handle)), (4, 0))
        p.settle(t, consumed=False)

    def test_foreign_forged_and_double_settlement_refused(self):
        a, b = self.governor(), self.governor()
        r = self.reserve(a)
        for governor, token in ((b, r), (a, replace(r))):
            with self.assertRaises(ReservationInactive):
                governor.settle(token, consumed=False)
        with self.assertRaisesRegex(ValueError, "^consumed must be a bool$"):
            a.settle(r, consumed=1)
        self.assertEqual((a.pending_count, a.remaining_global), (1, 4))
        a.settle(r, consumed=True)
        with self.assertRaises(ReservationInactive):
            a.settle(r, consumed=True)
        with self.assertRaises(ReservationInactive):
            a.settle(r, consumed=False)
        self.assertEqual((a.spent_global, a.remaining_global, len(self.events)),
                         (1, 4, 2))

    def test_generation_swap_revokes_unsent_reservation(self):
        g = self.governor()
        old = self.reserve(g, 2, action_id="old")
        newer = Handle("xvfb_screen", "target", 2)
        new = self.reserve(g, 3, action_id="new", handle=newer)
        with self.assertRaises(ReservationInactive):
            g.settle(old, consumed=False)
        with self.assertRaisesRegex(StaleHandle, "STALE_HANDLE"):
            self.reserve(g, 1, action_id="replay")
        self.assertEqual((g.remaining_global, g.pending_count), (2, 1))
        g.settle(new, consumed=True)
        self.assertEqual((g.remaining_global, g.spent_global), (2, 3))
        self.assertEqual([e["event"] for e in self.events],
                         ["reserve", "invalidate", "reserve", "settle"])

    def test_committed_or_ambiguous_send_cannot_be_refunded(self):
        g = self.governor(global_budget=1, per_target_budget=1)
        r = self.reserve(g)
        g.commit(r)  # Host send fence: even an ambiguous result spends.
        with self.assertRaises(ReservationInactive):
            g.settle(r, consumed=False)
        g.cancel(r)  # Legacy no-op; not a second release.
        self.assertEqual((g.remaining_global, g.spent_global), (0, 1))
        self.assertEqual([e["event"] for e in self.events],
                         ["reserve", "commit"])

    def test_legacy_default_now_and_duplicate_cancel_are_preserved(self):
        g = BudgetGovernor(2, 2, service_cap_ns=10)
        r = g.reserve(self.handle, 2, deadline_ns=10)
        g.cancel(r)
        g.cancel(r)
        g.release(r)
        self.assertEqual((g.remaining_global, g.spent_global), (2, 0))
        after = g.reserve(self.handle, 2, deadline_ns=10, now_ns=0)
        g.commit(after)
        g.cancel(after)
        with self.assertRaises(ReservationInactive):
            g.commit(after)
        self.assertEqual((g.remaining_global, g.spent_global), (0, 2))

    def test_parallel_global_and_target_caps_with_barriers(self):
        for same_target in (False, True):
            with self.subTest(same_target=same_target):
                events = []
                g = BudgetGovernor(6 if same_target else 1, 1,
                                   service_cap_ns=10, clock=self.clock,
                                   telemetry=events)
                barrier = Barrier(6)
                holds, refusals, unexpected = [], [], []

                def attempt(index):
                    try:
                        barrier.wait(timeout=5)
                        handle = (self.handle if same_target else
                                  Handle("cdp_tab", "target-%d" % index, 1))
                        holds.append(g.reserve(handle, 1, action_id="a%d" % index,
                                               deadline_ns=120))
                    except BudgetExhausted as exc:
                        refusals.append(str(exc))
                    except BaseException as exc:
                        unexpected.append(str(exc))

                threads = [Thread(target=attempt, args=(n,)) for n in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=8)
                self.assertFalse(any(t.is_alive() for t in threads))
                self.assertEqual(unexpected, [])
                self.assertEqual((len(holds), len(refusals)), (1, 5))
                self.assertEqual(len(events), 1)
                g.settle(holds[0], consumed=False)
                self.assertEqual(g.pending_count, 0)
                self.assertEqual(g.remaining_global, 6 if same_target else 1)
                self.assertEqual(len(events), 2)

    def test_no_disk_network_or_process_side_effects(self):
        g = self.governor()
        with (patch("builtins.open", side_effect=AssertionError("disk")),
              patch("pathlib.Path.open", side_effect=AssertionError("disk")),
              patch("pathlib.Path.write_bytes", side_effect=AssertionError("frames to disk")),
              patch("os.open", side_effect=AssertionError("disk")),
              patch("socket.socket", side_effect=AssertionError("network")),
              patch("subprocess.Popen", side_effect=AssertionError("process"))):
            r = self.reserve(g)
            g.settle(r, consumed=True)
        self.assertEqual((g.spent_global, len(self.events)), (1, 2))


class Registry:
    def __init__(self, handle):
        self.handle = handle
        self.record = TargetRecord(handle, object(), True, 0, None, False, False)

    def get(self, handle):
        return self.record if handle == self.handle else None

    def canonical_resource(self, handle):
        return "xvfb:target" if handle == self.handle else "other"


class Locks:
    def __init__(self):
        self.leases = []
        self.quarantines = []

    def try_acquire(self, handle, *, owner, expires_ns):
        lease = ActorLease(handle, owner, handle.generation, b"owned", expires_ns)
        self.leases.append(lease)
        return lease

    def release(self, lease):
        self.leases.remove(lease)

    def quarantine(self, lease):
        self.quarantines.append(lease)


class Focus:
    def __init__(self):
        self.leases = []
        self.quarantines = []

    def try_acquire(self, actor, *, epoch):
        lease = FocusLease(actor, "domain:target", epoch)
        self.leases.append(lease)
        return lease

    def release(self, lease):
        self.leases.remove(lease)

    def quarantine(self, lease):
        self.quarantines.append(lease)


class Chain:
    def is_settled(self, handle, epoch):
        return True


class Dispatch:
    offline = True

    def __init__(self, fence):
        self.writer_fence = fence
        self.prerequisite_gates = dict.fromkeys(PREREQUISITE_GATES, True)
        self.sends = []
        self.behavior = "ack"

    def __call__(self, request, context):
        if self.behavior == "stop":
            self.writer_fence.stop(request.handle)
        context.send(self.sends.append, request.action_id)
        if self.behavior == "unknown":
            return None
        return Completion(request.action_id, request.handle, "SUCCEEDED", True)


class SchedulerBudgetContract(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(now=0)
        self.events = []
        self.handle = Handle("xvfb_screen", "target", 1)
        self.registry = Registry(self.handle)
        self.locks, self.focus = Locks(), Focus()
        self.fence = WriterFence(clock=self.clock)
        self.dispatch = Dispatch(self.fence)
        self.budget = BudgetGovernor(
            2, 2, service_cap_ns=10, clock=self.clock, telemetry=self.events)
        self.scheduler = WindowScheduler(
            registry=self.registry, locks=self.locks, focus_policy=self.focus,
            budget=self.budget, chain=Chain(), writer_fence=self.fence,
            dispatch=self.dispatch, max_queue=4, max_targets=1,
            service_cap_ns=10, clock=self.clock)

    def request(self, action_id="live", cost=2):
        return ActionRequest(self.handle, action_id, 0, {}, cost, 100)

    def test_scheduler_reserve_and_settle_consumed(self):
        ticket = self.scheduler.submit(self.request())
        self.assertIsInstance(ticket, Ticket)
        receipts = self.scheduler.step(0)
        self.assertEqual([(r.status, r.action_id) for r in receipts],
                         [("SUCCEEDED", "live")])
        self.assertEqual(self.dispatch.sends, ["live"])
        self.assertEqual((self.budget.spent_global,
                          self.budget.remaining_global,
                          self.budget.pending_count), (2, 0, 0))
        self.assertEqual([e["event"] for e in self.events],
                         ["reserve", "settle"])
        self.assertEqual(self.events[-1]["consumed"], True)

    def test_host_stop_fence_refuses_send_and_refunds_unsent(self):
        self.dispatch.behavior = "stop"
        ticket = self.scheduler.submit(self.request())
        self.assertIsInstance(ticket, Ticket)
        receipts = self.scheduler.step(0)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status, "REFUSED")
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual((self.budget.spent_global,
                          self.budget.remaining_global,
                          self.budget.pending_count), (0, 2, 0))
        self.assertEqual([e["consumed"] for e in self.events], [None, False])

    def test_ambiguous_sent_work_stays_held_until_acknowledged_quiescence(self):
        self.dispatch.behavior = "unknown"
        ticket = self.scheduler.submit(self.request())
        self.assertIsInstance(ticket, Ticket)
        self.assertEqual(self.scheduler.step(0), [])
        self.assertEqual(self.dispatch.sends, ["live"])
        self.scheduler.complete(ticket, Completion("live", self.handle, "UNKNOWN"))
        self.assertEqual((self.budget.pending_count,
                          self.budget.spent_global,
                          self.budget.remaining_global), (1, 0, 0))
        refused = self.scheduler.submit(self.request("retry"))
        self.assertIsInstance(refused, Receipt)
        self.assertEqual((refused.status, refused.reason),
                         ("REFUSED", "QUARANTINED"))
        self.assertEqual(len(self.events), 1)
        self.scheduler.reconcile(self.handle, acknowledged=True)
        self.assertEqual((self.budget.pending_count,
                          self.budget.spent_global,
                          self.budget.remaining_global), (0, 2, 0))
        self.assertEqual([e["consumed"] for e in self.events], [None, True])
        self.assertEqual(self.dispatch.sends, ["live"])

    def test_prerequisite_gate_absence_blocks_admission(self):
        self.dispatch.prerequisite_gates["writer_fence"] = False
        result = self.scheduler.submit(self.request())
        self.assertIsInstance(result, Receipt)
        self.assertEqual(result.status, "AUSENTE")
        self.assertEqual((len(self.events), len(self.dispatch.sends)), (0, 0))

    def test_scheduler_budget_exhaustion_does_not_dispatch(self):
        self.budget = BudgetGovernor(1, 1, service_cap_ns=10,
                                     clock=self.clock, telemetry=self.events)
        self.scheduler.budget = self.budget
        with self.assertRaisesRegex(BudgetExhausted,
                                    "BUDGET_EXHAUSTED: global credits"):
            self.scheduler.submit(self.request())
        self.assertEqual((self.budget.pending_count, len(self.events),
                          len(self.dispatch.sends)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
