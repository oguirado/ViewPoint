"""Offline public-path pins for WindowScheduler; dependency doubles only.

The scheduler, writer fence, and completion state machine are production code.
All fixture metadata is in RAM. Network, process, and file I/O raise during
each test; barrier waits coordinate threads without making speed assertions.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from wender.act.window_scheduler import (
    ActionRequest, ActorLease, Completion, FenceRefused, FocusLease, FrameRef,
    Handle, InvalidRequest, Observation, Receipt, TargetRecord, Ticket, WindowFrames,
    WindowScheduler, WriterFence, check_lock_order,
)


def key(handle: object) -> tuple[str, str, int]:
    return (handle.kind, handle.canonical_id, handle.generation)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, value: int) -> None:
        self.now += value


class MetadataProbe:
    """Assert public dependency calls do not inherit scheduler metadata locks."""

    def __init__(self, fence: WriterFence) -> None:
        self.fence = fence
        self.calls: list[str] = []

    def __call__(self, operation: str) -> None:
        lock = self.fence.metadata_lock
        if lock._is_owned():
            raise AssertionError(operation + " called under metadata lock")
        self.calls.append(operation)


class FakeRegistry:
    def __init__(self, probe: MetadataProbe) -> None:
        self.probe = probe
        self.records: dict[tuple[str, str, int], TargetRecord] = {}
        self.resources: dict[tuple[str, str, int], str] = {}

    def add(self, handle: Handle, *, epoch: int = 0,
            resource: str | None = None, closed: bool = False) -> None:
        self.records[key(handle)] = TargetRecord(
            handle, object(), True, epoch, "sess-" + handle.canonical_id, True, closed)
        self.resources[key(handle)] = resource or (
            handle.kind + ":" + handle.canonical_id)

    def get(self, handle: Handle) -> TargetRecord | None:
        self.probe("registry.get")
        return self.records.get(key(handle))

    def canonical_resource(self, handle: Handle) -> str:
        self.probe("registry.canonical_resource")
        return self.resources[key(handle)]


class FakeLocks:
    def __init__(self, registry: FakeRegistry, probe: MetadataProbe) -> None:
        self.registry, self.probe = registry, probe
        self.held: dict[str, ActorLease] = {}
        self.quarantined: list[ActorLease] = []
        self.released: list[ActorLease] = []
        self.busy: set[str] = set()
        self.calls = 0
        self.attempts: list[tuple[tuple[str, str, int], str]] = []
        self.on_acquire = None
        self.on_release = None
        self._mutex = threading.Lock()

    def try_acquire(self, handle: Handle, *, owner: str,
                    expires_ns: int) -> ActorLease | None:
        self.probe("locks.try_acquire")
        resource = self.registry.canonical_resource(handle)
        self.attempts.append((key(handle), owner))
        if self.on_acquire is not None:
            self.on_acquire(handle, owner)
        with self._mutex:
            self.calls += 1
            if resource in self.held or resource in self.busy:
                return None
            lease = ActorLease(handle, owner, handle.generation,
                               self.calls.to_bytes(8, "big"), expires_ns)
            self.held[resource] = lease
            return lease

    def release(self, lease: ActorLease) -> None:
        self.probe("locks.release")
        if self.on_release is not None:
            self.on_release(lease)
        with self._mutex:
            # The lease owns its binding even after registry retirement.
            resource = next((resource for resource, held in self.held.items()
                             if held == lease), None)
            if resource is None:
                raise AssertionError("duplicate or foreign actor release")
            del self.held[resource]
            self.released.append(lease)

    def quarantine(self, lease: ActorLease) -> None:
        self.probe("locks.quarantine")
        with self._mutex:
            if lease not in self.quarantined:
                self.quarantined.append(lease)


class FakeFocus:
    def __init__(self, registry: FakeRegistry, probe: MetadataProbe) -> None:
        self.registry, self.probe = registry, probe
        self.domains: dict[tuple[str, str, int], str] = {}
        self.held: dict[str, FocusLease] = {}
        self.quarantined: list[FocusLease] = []
        self.released: list[FocusLease] = []
        self._mutex = threading.Lock()

    def try_acquire(self, lease: ActorLease, *, epoch: int) -> FocusLease | None:
        self.probe("focus.try_acquire")
        domain = self.domains.get(key(lease.handle),
                                  self.registry.canonical_resource(lease.handle))
        with self._mutex:
            if domain in self.held:
                return None
            focus = FocusLease(lease, domain, epoch)
            self.held[domain] = focus
            return focus

    def release(self, lease: FocusLease) -> None:
        self.probe("focus.release")
        with self._mutex:
            if self.held.get(lease.domain) != lease:
                raise AssertionError("duplicate or foreign focus release")
            del self.held[lease.domain]
            self.released.append(lease)

    def quarantine(self, lease: FocusLease) -> None:
        self.probe("focus.quarantine")
        with self._mutex:
            if lease not in self.quarantined:
                self.quarantined.append(lease)


class FakeBudget:
    def __init__(self, probe: MetadataProbe, *, global_limit: int = 10000,
                 target_limit: int = 10000) -> None:
        self.probe = probe
        self.global_limit, self.target_limit = global_limit, target_limit
        self.tokens: dict[object, dict] = {}
        self.settlements: list[tuple[object, bool]] = []
        self.on_reserve = None
        self._mutex = threading.Lock()

    def reserve(self, handle: Handle, cost: int, *, action_id: str,
                deadline_ns: int) -> object | None:
        self.probe("budget.reserve")
        if self.on_reserve is not None:
            self.on_reserve()
        with self._mutex:
            charged = [r for r in self.tokens.values()
                       if r["settled"] is not False]
            total = sum(r["cost"] for r in charged)
            target = sum(r["cost"] for r in charged
                         if key(r["handle"]) == key(handle))
            if total + cost > self.global_limit or target + cost > self.target_limit:
                return None
            token = object()
            self.tokens[token] = dict(handle=handle, cost=cost, action_id=action_id,
                                      deadline_ns=deadline_ns, settled=None)
            return token

    def settle(self, token: object, *, consumed: bool) -> None:
        self.probe("budget.settle")
        with self._mutex:
            record = self.tokens[token]
            if record["settled"] is not None:
                raise AssertionError("reservation settled twice")
            record["settled"] = consumed
            self.settlements.append((token, consumed))


class FakeChain:
    def __init__(self, probe: MetadataProbe) -> None:
        self.probe = probe
        self.unsettled: set[tuple[str, str, int]] = set()
        self.on_check = None
        self.calls: list[tuple[tuple[str, str, int], int]] = []

    def is_settled(self, handle: Handle, epoch: int) -> bool:
        self.probe("chain.is_settled")
        self.calls.append((key(handle), epoch))
        if self.on_check is not None:
            self.on_check()
        return key(handle) not in self.unsettled


class FakeDispatch:
    offline = True

    def __init__(self, fence: WriterFence, probe: MetadataProbe) -> None:
        self.writer_fence = fence
        self.probe = probe
        self.prerequisite_gates = {
            "identity_streams": True, "ownership_perception": True,
            "readiness": True, "writer_fence": True,
        }
        self.requests: list[ActionRequest] = []
        self.contexts: list[object] = []
        self.sends: list[str] = []
        self.behavior = None

    def __call__(self, request: ActionRequest, context: object) -> Completion | None:
        self.probe("dispatch")
        self.requests.append(request)
        self.contexts.append(context)
        if self.behavior is not None:
            return self.behavior(request, context)
        context.send(self.sends.append, request.action_id)
        return Completion(request.action_id, request.handle, "SUCCEEDED", True)


class WindowSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        forbidden = AssertionError("forbidden I/O in offline scheduler test")
        for target in ("builtins.open", "pathlib.Path.open", "socket.socket",
                       "socket.create_connection", "subprocess.Popen", "os.system",
                       "numpy.save", "numpy.savez", "numpy.memmap"):
            self.enterContext(patch(target, side_effect=forbidden))
        self.clock = FakeClock()
        self.fence = WriterFence(clock=self.clock)
        self.probe = MetadataProbe(self.fence)
        self.registry = FakeRegistry(self.probe)
        self.locks = FakeLocks(self.registry, self.probe)
        self.focus = FakeFocus(self.registry, self.probe)
        self.budget = FakeBudget(self.probe)
        self.chain = FakeChain(self.probe)
        self.dispatch = FakeDispatch(self.fence, self.probe)
        self.scheduler = self.make_scheduler()

    def make_scheduler(self, **changes: object) -> WindowScheduler:
        args = dict(registry=self.registry, locks=self.locks,
                    focus_policy=self.focus, budget=self.budget, chain=self.chain,
                    writer_fence=self.fence, dispatch=self.dispatch, clock=self.clock,
                    max_queue=100, max_targets=4, service_cap_ns=10)
        args.update(changes)
        return WindowScheduler(**args)

    def handle(self, name: str, *, kind: str = "cdp_tab", generation: int = 1,
               epoch: int = 0, resource: str | None = None) -> Handle:
        handle = Handle(kind, name, generation)
        self.registry.add(handle, epoch=epoch, resource=resource)
        return handle

    def request(self, handle: Handle, action: str = "job", *, cost: int = 1,
                deadline: int = 10000, epoch: int = 0,
                payload: dict | None = None) -> ActionRequest:
        return ActionRequest(handle, action, epoch, payload or {}, cost, deadline)

    def accepted(self, request: ActionRequest,
                 scheduler: WindowScheduler | None = None) -> Ticket:
        result = (scheduler or self.scheduler).submit(request)
        self.assertIsInstance(result, Ticket, repr(result))
        self.assertEqual(result.action_id, request.action_id)
        self.assertEqual(key(result.handle), key(request.handle))
        return result

    def rejected(self, request: ActionRequest, *, status: str | None = None,
                 scheduler: WindowScheduler | None = None) -> Receipt:
        result = (scheduler or self.scheduler).submit(request)
        self.assertIsInstance(result, Receipt, repr(result))
        self.assertNotEqual(result.status, "SUCCEEDED")
        if status is not None:
            self.assertEqual(result.status, status)
        return result

    def run_steps(self, count: int) -> list[Receipt]:
        result: list[Receipt] = []
        for _ in range(count):
            result.extend(self.scheduler.step(self.clock()))
        return result

    def test_service_cap_is_required_and_deadline_must_be_feasible(self) -> None:
        handle = self.handle("a")
        self.rejected(self.request(handle), status="CONFIG_REQUIRED",
                      scheduler=self.make_scheduler(service_cap_ns=None))
        self.rejected(self.request(handle, deadline=1))
        self.assertEqual(self.budget.tokens, {})
        self.assertEqual(self.dispatch.requests, [])

    def test_queue_population_and_both_credit_limits(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        small = self.make_scheduler(max_queue=1, max_targets=1)
        self.accepted(self.request(a, "a"), small)
        self.rejected(self.request(a, "a2"), scheduler=small)
        self.rejected(self.request(b, "b"), scheduler=small)
        self.assertEqual(len(self.budget.tokens), 1)
        self.budget.global_limit = 1
        self.rejected(self.request(b, "global"))
        self.budget.global_limit = 10000
        self.budget.target_limit = 1
        self.rejected(self.request(a, "target"))
        self.accepted(self.request(b, "healthy"))

    def test_invalid_incarnation_closed_record_epoch_and_kind_refuse(self) -> None:
        a = self.handle("a", epoch=2, generation=2**80)
        self.rejected(self.request(Handle("cdp_tab", "a", 1), "stale"))
        self.rejected(self.request(a, "epoch", epoch=1))
        closed = self.handle("closed")
        self.registry.add(closed, closed=True)
        self.rejected(self.request(closed, "closed"))
        unsupported = SimpleNamespace(kind="invented", canonical_id="no", generation=1)
        with self.assertRaises(InvalidRequest):
            self.scheduler.submit(SimpleNamespace(handle=unsupported, action_id="kind", epoch=0,
                                                  payload={}, cost=1, deadline_ns=1000))
        self.accepted(self.request(a, "valid", epoch=2))
        self.run_steps(2)
        self.assertEqual(self.dispatch.sends, ["valid"])

    def test_target_population_bound_is_independent_of_queue_capacity(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        scheduler = self.make_scheduler(max_queue=10, max_targets=1)
        self.accepted(self.request(a, "a"), scheduler)
        rejection = self.rejected(self.request(b, "b"), scheduler=scheduler)
        self.assertEqual(rejection.reason, "TARGET_LIMIT")
        self.accepted(self.request(a, "a-next"), scheduler)
        self.assertEqual(len(self.budget.tokens), 2)

    def test_all_supported_kinds_and_structural_handle_values(self) -> None:
        scheduler = self.make_scheduler(max_targets=5)
        for index, kind in enumerate(("cdp_tab", "x11_window", "xvfb_screen",
                                      "capsule", "godot_window")):
            handle = self.handle(str(index), kind=kind)
            foreign = SimpleNamespace(kind=handle.kind,
                                      canonical_id=handle.canonical_id,
                                      generation=handle.generation)
            self.accepted(self.request(foreign, "job-" + kind), scheduler)
        for _ in range(12):
            scheduler.step(self.clock())
        self.assertEqual(set(self.dispatch.sends),
                         {"job-" + kind for kind in ("cdp_tab", "x11_window",
                          "xvfb_screen", "capsule", "godot_window")})

    def test_missing_prerequisite_gate_is_explicit_absence(self) -> None:
        handle = self.handle("a")
        del self.dispatch.prerequisite_gates["readiness"]
        self.rejected(self.request(handle), status="AUSENTE")
        self.assertEqual(self.dispatch.requests, [])
        self.assertEqual(self.budget.tokens, {})

    def test_dispatch_must_use_exact_shared_writer_fence(self) -> None:
        handle = self.handle("a")
        self.dispatch.writer_fence = WriterFence(clock=self.clock)
        self.rejected(self.request(handle), status="AUSENTE")
        self.assertEqual(self.dispatch.requests, [])

    def test_synchronous_completion_releases_once_and_consumes_budget(self) -> None:
        a = self.handle("a")
        ticket = self.accepted(self.request(a))
        receipts = self.run_steps(2)
        self.assertEqual([r.status for r in receipts if r.action_id == "job"],
                         ["SUCCEEDED"])
        self.assertEqual(self.dispatch.sends, ["job"])
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.assertEqual(len(self.locks.released), 1)
        self.assertEqual(len(self.focus.released), 1)
        self.scheduler.complete(ticket, Completion("job", a, "SUCCEEDED", True))
        self.scheduler.reconcile(a, acknowledged=True)
        self.assertEqual(len(self.budget.settlements), 1)
        self.assertEqual(len(self.locks.released), 1)

    def test_contended_step_dispatches_once_and_independent_target_progresses(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        self.accepted(self.request(a, "a"))
        entered, release = threading.Event(), threading.Event()
        errors: list[BaseException] = []

        def dispatch(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            if request.action_id == "a":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release dispatch barrier")
            return Completion(request.action_id, request.handle, "SUCCEEDED", True)

        def step():
            try:
                self.scheduler.step(self.clock())
            except BaseException as error:
                errors.append(error)

        self.dispatch.behavior = dispatch
        worker = threading.Thread(target=step)
        worker.start()
        try:
            self.assertTrue(entered.wait(3), "first dispatch did not reach barrier")
            self.accepted(self.request(a, "a-next"))
            self.accepted(self.request(b, "b"))
            contenders = [threading.Thread(target=step) for _ in range(4)]
            for contender in contenders:
                contender.start()
            for contender in contenders:
                contender.join(3)
                self.assertFalse(contender.is_alive(), "metadata lock spans callback")
            self.assertEqual(self.dispatch.sends.count("a"), 1)
            self.assertNotIn("a-next", self.dispatch.sends)
            self.assertIn("b", self.dispatch.sends)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.run_steps(8)
        self.assertEqual(self.dispatch.sends.count("a-next"), 1)

    def test_two_schedulers_contend_for_one_canonical_claim(self) -> None:
        a = self.handle("a")
        second = self.make_scheduler()
        self.accepted(self.request(a, "one"))
        self.accepted(self.request(a, "two"), second)
        self.dispatch.behavior = lambda request, ctx: (
            ctx.send(self.dispatch.sends.append, request.action_id) and None)
        self.scheduler.step(self.clock())
        second.step(self.clock())
        self.assertEqual(len(self.dispatch.sends), 1)
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(self.budget.settlements, [])

    def test_claim_is_atomic_before_actor_provider_returns(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        self.accepted(self.request(a, "a"))
        self.accepted(self.request(a, "a-next"))
        entered, release = threading.Event(), threading.Event()
        errors: list[BaseException] = []

        def actor_barrier(handle, owner):
            if owner == "a":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release actor barrier")

        def step():
            try:
                self.scheduler.step(self.clock())
            except BaseException as error:
                errors.append(error)

        self.locks.on_acquire = actor_barrier
        worker = threading.Thread(target=step)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.accepted(self.request(b, "b"))
            contenders = [threading.Thread(target=step) for _ in range(3)]
            for contender in contenders:
                contender.start()
            for contender in contenders:
                contender.join(3)
                self.assertFalse(contender.is_alive())
            self.assertNotIn((key(a), "a-next"), self.locks.attempts)
            self.assertNotIn("a-next", self.dispatch.sends)
            self.assertIn("b", self.dispatch.sends)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.run_steps(6)
        self.assertEqual(self.dispatch.sends.count("a"), 1)
        self.assertEqual(self.dispatch.sends.count("a-next"), 1)

    def test_urgent_edf_is_bounded_by_reserved_fair_cursor(self) -> None:
        self.scheduler = self.make_scheduler(max_targets=3)
        urgent, patient, other = [self.handle(name) for name in ("urgent", "patient", "other")]
        for index in range(20):
            self.accepted(self.request(urgent, "u%d" % index, deadline=5000, cost=1))
            self.accepted(self.request(patient, "p%d" % index, deadline=90000, cost=9))
            self.accepted(self.request(other, "o%d" % index, deadline=90000, cost=3))
        self.assertEqual(self.scheduler.starvation_bound, 6)
        self.run_steps(36)
        sent = [key(request.handle) for request in self.dispatch.requests]
        bound = self.scheduler.starvation_bound
        for handle in (urgent, patient, other):
            positions = [i for i, observed in enumerate(sent) if observed == key(handle)]
            self.assertTrue(positions, handle)
            self.assertLess(positions[0], bound)
            self.assertTrue(all(right - left <= bound
                                for left, right in zip(positions, positions[1:])))
            self.assertLess(len(sent) - 1 - positions[-1], bound)
            self.assertGreater(self.scheduler.fair_visits[key(handle)], 0)
        self.assertGreater(self.scheduler.urgent_dispatches, 0)
        self.assertLessEqual(self.scheduler.urgent_dispatches,
                             sum(self.scheduler.fair_visits.values()) + 1)
        self.assertEqual(len(self.dispatch.sends), 36)

    def test_equal_deadlines_keep_submission_order_within_target(self) -> None:
        handle = self.handle("a")
        for action in ("z", "a", "m"):
            self.accepted(self.request(handle, action, deadline=1000))
        self.run_steps(6)
        self.assertEqual(self.dispatch.sends, ["z", "a", "m"])

    def test_readiness_and_ownership_wait_do_not_restart_deadline(self) -> None:
        handle = self.handle("a")
        self.chain.unsettled.add(key(handle))
        self.accepted(self.request(handle, deadline=100))
        self.scheduler.step(0)
        self.clock.advance(50)
        self.chain.unsettled.clear()
        self.locks.busy.add(self.registry.canonical_resource(handle))
        self.scheduler.step(self.clock())
        self.clock.advance(50)
        self.locks.busy.clear()
        receipts = self.scheduler.step(self.clock())
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(len([r for r in receipts if r.action_id == "job"]), 1)
        self.assertNotEqual(receipts[0].status, "SUCCEEDED")
        self.assertEqual([r["deadline_ns"] for r in self.budget.tokens.values()], [100])
        self.assertEqual([used for _, used in self.budget.settlements], [False])
        self.scheduler.stop(handle)
        self.scheduler.reconcile(handle, acknowledged=True)
        self.run_steps(2)
        self.assertEqual(len(self.budget.settlements), 1)
        self.assertGreater(self.scheduler.eligibility_stalls[key(handle)], 0)

    def test_possible_send_timeout_quarantines_without_refund_or_replay(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle, deadline=100))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        self.clock.advance(100)
        receipts = self.scheduler.step(self.clock())
        self.assertEqual([r.status for r in receipts if r.action_id == "job"], ["UNKNOWN"])
        self.assertEqual(self.budget.settlements, [])
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(len(self.focus.held), 1)
        self.assertEqual(len(self.locks.quarantined), 1)
        self.rejected(self.request(handle, "another", deadline=10000))
        self.scheduler.reconcile(handle)
        self.assertEqual(self.budget.settlements, [])
        self.assertEqual(len(self.locks.held), 1)
        self.scheduler.complete(ticket, Completion("job", handle, "SUCCEEDED", False))
        self.assertEqual(len(self.locks.held), 1)
        self.scheduler.reconcile(handle, acknowledged=True)
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.assertEqual(self.locks.held, {})
        self.assertEqual(self.focus.held, {})
        self.assertEqual(self.dispatch.sends, ["job"])

    def test_late_positive_acknowledgment_releases_quarantined_claim_once(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle, deadline=100))
        self.dispatch.behavior = lambda request, ctx: (
            ctx.send(self.dispatch.sends.append, request.action_id) and None)
        self.scheduler.step(0)
        self.clock.advance(100)
        self.scheduler.step(self.clock())
        self.scheduler.complete(ticket, Completion("job", handle, "FAILED", True))
        self.scheduler.complete(ticket, Completion("job", handle, "FAILED", True))
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.assertEqual(len(self.locks.released), 1)
        self.assertEqual(len(self.focus.released), 1)

    def test_unacknowledged_success_is_not_terminal_ownership_evidence(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle))

        def unacknowledged(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return Completion(request.action_id, request.handle, "SUCCEEDED", False)

        self.dispatch.behavior = unacknowledged
        self.scheduler.step(0)
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.complete(ticket, Completion("job", handle, "SUCCEEDED", True))
        self.assertEqual(len(self.locks.released), 1)

    def test_stop_between_dispatch_authorization_and_first_send_refunds(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle))

        def stopped(request, context):
            self.scheduler.stop(handle)
            self.assertFalse(context.check())
            with self.assertRaises(FenceRefused):
                context.send(self.dispatch.sends.append, "forbidden")
            return None

        self.dispatch.behavior = stopped
        self.scheduler.step(0)
        self.run_steps(2)
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual([used for _, used in self.budget.settlements], [False])
        self.assertEqual(self.locks.held, {})

    def test_stop_fences_cleanup_and_healthy_nonconflicting_target_progresses(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        self.accepted(self.request(a, "a"))
        self.accepted(self.request(b, "b"))

        def stopped(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            if request.action_id == "a":
                self.scheduler.stop(a)
                self.assertFalse(self.fence.check(a, ticket=context.ticket))
                with self.assertRaises(FenceRefused):
                    context.send(self.dispatch.sends.append, "cleanup")
            return Completion(request.action_id, request.handle, "SUCCEEDED", True)

        self.dispatch.behavior = stopped
        self.run_steps(6)
        self.assertEqual(self.dispatch.sends, ["a", "b"])
        self.rejected(self.request(a, "later"))
        self.assertFalse(self.dispatch.contexts[0].check())
        self.assertTrue(all(consumed for _, consumed in self.budget.settlements))

    def test_stop_during_readiness_callback_is_linearized_before_send(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle))
        self.chain.on_check = lambda: self.scheduler.stop(handle)
        self.run_steps(3)
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual([used for _, used in self.budget.settlements], [False])
        self.assertEqual(self.locks.held, {})

    def test_focus_domain_serializes_while_independent_domain_progresses(self) -> None:
        a, b, c = [self.handle(name) for name in ("a", "b", "c")]
        self.focus.domains[key(a)] = self.focus.domains[key(b)] = "seat-0"
        self.focus.domains[key(c)] = "seat-1"
        ta = self.accepted(self.request(a, "a"))
        self.accepted(self.request(b, "b"))
        self.accepted(self.request(c, "c"))

        def pending_a(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            if request.action_id != "a":
                return Completion(request.action_id, request.handle, "SUCCEEDED", True)
            return None

        self.dispatch.behavior = pending_a
        self.run_steps(10)
        self.assertIn("a", self.dispatch.sends)
        self.assertIn("c", self.dispatch.sends)
        self.assertNotIn("b", self.dispatch.sends)
        self.scheduler.complete(ta, Completion("a", a, "SUCCEEDED", True))
        self.run_steps(6)
        self.assertEqual(self.dispatch.sends.count("b"), 1)

    def test_capsule_and_child_screen_share_actuation_and_stop_fence(self) -> None:
        capsule = self.handle("capsule-1", kind="capsule", resource="screen:42")
        screen = self.handle("screen-42", kind="xvfb_screen", resource="screen:42")
        self.accepted(self.request(capsule, "capsule"))
        self.accepted(self.request(screen, "screen"))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.run_steps(5)
        self.assertEqual(self.dispatch.sends, ["capsule"])
        self.scheduler.stop(screen)
        self.assertFalse(self.dispatch.contexts[0].check())
        self.assertFalse(self.fence.check(capsule))
        self.run_steps(5)
        self.assertEqual(self.dispatch.sends, ["capsule"])

    def test_context_deadline_and_remaining_budget_are_absolute(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle, deadline=100))

        def remaining(request, context):
            self.assertEqual(context.deadline_ns, 100)
            self.assertEqual(context.remaining_ns(), 100)
            self.clock.advance(90)
            self.assertEqual(context.remaining_ns(), 10)
            self.clock.advance(10)
            self.assertEqual(context.remaining_ns(), 0)
            with self.assertRaises(FenceRefused):
                context.send(self.dispatch.sends.append, "late")
            return None

        self.dispatch.behavior = remaining
        self.scheduler.step(0)
        self.scheduler.step(self.clock())
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual([used for _, used in self.budget.settlements], [False])

    def test_observation_identity_epoch_sequence_and_owned_rgb_snapshot(self) -> None:
        handle = self.handle("a")
        rgb = np.arange(12, dtype=np.uint8).reshape(2, 2, 3).copy()
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        frame = FrameRef(handle, 0, 4, 2, 2, rgb, digest)
        observation = Observation(handle, 0, 4, 1000000, {}, frame)
        self.accepted(self.request(handle, payload={"observation": observation}))
        rgb[:] = 0
        self.run_steps(2)
        copied = self.dispatch.requests[0].payload["observation"].frame.rgb
        self.assertEqual(hashlib.sha256(copied.tobytes()).hexdigest(), digest)
        self.assertFalse(copied.flags.writeable)
        self.assertTrue(copied.flags.c_contiguous)
        self.assertTrue(copied.flags.owndata)
        self.assertEqual(self.dispatch.sends, ["job"])

    def test_bad_frame_evidence_is_refused_without_budget_or_dispatch(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        rgb = np.ones((2, 2, 3), dtype=np.uint8)
        digest = hashlib.sha256(rgb.tobytes()).hexdigest()
        good = FrameRef(a, 0, 4, 2, 2, rgb, digest)
        # Structural inputs bypass eager record validation to exercise submit's gate.
        def observation(handle=a, epoch=0, frame_seq=4, dpr_ppm=1000000, frame=good):
            return SimpleNamespace(handle=handle, epoch=epoch, frame_seq=frame_seq,
                                   dpr_ppm=dpr_ppm, state={}, frame=frame)

        bad_observations = [
            observation(handle=b),
            observation(epoch=1),
            observation(frame_seq=5),
            observation(dpr_ppm=0),
            observation(frame=SimpleNamespace(**{**vars(good), "digest": "0" * 64})),
        ]
        for index, observation in enumerate(bad_observations):
            with self.subTest(index=index):
                with self.assertRaises(InvalidRequest):
                    self.scheduler.submit(self.request(a, "bad%d" % index,
                                          payload={"observation": observation}))
        self.assertEqual(self.budget.tokens, {})
        self.assertEqual(self.dispatch.sends, [])

    def test_window_frame_sequences_are_local_ordered_and_length_matched(self) -> None:
        handle = self.handle("a")
        rgb = np.ones((2, 2, 3), dtype=np.uint8)
        frames = (rgb, rgb.copy())
        for index, sequences in enumerate(((2, 1), (1, 1), (1,))):
            values = SimpleNamespace(handle=handle, epoch=0, frames=frames,
                                     frame_seqs=sequences, dpr_ppm=1000000)
            with self.assertRaises(InvalidRequest):
                self.scheduler.submit(self.request(handle, "bad%d" % index,
                                      payload={"window_frames": values}))
        values = WindowFrames(handle, 0, frames, (1, 2), 1000000)
        self.accepted(self.request(handle, "good", payload={"window_frames": values}))
        self.run_steps(2)
        self.assertEqual(self.dispatch.sends, ["good"])

    def test_lock_order_rejects_rank_and_same_rank_canonical_inversion(self) -> None:
        check_lock_order([0, 1, 2, 3, 4, 5, 6])
        with self.assertRaises(ValueError):
            check_lock_order([0, 2, 1])
        check_lock_order([(3, "a"), (3, "b")])
        with self.assertRaises(ValueError):
            check_lock_order([(3, "b"), (3, "a")])

    def test_global_stop_refuses_future_admission_and_refunds_queued_once(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        self.accepted(self.request(a, "a"))
        self.accepted(self.request(b, "b"))
        self.scheduler.stop()
        self.scheduler.stop()
        self.run_steps(3)
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual([used for _, used in self.budget.settlements], [False, False])
        self.rejected(self.request(a, "future"))
        self.assertFalse(self.fence.check(a))
        self.assertFalse(self.fence.check(b))

    def test_stop_inside_budget_reservation_refunds_without_authorization(self) -> None:
        handle = self.handle("a")
        self.budget.on_reserve = lambda: self.scheduler.stop(handle)
        self.rejected(self.request(handle))
        self.run_steps(3)
        self.assertEqual(len(self.budget.tokens), 1)
        self.assertEqual([used for _, used in self.budget.settlements], [False])
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.locks.held, {})

    def test_forged_completion_cannot_release_an_incumbent_claim(self) -> None:
        a, b = self.handle("a"), self.handle("b")
        ticket = self.accepted(self.request(a, "a"))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        with self.assertRaises(InvalidRequest):
            self.scheduler.complete(ticket, Completion("a", b, "SUCCEEDED", True))
        with self.assertRaises(InvalidRequest):
            self.scheduler.complete(Ticket("forged", a),
                                    Completion("forged", a, "SUCCEEDED", True))
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.complete(ticket, Completion("a", a, "SUCCEEDED", True))
        self.assertEqual(self.locks.held, {})

    def test_exception_after_send_stays_unknown_and_retains_claim(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle))

        def broken(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            raise RuntimeError("offline transport lost acknowledgment")

        self.dispatch.behavior = broken
        receipts = self.scheduler.step(0)
        self.assertEqual([r.status for r in receipts], ["UNKNOWN"])
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(self.budget.settlements, [])
        self.assertFalse(self.fence.check(handle))
        self.scheduler.reconcile(handle)
        self.assertEqual(len(self.locks.held), 1)
        self.scheduler.reconcile(handle, acknowledged=True)
        self.assertEqual([used for _, used in self.budget.settlements], [True])

    def test_prerequisite_removed_after_admission_blocks_dispatch(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle))
        self.dispatch.prerequisite_gates["readiness"] = False
        receipts = self.run_steps(3)
        self.assertEqual([r.status for r in receipts], ["AUSENTE"])
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual([used for _, used in self.budget.settlements], [False])

    def test_truthy_readiness_is_not_positive_settled_evidence(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle, deadline=100))
        self.chain.is_settled = lambda handle, epoch: 1
        self.scheduler.step(0)
        self.assertEqual(self.dispatch.sends, [])
        self.clock.advance(100)
        receipts = self.scheduler.step(self.clock())
        self.assertEqual(len(receipts), 1)
        self.assertNotEqual(receipts[0].status, "SUCCEEDED")
        self.assertEqual([used for _, used in self.budget.settlements], [False])

    def test_epoch_change_during_preparation_refuses_before_send(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle, epoch=0))
        self.chain.on_check = lambda: self.registry.add(handle, epoch=1)
        receipts = self.run_steps(3)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].reason, "STALE_IDENTITY_OR_EPOCH")
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.locks.held, {})
        self.assertEqual([used for _, used in self.budget.settlements], [False])

    def test_completion_during_unsent_retry_release_cannot_claim_success(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle))
        self.focus.try_acquire = lambda lease, epoch: None
        rejected: list[bool] = []

        def attempt_completion(lease):
            try:
                self.scheduler.complete(ticket, Completion("job", handle, "SUCCEEDED", True))
            except InvalidRequest:
                rejected.append(True)
            else:
                rejected.append(False)

        self.locks.on_release = attempt_completion
        receipts = self.scheduler.step(0)
        self.assertEqual(rejected, [True])
        self.assertFalse(any(receipt.status == "SUCCEEDED" for receipt in receipts))
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.stop(handle)
        self.assertEqual([used for _, used in self.budget.settlements], [False])

    def test_stop_and_reconcile_use_retained_binding_after_registry_retirement(self) -> None:
        handle = self.handle("a")
        self.accepted(self.request(handle))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        del self.registry.records[key(handle)]
        del self.registry.resources[key(handle)]
        self.scheduler.stop(handle)
        self.assertFalse(self.dispatch.contexts[0].check())
        self.assertEqual(len(self.locks.held), 1)
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.reconcile(handle, acknowledged=True)
        self.assertEqual(self.locks.held, {})
        self.assertEqual(self.focus.held, {})
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.assertFalse(self.fence.check(handle))

    def test_shared_fence_time_blocks_late_success_with_an_older_scheduler_clock(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle, deadline=100))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        later_clock = FakeClock()
        later_clock.advance(100)
        other_scheduler = self.make_scheduler(clock=later_clock)
        other_scheduler.step(100)
        self.assertEqual(self.clock(), 0)
        self.scheduler.complete(ticket, Completion("job", handle, "SUCCEEDED", True))
        receipts = self.scheduler.step(0)
        self.assertEqual([receipt.status for receipt in receipts], ["UNKNOWN"])
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.assertEqual(self.locks.held, {})
        self.assertFalse(self.dispatch.contexts[0].check())

    def test_budget_reservation_time_must_preserve_feasible_remaining_deadline(self) -> None:
        handle = self.handle("a")
        # Initially 100ns >= 2 * 4 targets * 10ns cap; after reserve, 79ns is not.
        self.budget.on_reserve = lambda: self.clock.advance(21)
        receipt = self.rejected(self.request(handle, deadline=100))
        self.assertEqual(receipt.reason, "INFEASIBLE_DEADLINE")
        self.assertLess(self.clock(), 100)
        self.assertEqual([used for _, used in self.budget.settlements], [False])
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.locks.held, {})

    def test_epoch_publication_revokes_old_context_without_permanent_stop(self) -> None:
        handle = self.handle("a")
        ticket = self.accepted(self.request(handle, "old", epoch=0))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        old_context = self.dispatch.contexts[0]
        self.fence.publish_epoch(handle, 1)
        self.registry.add(handle, epoch=1)
        self.assertFalse(old_context.check())
        receipts = self.scheduler.step(0)
        self.assertEqual([receipt.status for receipt in receipts], ["UNKNOWN"])
        self.assertEqual(self.budget.settlements, [])
        self.assertEqual(len(self.locks.held), 1)
        self.scheduler.complete(ticket, Completion("old", handle, "SUCCEEDED", True))
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.dispatch.behavior = None
        self.accepted(self.request(handle, "new", epoch=1))
        self.run_steps(3)
        self.assertEqual(self.dispatch.sends, ["old", "new"])
        self.assertFalse(old_context.check())

    def test_invalidated_incarnation_can_yield_to_new_generation_after_reconciliation(self) -> None:
        old = self.handle("window", generation=1, resource="physical-window")
        self.accepted(self.request(old, "old"))

        def pending(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            return None

        self.dispatch.behavior = pending
        self.scheduler.step(0)
        old_context = self.dispatch.contexts[0]
        self.fence.invalidate(old)
        del self.registry.records[key(old)]
        del self.registry.resources[key(old)]
        new = self.handle("window", generation=2, resource="physical-window")
        self.assertFalse(old_context.check())
        receipts = self.scheduler.step(0)
        self.assertEqual([receipt.status for receipt in receipts], ["UNKNOWN"])
        self.rejected(self.request(new, "too-early"))
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.reconcile(old, acknowledged=True)
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        self.dispatch.behavior = None
        self.accepted(self.request(new, "new"))
        self.run_steps(3)
        self.assertEqual(self.dispatch.sends, ["old", "new"])
        self.assertFalse(old_context.check())

    def test_shared_fence_claim_spans_unlocked_terminal_budget_accounting(self) -> None:
        handle = self.handle("a")
        other_scheduler = self.make_scheduler()
        first = self.accepted(self.request(handle, "first"))
        self.accepted(self.request(handle, "second"), other_scheduler)

        def dispatch(request, context):
            context.send(self.dispatch.sends.append, request.action_id)
            if request.action_id == "first":
                return None
            return Completion(request.action_id, request.handle, "SUCCEEDED", True)

        self.dispatch.behavior = dispatch
        self.scheduler.step(0)
        self.assertEqual(self.dispatch.sends, ["first"])
        during_settlement: list[tuple[str, ...]] = []
        settle = self.budget.settle

        def reentrant_settle(token, *, consumed):
            self.probe("budget.settle.reentrant")
            if self.budget.tokens[token]["action_id"] == "first":
                other_scheduler.step(0)
                during_settlement.append(tuple(self.dispatch.sends))
            settle(token, consumed=consumed)

        self.budget.settle = reentrant_settle
        self.scheduler.complete(first, Completion("first", handle, "SUCCEEDED", True))
        self.assertEqual(during_settlement, [("first",)])
        self.assertEqual(self.dispatch.sends, ["first"])
        self.assertEqual([used for _, used in self.budget.settlements], [True])
        for _ in range(3):
            other_scheduler.step(0)
        self.assertEqual(self.dispatch.sends, ["first", "second"])
        self.assertEqual([used for _, used in self.budget.settlements], [True, True])
        self.assertEqual(self.locks.held, {})
        self.assertEqual(self.focus.held, {})

    def test_foreign_actor_capability_is_never_released_or_quarantined(self) -> None:
        requested, incumbent, healthy = [self.handle(name)
                                         for name in ("requested", "incumbent", "healthy")]
        foreign = self.locks.try_acquire(incumbent, owner="incumbent-owner", expires_ns=10000)
        original_acquire = self.locks.try_acquire

        def corrupt_actor(handle, *, owner, expires_ns):
            self.probe("locks.corrupt_actor")
            if key(handle) == key(requested):
                return foreign
            return original_acquire(handle, owner=owner, expires_ns=expires_ns)

        self.locks.try_acquire = corrupt_actor
        self.accepted(self.request(requested, "requested"))
        receipts = self.scheduler.step(0)
        self.assertEqual([receipt.reason for receipt in receipts], ["INVALID_ACTOR_LEASE"])
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.locks.held,
                         {self.registry.canonical_resource(incumbent): foreign})
        self.assertEqual(self.locks.released, [])
        self.assertEqual(self.locks.quarantined, [])
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.reconcile(requested)
        self.assertEqual(self.budget.settlements, [])
        self.accepted(self.request(healthy, "healthy"))
        self.run_steps(3)
        self.assertEqual(self.dispatch.sends, ["healthy"])
        self.assertNotIn(foreign, self.locks.released)
        self.assertNotIn(foreign, self.locks.quarantined)
        self.scheduler.reconcile(requested, acknowledged=True)
        self.assertEqual(self.locks.held,
                         {self.registry.canonical_resource(incumbent): foreign})
        self.assertNotIn(foreign, self.locks.released)
        self.assertEqual([used for _, used in self.budget.settlements], [True, False])

    def test_foreign_focus_capability_does_not_release_the_incumbent(self) -> None:
        requested, incumbent = self.handle("requested"), self.handle("incumbent")
        incumbent_actor = self.locks.try_acquire(
            incumbent, owner="incumbent-owner", expires_ns=10000)
        foreign_focus = self.focus.try_acquire(incumbent_actor, epoch=0)
        original_acquire = self.focus.try_acquire

        def corrupt_focus(lease, *, epoch):
            self.probe("focus.corrupt_focus")
            if key(lease.handle) == key(requested):
                return foreign_focus
            return original_acquire(lease, epoch=epoch)

        self.focus.try_acquire = corrupt_focus
        self.accepted(self.request(requested, "requested"))
        receipts = self.scheduler.step(0)
        self.assertEqual([receipt.reason for receipt in receipts], ["INVALID_FOCUS_LEASE"])
        self.assertEqual(self.dispatch.sends, [])
        self.assertEqual(self.focus.held, {foreign_focus.domain: foreign_focus})
        self.assertEqual(self.focus.released, [])
        self.assertEqual(self.focus.quarantined, [])
        self.assertEqual(self.locks.released, [])
        self.assertNotIn(incumbent_actor, self.locks.quarantined)
        self.assertEqual(self.budget.settlements, [])
        self.scheduler.reconcile(requested)
        self.assertEqual(self.locks.released, [])
        self.scheduler.reconcile(requested, acknowledged=True)
        self.assertEqual(self.focus.held, {foreign_focus.domain: foreign_focus})
        self.assertEqual(self.focus.released, [])
        self.assertEqual(self.focus.quarantined, [])
        self.assertEqual(self.locks.held,
                         {self.registry.canonical_resource(incumbent): incumbent_actor})
        self.assertEqual(len(self.locks.released), 1)
        self.assertEqual(key(self.locks.released[0].handle), key(requested))
        self.assertNotIn(incumbent_actor, self.locks.released)
        self.assertEqual([used for _, used in self.budget.settlements], [False])


if __name__ == "__main__":
    unittest.main()
