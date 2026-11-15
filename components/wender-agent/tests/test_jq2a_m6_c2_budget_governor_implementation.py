"""G M6 4 offline evidence against global-only and double-refund mutants."""
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from threading import Barrier, Thread
from unittest.mock import patch
from wender.act.budget_governor import (
    BudgetGovernor, BudgetExhausted, ConfigurationRequired, DeadlineInfeasible,
    Handle, Reservation, ReservationInactive, StaleHandle,
)


def h(name, kind="cdp_tab", generation=1):
    return Handle(kind, name, generation)


def governor(global_budget, per_target_budget):
    return BudgetGovernor(global_budget, per_target_budget, service_cap_ns=10)


class TestBudgetGovernor(unittest.TestCase):
    def test_frozen_public_reservation_contract(self):
        self.assertEqual([f.name for f in fields(Reservation)],
                         ["handle", "cost", "deadline_ns", "nonce"])
        g = governor(1, 1)
        r = g.reserve(h("a", generation=10**100), 1, deadline_ns=100)
        self.assertIsInstance(r.nonce, bytes)
        with self.assertRaises(FrozenInstanceError):
            r.cost = 7
        g.cancel(r)

    def test_per_target_only_mutant_rejected(self):
        g = governor(1, 1)
        first = g.reserve(h("a"), 1, deadline_ns=100)
        with self.assertRaises(BudgetExhausted):
            g.reserve(h("b"), 1, deadline_ns=100)
        self.assertEqual((g.remaining_global, g.remaining_for(h("b"))), (0, 1))
        g.commit(first)
        g.cancel(first)
        self.assertEqual((g.remaining_global, g.spent_global), (0, 1))

    def test_exact_last_credit_positive_control(self):
        g = governor(2, 2)
        r = g.reserve(h("a"), 2, deadline_ns=100)
        self.assertEqual((g.remaining_global, g.remaining_for(h("a"))), (0, 0))
        with self.assertRaises(BudgetExhausted):
            g.reserve(h("a"), 1, deadline_ns=100)
        g.release(r)
        r2 = g.reserve(h("a"), 2, deadline_ns=100)
        g.commit(r2)
        self.assertEqual((g.remaining_global, g.spent_for(h("a"))), (0, 2))

    def test_per_target_limit_is_independent(self):
        g = governor(4, 1)
        a = g.reserve(h("a"), 1, deadline_ns=100)
        with self.assertRaises(BudgetExhausted):
            g.reserve(h("a"), 1, deadline_ns=100)
        b = g.reserve(h("b"), 1, deadline_ns=100)
        self.assertEqual(g.remaining_global, 2)
        g.cancel(a)
        g.cancel(b)
        self.assertEqual(g.remaining_global, 4)

    def test_double_refund_mutant_rejected(self):
        g = governor(1, 1)
        r = g.reserve(h("a"), 1, deadline_ns=100)
        g.cancel(r)
        g.release(r)
        g.cancel(r)
        self.assertEqual((g.remaining_global, g.remaining_for(h("a"))), (1, 1))
        r2 = g.reserve(h("b"), 1, deadline_ns=100)
        with self.assertRaises(BudgetExhausted):
            g.reserve(h("a"), 1, deadline_ns=100)
        g.cancel(r2)
        with self.assertRaises(ReservationInactive):
            g.commit(r)

    def test_single_use_and_forged_copy(self):
        g = governor(2, 2)
        r = g.reserve(h("a"), 1, deadline_ns=100)
        forged = replace(r)
        with self.assertRaises(ReservationInactive):
            g.cancel(forged)
        with self.assertRaises(ReservationInactive):
            g.commit(forged)
        g.commit(r)
        with self.assertRaises(ReservationInactive):
            g.commit(r)
        g.cancel(r)
        self.assertEqual((g.remaining_global, g.spent_global), (1, 1))

    def test_uncertain_mutating_retry_consumes_again(self):
        g = governor(2, 2)
        for _ in range(2):
            r = g.reserve(h("a"), 1, deadline_ns=100)
            g.commit(r)  # under external send fence before a potentially sent step
            g.cancel(r)  # UNKNOWN outcome must never restore spent credit
        self.assertEqual(g.spent_global, 2)
        with self.assertRaises(BudgetExhausted):
            g.reserve(h("a"), 1, deadline_ns=100)

    def test_required_service_cap_and_absolute_deadline(self):
        g = BudgetGovernor(1, 1)
        with self.assertRaisesRegex(ConfigurationRequired, "CONFIG_REQUIRED"):
            g.reserve(h("a"), 1, deadline_ns=100)
        g = governor(1, 1)
        with self.assertRaises(DeadlineInfeasible):
            g.reserve(h("a"), 1, deadline_ns=19, now_ns=10)
        r = g.reserve(h("a"), 1, deadline_ns=20, now_ns=10)
        self.assertEqual(r.deadline_ns, 20)
        g.cancel(r)

    def test_dict_configuration_alias_and_incarnation(self):
        g = governor(2, {("capsule", "dest"): 1})
        cap, screen = h("dest", "capsule"), h("dest", "xvfb_screen")
        r = g.reserve(cap, 1, deadline_ns=100)
        with self.assertRaises(BudgetExhausted):
            g.reserve(screen, 1, deadline_ns=100)
        g.cancel(r)
        later = g.reserve(h("dest", "capsule", 10**100), 1, deadline_ns=100)
        with self.assertRaises(StaleHandle):
            g.reserve(cap, 1, deadline_ns=100)
        g.commit(later)
        self.assertEqual(g.spent_for(screen), 1)
        with self.assertRaises(ConfigurationRequired):
            g.reserve(h("absent"), 1, deadline_ns=100)

    def test_parallel_last_credit_global_and_target(self):
        for shared in (False, True):
            n = 8
            g = governor(1 if not shared else 8, 1)
            barrier = Barrier(n)
            passed, refused, errors = [], [], []

            def attempt(i):
                try:
                    barrier.wait(timeout=8)
                    passed.append(g.reserve(h("one" if shared else str(i)), 1,
                                            deadline_ns=100))
                except BudgetExhausted:
                    refused.append(i)
                except BaseException as error:
                    errors.append(error)

            threads = [Thread(target=attempt, args=(i,)) for i in range(n)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            self.assertEqual((len(passed), len(refused)), (1, n - 1))
            g.cancel(passed[0])
            self.assertEqual(g.remaining_global, 8 if shared else 1)

    def test_parallel_double_cancel_is_exactly_one_refund(self):
        g = governor(1, 1)
        r = g.reserve(h("a"), 1, deadline_ns=100)
        barrier = Barrier(8)
        errors = []

        def cancel():
            try:
                barrier.wait(timeout=8)
                g.cancel(r)
            except BaseException as error:
                errors.append(error)

        threads = [Thread(target=cancel) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual((g.remaining_global, g.pending_count), (1, 0))

    def test_forbidden_io_raises_if_accounting_attempts_it(self):
        g = governor(1, 1)
        with (patch("socket.socket", side_effect=AssertionError("network forbidden")),
              patch("subprocess.Popen", side_effect=AssertionError("process forbidden")),
              patch("builtins.open", side_effect=AssertionError("disk forbidden"))):
            r = g.reserve(h("a"), 1, deadline_ns=100)
            g.commit(r)
            self.assertEqual(g.spent_global, 1)


if __name__ == "__main__":
    unittest.main()
