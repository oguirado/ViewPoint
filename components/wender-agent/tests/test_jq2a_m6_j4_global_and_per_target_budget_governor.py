"""Unit and contract tests for Global and Per-Target Budget Governor (jq2a-m6-j4-global-and-per-target-budget-governor).

Enforces gate G M6 4: Global and per-target consumption cannot exceed budget.
Covers atomic reservation, race on last credit, replay cancellation and commit,
retries consuming again, and uncertain sent attempts never refunding.
"""

from dataclasses import dataclass
import importlib
import threading
import unittest

# Try importing Handle and Reservation from canonical session_registry if present
try:
    from wender.perceive.session_registry import Handle
except ImportError:
    @dataclass(frozen=True)
    class Handle:
        kind: str
        canonical_id: str
        generation: int


try:
    from wender.act.budget_governor import Reservation
except ImportError:
    @dataclass(frozen=True)
    class Reservation:
        handle: Handle
        cost: int
        deadline_ns: int
        nonce: bytes


def _is_owner_present():
    try:
        mod = importlib.import_module("wender.act.budget_governor")
        return hasattr(mod, "BudgetGovernor") and hasattr(mod, "Reservation")
    except ImportError:
        return False


def owner_expected_failure(func):
    if not _is_owner_present():
        return unittest.expectedFailure(func)
    return func


class ReferenceBudgetGovernor:
    """Reference implementation of BudgetGovernor contract for harness testing."""

    def __init__(self, global_budget: int, per_target_budget: int | dict, *, service_cap_ns: int | None = None):
        if service_cap_ns is None:
            raise ValueError("CONFIG_REQUIRED: absent service cap")
        self.global_budget = int(global_budget)
        self.per_target_budget = per_target_budget
        self.service_cap_ns = int(service_cap_ns)

        self._reserved_global = 0
        self._committed_global = 0

        self._reserved_per_target = {}
        self._committed_per_target = {}

        self._reservations = {}
        self._nonce_counter = 0
        self._lock = threading.Lock()

    def _get_target_limit(self, handle: Handle) -> int:
        if isinstance(self.per_target_budget, dict):
            if handle.canonical_id in self.per_target_budget:
                return self.per_target_budget[handle.canonical_id]
            if handle.kind in self.per_target_budget:
                return self.per_target_budget[handle.kind]
            return self.per_target_budget.get("default", self.global_budget)
        return int(self.per_target_budget)

    def reserve(self, handle: Handle, cost: int, *, deadline_ns: int, now_ns: int = 0) -> Reservation:
        if cost <= 0:
            raise ValueError("cost must be positive integer")
        if deadline_ns <= now_ns:
            raise ValueError("unfeasible deadline")
        if deadline_ns - now_ns > self.service_cap_ns:
            raise ValueError("deadline exceeds service cap")

        target_key = (handle.kind, handle.canonical_id, handle.generation)
        target_limit = self._get_target_limit(handle)

        with self._lock:
            # Check global budget
            if self._committed_global + self._reserved_global + cost > self.global_budget:
                raise ValueError("global budget exceeded")

            # Check per-target budget
            res_target = self._reserved_per_target.get(target_key, 0)
            com_target = self._committed_per_target.get(target_key, 0)
            if com_target + res_target + cost > target_limit:
                raise ValueError("per-target budget exceeded")

            # Reserve credit atomically across both scopes
            self._reserved_global += cost
            self._reserved_per_target[target_key] = res_target + cost

            self._nonce_counter += 1
            nonce = f"nonce-{self._nonce_counter}".encode("utf-8")
            res = Reservation(handle=handle, cost=cost, deadline_ns=deadline_ns, nonce=nonce)
            self._reservations[nonce] = {
                "reservation": res,
                "state": "reserved",
                "handle": handle,
                "cost": cost,
                "target_key": target_key,
            }
            return res

    def commit(self, reservation: Reservation) -> None:
        with self._lock:
            info = self._reservations.get(reservation.nonce)
            if info is None:
                raise ValueError("unknown reservation")
            if info["state"] == "reserved":
                info["state"] = "committed"
                cost = info["cost"]
                target_key = info["target_key"]
                self._reserved_global -= cost
                self._committed_global += cost
                self._reserved_per_target[target_key] -= cost
                self._committed_per_target[target_key] = self._committed_per_target.get(target_key, 0) + cost
            elif info["state"] == "sent":
                info["state"] = "committed"
                cost = info["cost"]
                target_key = info["target_key"]
                self._committed_global += cost
                self._committed_per_target[target_key] = self._committed_per_target.get(target_key, 0) + cost

    def mark_sent(self, reservation: Reservation) -> None:
        with self._lock:
            info = self._reservations.get(reservation.nonce)
            if info and info["state"] == "reserved":
                info["state"] = "sent"
                cost = info["cost"]
                self._reserved_global -= cost
                # Reserved global credit transitions to sent (uncertain); per-target remains tracked

    def cancel(self, reservation: Reservation) -> None:
        with self._lock:
            info = self._reservations.get(reservation.nonce)
            if info is None:
                return
            if info["state"] == "reserved":
                info["state"] = "cancelled"
                cost = info["cost"]
                target_key = info["target_key"]
                self._reserved_global -= cost
                self._reserved_per_target[target_key] -= cost
            # Replay cancel or cancel after sent/committed is no-op (never refund)

    release = cancel


