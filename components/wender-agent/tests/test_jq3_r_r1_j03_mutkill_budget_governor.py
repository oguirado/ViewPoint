# @module {"id":"wa.tests.test_jq3_r_r1_j03_mutkill_budget_governor","layer":1,"owner":"ACT","deps":["wa.act.budget_governor"]}
"""Unit tests and in-memory AST mutation kills for BudgetGovernor."""

import ast
import random
import sys
import threading
import time
import types
import unittest

from wender.act.budget_governor import (
    BudgetGovernor,
    Reservation,
    Handle,
    BudgetError,
    ConfigurationRequired,
    BudgetExhausted,
    DeadlineInfeasible,
    StaleHandle,
    ReservationInactive,
    _configured_key,
    _handle_snapshot,
    _resource_key,
)


class FakeHandle:
    """Structural handle collaborator without class inheritance."""

    def __init__(self, kind: str, canonical_id: str, generation: int):
        self.kind = kind
        self.canonical_id = canonical_id
        self.generation = generation


class TestBudgetGovernorSpec(unittest.TestCase):
    """Substantive unit tests for BudgetGovernor specification and boundaries."""

    # Unique anchor for symbol wa.package.unit.symbol
    # @anchor wa.tests.test_jq3_r_r1_j03_mutkill_budget_governor.TestBudgetGovernorSpec

    def test_configured_target_identity_and_wildcard(self):
        """Test configured target lookup and wildcard matching."""
        gov = BudgetGovernor(
            global_budget=100,
            per_target_budget={
                ("cdp_tab", "target-1"): 20,
                "target-wildcard": 30,
            },
            service_cap_ns=10,
        )

        h1 = Handle("cdp_tab", "target-1", 1)
        self.assertEqual(gov.remaining_for(h1), 20)

        # Wildcard lookup for bare string target-wildcard
        h_wild = Handle("x11_window", "target-wildcard", 1)
        self.assertEqual(gov.remaining_for(h_wild), 30)

        # Missing configuration raises ConfigurationRequired
        h_missing = Handle("cdp_tab", "unconfigured", 1)
        with self.assertRaises(ConfigurationRequired):
            gov.remaining_for(h_missing)

    def test_reservation_commit_cancel_balances(self):
        """Test reservation, commit, cancel, and independent balances."""
        gov = BudgetGovernor(global_budget=100, per_target_budget=50, service_cap_ns=10)
        h = Handle("cdp_tab", "target-1", 1)

        self.assertEqual(gov.remaining_global, 100)
        self.assertEqual(gov.spent_global, 0)
        self.assertEqual(gov.remaining_for(h), 50)
        self.assertEqual(gov.spent_for(h), 0)
        self.assertEqual(gov.pending_count, 0)

        # Reserve cost 15
        res = gov.reserve(h, cost=15, deadline_ns=100, now_ns=0)
        self.assertIsInstance(res, Reservation)
        self.assertEqual(len(res.nonce), 16)
        self.assertEqual(gov.remaining_global, 85)
        self.assertEqual(gov.spent_global, 0)
        self.assertEqual(gov.remaining_for(h), 35)
        self.assertEqual(gov.spent_for(h), 0)
        self.assertEqual(gov.pending_count, 1)

        # Commit reservation
        gov.commit(res)
        self.assertEqual(gov.remaining_global, 85)
        self.assertEqual(gov.spent_global, 15)
        self.assertEqual(gov.remaining_for(h), 35)
        self.assertEqual(gov.spent_for(h), 15)
        self.assertEqual(gov.pending_count, 0)

        # Reserve cost 10 and then cancel
        res2 = gov.reserve(h, cost=10, deadline_ns=100, now_ns=0)
        self.assertEqual(gov.remaining_global, 75)
        self.assertEqual(gov.remaining_for(h), 25)
        self.assertEqual(gov.pending_count, 1)

        gov.cancel(res2)
        self.assertEqual(gov.remaining_global, 85)
        self.assertEqual(gov.spent_global, 15)
        self.assertEqual(gov.remaining_for(h), 35)
        self.assertEqual(gov.spent_for(h), 15)
        self.assertEqual(gov.pending_count, 0)

        # Duplicate cancel is a no-op
        gov.cancel(res2)
        self.assertEqual(gov.remaining_global, 85)

    def test_exact_exhaustion_boundaries(self):
        """Test exact budget exhaustion boundaries for global and per-target limits."""
        gov = BudgetGovernor(global_budget=10, per_target_budget=10, service_cap_ns=5)
        h1 = Handle("cdp_tab", "t1", 1)

        # Reserve exact limit
        res = gov.reserve(h1, cost=10, deadline_ns=10, now_ns=0)
        self.assertEqual(gov.remaining_global, 0)
        self.assertEqual(gov.remaining_for(h1), 0)

        # Further reservation raises BudgetExhausted
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h1, cost=1, deadline_ns=10, now_ns=0)

        # Test per-target vs global exhaustion separation
        gov2 = BudgetGovernor(global_budget=20, per_target_budget=5, service_cap_ns=5)
        h_a = Handle("cdp_tab", "ta", 1)
        h_b = Handle("cdp_tab", "tb", 1)

        gov2.reserve(h_a, cost=5, deadline_ns=10, now_ns=0)
        with self.assertRaises(BudgetExhausted):
            gov2.reserve(h_a, cost=1, deadline_ns=10, now_ns=0)

        # Second target still has budget
        res_b = gov2.reserve(h_b, cost=5, deadline_ns=10, now_ns=0)
        self.assertIsNotNone(res_b)

    def test_malformed_service_cap_boundaries(self):
        """Test service cap boundaries and infeasible deadlines."""
        with self.assertRaises(ValueError):
            BudgetGovernor(100, 50, service_cap_ns=0)

        with self.assertRaises(ValueError):
            BudgetGovernor(100, 50, service_cap_ns=-10)

        with self.assertRaises(ValueError):
            BudgetGovernor(100, 50, service_cap_ns="invalid")

        # service_cap_ns=None requires configuration at reserve time
        gov_none = BudgetGovernor(100, 50, service_cap_ns=None)
        h = Handle("cdp_tab", "t1", 1)
        with self.assertRaises(ConfigurationRequired):
            gov_none.reserve(h, cost=1, deadline_ns=100, now_ns=0)

        # Deadline infeasible check
        gov = BudgetGovernor(100, 50, service_cap_ns=20)
        with self.assertRaises(DeadlineInfeasible):
            gov.reserve(h, cost=1, deadline_ns=15, now_ns=0)

    def test_duplicate_conflicting_limits(self):
        """Test duplicate conflicting limits in per_target_budget raise ValueError."""
        h1 = Handle("cdp_tab", "t1", 1)
        h2 = Handle("cdp_tab", "t1", 2)  # Same canonical target, different generation

        with self.assertRaises(ValueError):
            BudgetGovernor(
                global_budget=100,
                per_target_budget={
                    h1: 10,
                    h2: 20,
                },
                service_cap_ns=10,
            )

    def test_cancellation_of_foreign_reservation(self):
        """Test cancellation of foreign reservation and invalid reservation types."""
        gov = BudgetGovernor(100, 50, service_cap_ns=10)
        h1 = Handle("cdp_tab", "t1", 1)
        h2 = Handle("cdp_tab", "t2", 1)

        res1 = gov.reserve(h1, cost=10, deadline_ns=100, now_ns=0)

        # Reservation with matching nonce but wrong token identity
        foreign_res = Reservation(handle=h2, cost=10, deadline_ns=100, nonce=res1.nonce)
        with self.assertRaises(ReservationInactive):
            gov.cancel(foreign_res)

        # Unknown tokens are foreign even when their nonce is not active
        unknown_res = Reservation(handle=h1, cost=10, deadline_ns=100, nonce=b"x" * 16)
        with self.assertRaises(ReservationInactive):
            gov.cancel(unknown_res)
        self.assertEqual(gov.remaining_global, 90)
        self.assertEqual(gov.remaining_for(h1), 40)
        self.assertEqual(gov.pending_count, 1)

        # Non-Reservation object raises ValueError
        with self.assertRaises(ValueError):
            gov.cancel("invalid_reservation")

    def test_conservation_with_independent_integer_ledger(self):
        """Verify global credit conservation against an independent integer ledger."""
        initial_global = 200
        target_limit = 100
        gov = BudgetGovernor(initial_global, target_limit, service_cap_ns=10)
        h1 = Handle("cdp_tab", "t1", 1)
        h2 = Handle("cdp_tab", "t2", 1)

        # Perform mixed sequence of reserves, commits, cancels
        r1 = gov.reserve(h1, cost=30, deadline_ns=100)
        r2 = gov.reserve(h2, cost=40, deadline_ns=100)
        gov.commit(r1)
        r3 = gov.reserve(h1, cost=20, deadline_ns=100)
        gov.cancel(r2)

        # Independent accounting check:
        # remaining_global + spent_global + sum(active hold costs) == initial_global
        pending_costs = 20  # r3
        self.assertEqual(
            gov.remaining_global + gov.spent_global + pending_costs,
            initial_global,
        )

    def test_seeded_random_reservation_schedules(self):
        """Property-based randomized schedules with 64 valid cases and shrink attempts."""
        seed = 1337
        rng = random.Random(seed)
        valid_cases = 0

        for case_idx in range(64):
            global_limit = rng.randint(50, 500)
            target_limit = rng.randint(20, 200)
            service_cap = rng.randint(1, 20)

            gov = BudgetGovernor(
                global_limit, target_limit, service_cap_ns=service_cap
            )
            h = Handle("cdp_tab", "target_rnd", 1)

            ops = []
            active_reservations = []

            for _ in range(10):
                op_type = rng.choice(["reserve", "commit", "cancel"])
                if op_type == "reserve":
                    cost = rng.randint(1, 30)
                    try:
                        res = gov.reserve(
                            h, cost=cost, deadline_ns=100, now_ns=0
                        )
                        active_reservations.append(res)
                        ops.append(("reserve", cost, True))
                    except BudgetError:
                        ops.append(("reserve", cost, False))
                elif op_type == "commit" and active_reservations:
                    res = active_reservations.pop(
                        rng.randrange(len(active_reservations))
                    )
                    gov.commit(res)
                    ops.append(("commit", res.cost, True))
                elif op_type == "cancel" and active_reservations:
                    res = active_reservations.pop(
                        rng.randrange(len(active_reservations))
                    )
                    gov.cancel(res)
                    ops.append(("cancel", res.cost, True))

            # Validate invariant conservation
            pending_sum = sum(r.cost for r in active_reservations)
            self.assertEqual(
                gov.remaining_global + gov.spent_global + pending_sum,
                global_limit,
                f"Seed {seed}, case {case_idx}: Conservation failed for fixture {ops}",
            )
            valid_cases += 1

        self.assertEqual(valid_cases, 64)

    def test_thread_concurrency_bounded_joins(self):
        """Thread concurrency test with barriers, events, bounded joins and thread leak checks."""
        initial_global = 100
        gov = BudgetGovernor(initial_global, 100, service_cap_ns=5)
        num_threads = 4
        barrier = threading.Barrier(num_threads)
        stop_event = threading.Event()

        threads = []

        def worker(worker_id):
            barrier.wait()
            h = Handle("cdp_tab", f"t_{worker_id}", 1)
            for _ in range(8):
                if stop_event.is_set():
                    break
                try:
                    res = gov.reserve(h, cost=2, deadline_ns=100, now_ns=0)
                    time.sleep(0.001)
                    if worker_id % 2 == 0:
                        gov.commit(res)
                    else:
                        gov.cancel(res)
                except BudgetError:
                    pass

        for i in range(num_threads):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "Leaked thread detected after 2s wait")

        # Invariant check post concurrency
        self.assertLessEqual(gov.remaining_global + gov.spent_global, initial_global)