class PerTargetOnlyBudgetGovernorMutant(ReferenceBudgetGovernor):
    """Mutant that fails to enforce global budget caps."""

    def reserve(self, handle: Handle, cost: int, *, deadline_ns: int, now_ns: int = 0) -> Reservation:
        if cost <= 0 or deadline_ns <= now_ns or deadline_ns - now_ns > self.service_cap_ns:
            raise ValueError("invalid request")

        target_key = (handle.kind, handle.canonical_id, handle.generation)
        target_limit = self._get_target_limit(handle)

        with self._lock:
            # MUTANT BUG: Ignore global budget check completely!
            res_target = self._reserved_per_target.get(target_key, 0)
            com_target = self._committed_per_target.get(target_key, 0)
            if com_target + res_target + cost > target_limit:
                raise ValueError("per-target budget exceeded")

            self._reserved_global += cost
            self._reserved_per_target[target_key] = res_target + cost

            self._nonce_counter += 1
            nonce = f"nonce-{self._nonce_counter}".encode("utf-8")
            res = Reservation(handle=handle, cost=cost, deadline_ns=deadline_ns, nonce=nonce)
            self._reservations[nonce] = {
                "reservation": res,
                "state": "reserved",
                "handle": handle,
                "cost": cost,
                "target_key": target_key,
            }
            return res


class DoubleRefundBudgetGovernorMutant(ReferenceBudgetGovernor):
    """Mutant that double-refunds credit on replayed cancellation."""

    def cancel(self, reservation: Reservation) -> None:
        with self._lock:
            info = self._reservations.get(reservation.nonce)
            if info is None:
                return
            # MUTANT BUG: Refund credit every time cancel is called without state check
            cost = info["cost"]
            target_key = info["target_key"]
            self._reserved_global -= cost
            self._reserved_per_target[target_key] = self._reserved_per_target.get(target_key, 0) - cost
            info["state"] = "cancelled"

    release = cancel


class TestBudgetGovernorReferenceControls(unittest.TestCase):
    """Reference contract harness testing budget governor invariants, controls and negative mutants."""

    def test_last_available_credit_admits_positive_control(self):
        gov = ReferenceBudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res1 = gov.reserve(h, cost=9, deadline_ns=100, now_ns=0)
        self.assertIsNotNone(res1)

        # Last available credit (1 remaining out of 10) admits
        res2 = gov.reserve(h, cost=1, deadline_ns=100, now_ns=0)
        self.assertIsNotNone(res2)

        # Exceeding by 1 credit fails
        with self.assertRaises(ValueError):
            gov.reserve(h, cost=1, deadline_ns=100, now_ns=0)

    def test_race_global_and_per_target_on_last_credit(self):
        gov = ReferenceBudgetGovernor(global_budget=5, per_target_budget=5, service_cap_ns=1000)
        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="cdp_tab", canonical_id="tab-2", generation=1)

        # Pre-consume 4 credits globally
        gov.reserve(h1, cost=4, deadline_ns=100, now_ns=0)

        results = []
        errors = []

        def worker(handle):
            try:
                r = gov.reserve(handle, cost=1, deadline_ns=100, now_ns=0)
                results.append(r)
            except ValueError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(h1,))
        t2 = threading.Thread(target=worker, args=(h2,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly 1 thread succeeds on the last credit, 1 fails
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(gov._reserved_global, 5)

    def test_replay_cancellation_no_double_refund(self):
        gov = ReferenceBudgetGovernor(global_budget=10, per_target_budget=5, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res = gov.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        self.assertEqual(gov._reserved_global, 5)

        # First cancel refunds 5 credits
        gov.cancel(res)
        self.assertEqual(gov._reserved_global, 0)

        # Replay cancel must not double refund
        gov.cancel(res)
        gov.release(res)
        self.assertEqual(gov._reserved_global, 0)

        # Admitting 5 credits after single refund succeeds
        res2 = gov.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        self.assertIsNotNone(res2)

    def test_uncertain_sent_attempt_never_refunds(self):
        gov = ReferenceBudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res = gov.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        gov.mark_sent(res)

        # Cancellation of sent attempt does not refund
        gov.cancel(res)

        # Reserving 6 additional credits fails because 5 are locked as sent
        with self.assertRaises(ValueError):
            gov.reserve(h, cost=6, deadline_ns=100, now_ns=0)

    def test_retry_consumes_again(self):
        gov = ReferenceBudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res1 = gov.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        gov.mark_sent(res1)

        # Retry requires consuming another 5 credits
        res2 = gov.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        self.assertIsNotNone(res2)

        # Further retry fails (10/10 budget consumed)
        with self.assertRaises(ValueError):
            gov.reserve(h, cost=1, deadline_ns=100, now_ns=0)

    def test_absent_service_cap_yields_config_required(self):
        with self.assertRaisesRegex(ValueError, "CONFIG_REQUIRED"):
            ReferenceBudgetGovernor(global_budget=10, per_target_budget=5, service_cap_ns=None)

    def test_reject_per_target_only_mutant(self):
        mutant = PerTargetOnlyBudgetGovernorMutant(global_budget=5, per_target_budget=5, service_cap_ns=1000)
        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="cdp_tab", canonical_id="tab-2", generation=1)

        mutant.reserve(h1, cost=5, deadline_ns=100, now_ns=0)

        # Mutant fails to reject h2 when global budget (5) is exhausted
        try:
            mutant.reserve(h2, cost=5, deadline_ns=100, now_ns=0)
            mutant_failed = False
        except ValueError:
            mutant_failed = True

        self.assertFalse(mutant_failed, "Mutant allowed global budget overflow as expected")

    def test_reject_double_refund_mutant(self):
        mutant = DoubleRefundBudgetGovernorMutant(global_budget=5, per_target_budget=5, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res = mutant.reserve(h, cost=5, deadline_ns=100, now_ns=0)
        mutant.cancel(res)
        mutant.cancel(res)  # Mutant double-refunds into negative reserved budget!

        self.assertLess(mutant._reserved_global, 0, "Mutant caused negative reserved budget via double refund")


class TestBudgetGovernorProductionConformance(unittest.TestCase):
    """Production conformance tests probing wender.act.budget_governor."""

    @owner_expected_failure
    def test_production_budget_governor_last_credit_admits(self):
        from wender.act.budget_governor import (BudgetGovernor, BudgetExhausted,
                                                ConfigurationRequired, ReservationInactive)
        from wender.perceive.session_registry import Handle

        gov = BudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res1 = gov.reserve(h, cost=9, deadline_ns=10_000, now_ns=0)
        self.assertEqual(res1.cost, 9)

        res2 = gov.reserve(h, cost=1, deadline_ns=10_000, now_ns=0)
        self.assertEqual(res2.cost, 1)

        with self.assertRaises(BudgetExhausted):
            gov.reserve(h, cost=1, deadline_ns=10_000, now_ns=0)

    @owner_expected_failure
    def test_production_budget_governor_atomic_budget_refusal(self):
        from wender.act.budget_governor import (BudgetGovernor, BudgetExhausted,
                                                ConfigurationRequired, ReservationInactive)
        from wender.perceive.session_registry import Handle

        gov = BudgetGovernor(global_budget=10, per_target_budget={"tab-1": 5, "tab-2": 10}, service_cap_ns=1000)
        h1 = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)
        h2 = Handle(kind="cdp_tab", canonical_id="tab-2", generation=1)

        # Tab 1 hits per-target cap of 5
        gov.reserve(h1, cost=5, deadline_ns=10_000, now_ns=0)
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h1, cost=1, deadline_ns=10_000, now_ns=0)

        # Tab 2 hits global cap (5 + 5 = 10)
        gov.reserve(h2, cost=5, deadline_ns=10_000, now_ns=0)
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h2, cost=1, deadline_ns=10_000, now_ns=0)

    @owner_expected_failure
    def test_production_budget_governor_replay_cancel_and_commit(self):
        from wender.act.budget_governor import (BudgetGovernor, BudgetExhausted,
                                                ConfigurationRequired, ReservationInactive)
        from wender.perceive.session_registry import Handle

        gov = BudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=1000)
        h = Handle(kind="cdp_tab", canonical_id="tab-1", generation=1)

        res = gov.reserve(h, cost=5, deadline_ns=10_000, now_ns=0)
        gov.commit(res)

        # Replay commit or cancel on committed reservation
        with self.assertRaises(ReservationInactive):
            gov.commit(res)
        gov.cancel(res)

        # Committed budget stays consumed (5/10 remaining)
        res2 = gov.reserve(h, cost=5, deadline_ns=10_000, now_ns=0)
        self.assertIsNotNone(res2)

        with self.assertRaises(BudgetExhausted):
            gov.reserve(h, cost=1, deadline_ns=10_000, now_ns=0)

    @owner_expected_failure
    def test_production_budget_governor_config_required(self):
        from wender.act.budget_governor import (BudgetGovernor, BudgetExhausted,
                                                ConfigurationRequired, ReservationInactive)

        gov = BudgetGovernor(global_budget=10, per_target_budget=5, service_cap_ns=None)
        with self.assertRaises(ConfigurationRequired):
            gov.reserve(Handle("cdp_tab", "tab-1", 1), 1, deadline_ns=10_000, now_ns=0)


if __name__ == "__main__":
    unittest.main()