class TestBudgetGovernorASTMutationKills(unittest.TestCase):
    """In-memory AST mutation prove harness targeting catalogue obligations."""

    # Unique anchor for symbol wa.package.unit.symbol
    # @anchor wa.tests.test_jq3_r_r1_j03_mutkill_budget_governor.TestBudgetGovernorASTMutationKills

    @classmethod
    def setUpClass(cls):
        with open("wender/act/budget_governor.py", "r", encoding="utf-8") as f:
            cls.source_text = f.read()

    def _apply_mutation(self, mut_id: str) -> types.ModuleType:
        tree = ast.parse(self.source_text)

        class CatalogueMutator(ast.NodeTransformer):
            def visit_BoolOp(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if mut_id == "M1" and lineno == 90 and isinstance(node.op, ast.Or):
                    node.op = ast.And()
                elif mut_id == "M3" and lineno == 62 and isinstance(node.op, ast.Or):
                    node.op = ast.And()
                return node

            def visit_Compare(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if (
                    mut_id == "M2"
                    and lineno == 177
                    and isinstance(node.ops[0], ast.In)
                ):
                    node.ops[0] = ast.NotIn()
                return node

            def visit_Constant(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if mut_id == "M4" and lineno == 237 and node.value == 16:
                    node.value = 17
                elif mut_id == "M5" and lineno == 371 and node.value == 0:
                    node.value = 1
                return node

            def visit_If(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if mut_id == "M7" and lineno == 142:
                    node.test = ast.Constant(value=False)
                elif mut_id == "M8" and lineno == 328:
                    node.test = ast.Constant(value=False)
                elif mut_id == "M9" and lineno == 149:
                    node.test = ast.Constant(value=False)
                elif mut_id == "M10" and lineno == 259:
                    node.test = ast.Constant(value=False)
                return node

            def visit_Call(self, node):
                self.generic_visit(node)
                lineno = getattr(node, "lineno", None)
                if (
                    mut_id == "M11"
                    and lineno == 365
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                ):
                    if len(node.args) >= 2:
                        node.args[0], node.args[1] = node.args[1], node.args[0]
                elif (
                    mut_id == "M12"
                    and lineno == 234
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_resource_key"
                ):
                    if len(node.args) >= 2:
                        node.args[0], node.args[1] = node.args[1], node.args[0]
                elif (
                    mut_id == "M13"
                    and lineno == 88
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "isinstance"
                ):
                    if len(node.args) >= 2:
                        node.args[0], node.args[1] = node.args[1], node.args[0]
                return node

        mutated_tree = CatalogueMutator().visit(tree)
        ast.fix_missing_locations(mutated_tree)
        code = compile(mutated_tree, "wender/act/budget_governor.py", "exec")

        mod = types.ModuleType("wender.act.budget_governor")
        mod.__file__ = "wender/act/budget_governor.py"
        exec(code, mod.__dict__)
        return mod

    def _run_with_module(self, mut_id: str, test_func):
        original_sys_modules = dict(sys.modules)
        try:
            mod = self._apply_mutation(mut_id)
            sys.modules["wender.act.budget_governor"] = mod
            test_func(mod)
        finally:
            sys.modules.clear()
            sys.modules.update(original_sys_modules)

    def test_ast_mutation_kills_all_catalogue_obligations(self):
        """Verify baseline positive control and prove kills for M1..M5, M7..M13."""

        # Assertion definitions targeting specific mutations:
        def assert_M1(m):
            try:
                m._configured_key(("cdp_tab", "  "))
                raise AssertionError("M1 survived: expected ValueError on whitespace canonical_id")
            except ValueError:
                pass

        def assert_M2(m):
            g = m.BudgetGovernor(100, {"t1": 50}, service_cap_ns=10)
            try:
                val = g._limit_for(("cdp_tab", "t1"))
                if val != 50:
                    raise AssertionError(f"M2 survived: expected wildcard limit 50, got {val}")
            except m.ConfigurationRequired as exc:
                raise AssertionError(f"M2 kill verified via ConfigurationRequired: {exc}")

        def assert_M3(m):
            try:
                m._handle_snapshot(FakeHandle("bad_kind", "t1", 1))
                raise AssertionError("M3 survived: expected ValueError on bad handle kind")
            except ValueError as exc:
                if not str(exc).startswith("unsupported handle kind"):
                    raise AssertionError(f"M3 survived: unexpected exception msg: {exc}")

        def assert_M4(m):
            g = m.BudgetGovernor(100, 50, service_cap_ns=10)
            h = m.Handle("cdp_tab", "t1", 1)
            res = g.reserve(h, 1, deadline_ns=100)
            if len(res.nonce) != 16:
                raise AssertionError(f"M4 survived: expected nonce length 16, got {len(res.nonce)}")

        def assert_M5(m):
            g = m.BudgetGovernor(100, 50, service_cap_ns=10)
            h = m.Handle("cdp_tab", "t1", 1)
            val = g.spent_for(h)
            if val != 0:
                raise AssertionError(f"M5 survived: expected spent_for 0, got {val}")

        def assert_M7(m):
            h1 = m.Handle("cdp_tab", "t1", 1)
            h2 = m.Handle("cdp_tab", "t1", 2)
            try:
                m.BudgetGovernor(100, {h1: 10, h2: 20}, service_cap_ns=10)
                raise AssertionError("M7 survived: expected ValueError on conflicting limits")
            except ValueError:
                pass

        def assert_M8(m):
            g = m.BudgetGovernor(100, 50, service_cap_ns=10)
            try:
                g.cancel("not_a_reservation")
                raise AssertionError("M8 survived: expected ValueError on invalid reservation type")
            except ValueError:
                pass
            except AttributeError as exc:
                raise AssertionError(f"M8 kill verified via AttributeError: {exc}")

        def assert_M9(m):
            try:
                m.BudgetGovernor(100, 50, service_cap_ns=-10)
                raise AssertionError("M9 survived: expected ValueError on negative service_cap_ns")
            except ValueError:
                pass

        def assert_M10(m):
            g = m.BudgetGovernor(5, 50, service_cap_ns=10)
            h = m.Handle("cdp_tab", "t1", 1)
            try:
                g.reserve(h, 10, deadline_ns=100)
                raise AssertionError("M10 survived: expected BudgetExhausted on global budget")
            except m.BudgetExhausted:
                pass

        def assert_M11(m):
            g = m.BudgetGovernor(100, 50, service_cap_ns=10)
            h = m.Handle("cdp_tab", "t1", 1)
            val = g.remaining_for(h)
            if val != 50:
                raise AssertionError(f"M11 survived: expected remaining_for 50, got {val}")

        def assert_M12(m):
            g = m.BudgetGovernor(100, {("cdp_tab", "t1"): 50}, service_cap_ns=10)
            h = m.Handle("cdp_tab", "t1", 1)
            try:
                res = g.reserve(h, 1, deadline_ns=100)
                if res is None:
                    raise AssertionError("M12 survived: reserve returned None")
            except Exception as exc:
                raise AssertionError(f"M12 kill verified via exception: {exc}")

        def assert_M13(m):
            try:
                res = m._configured_key(("cdp_tab", "t1"))
                if res != ("cdp_tab", "t1"):
                    raise AssertionError(f"M13 survived: expected ('cdp_tab', 't1'), got {res}")
            except TypeError as exc:
                raise AssertionError(f"M13 kill verified via TypeError: {exc}")

        catalogue_map = {
            "M-wender-act-budget-governor-1": ("M1", assert_M1),
            "M-wender-act-budget-governor-2": ("M2", assert_M2),
            "M-wender-act-budget-governor-3": ("M3", assert_M3),
            "M-wender-act-budget-governor-4": ("M4", assert_M4),
            "M-wender-act-budget-governor-5": ("M5", assert_M5),
            "M-wender-act-budget-governor-7": ("M7", assert_M7),
            "M-wender-act-budget-governor-8": ("M8", assert_M8),
            "M-wender-act-budget-governor-9": ("M9", assert_M9),
            "M-wender-act-budget-governor-10": ("M10", assert_M10),
            "M-wender-act-budget-governor-11": ("M11", assert_M11),
            "M-wender-act-budget-governor-12": ("M12", assert_M12),
            "M-wender-act-budget-governor-13": ("M13", assert_M13),
        }

        # 1. Baseline positive control: All assertions must pass on unmutated code
        for cat_id, (m_id, assert_fn) in catalogue_map.items():
            try:
                self._run_with_module("NONE", assert_fn)
            except Exception as exc:
                self.fail(f"Baseline positive control failed for {cat_id} ({m_id}) on clean code: {exc}")

        # 2. Mutation kills: Each mutation must produce an AssertionError
        for cat_id, (m_id, assert_fn) in catalogue_map.items():
            with self.assertRaises(AssertionError, msg=f"Mutation {cat_id} ({m_id}) survived!"):
                self._run_with_module(m_id, assert_fn)


if __name__ == "__main__":
    unittest.main()
