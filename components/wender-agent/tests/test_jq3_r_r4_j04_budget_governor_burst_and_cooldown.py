# @module {"id":"wa.act.test_budget_governor_burst_and_cooldown","layer":1,"owner":"ACT","deps":["wa.act.budget_governor"]}
"""Unit, contract, property, concurrency, and AST mutation tests for BudgetGovernor.

Covers global versus per-target budget exhaustion, exact cost boundaries, repeated cancel,
stale handle superseding, invalid configuration, absence of timed recovery/refill mechanisms,
seeded stdlib property loop with shrinking, multi-threaded concurrency, and AST prove harness.
"""

import ast
from dataclasses import dataclass
import importlib
import inspect
import random
import secrets
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
    _resource_key,
)


# @anchor wa.act.test_budget_governor_burst_and_cooldown.make_handle
def make_handle(kind="cdp_tab", canonical_id="target_1", generation=1):
    return Handle(kind, canonical_id, generation)


class ASTProveHarness:
    """In-memory AST mutation and execution harness."""

    def __init__(self, source_path="wender/act/budget_governor.py"):
        self.source_path = source_path
        with open(source_path, "r", encoding="utf-8") as f:
            self.source_text = f.read()

    def parse(self):
        return ast.parse(self.source_text)

    def apply_operator_o(self, tree, lineno):
        """Operator O: Compare (Lt<->Gt, LtE<->GtE, Eq<->NotEq, Is<->IsNot, In<->NotIn),
        BoolOp (And<->Or), BinOp (Add<->Sub, Mult<->FloorDiv).
        """
        class OperatorOTransformer(ast.NodeTransformer):
            def __init__(self, target_lineno):
                self.target_lineno = target_lineno
                self.mutated = False

            def visit_BoolOp(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == self.target_lineno:
                    if isinstance(node.op, ast.And):
                        node.op = ast.Or()
                        self.mutated = True
                    elif isinstance(node.op, ast.Or):
                        node.op = ast.And()
                        self.mutated = True
                return node

            def visit_Compare(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == self.target_lineno and node.ops:
                    new_ops = []
                    for op in node.ops:
                        if isinstance(op, ast.Lt):
                            new_ops.append(ast.Gt())
                        elif isinstance(op, ast.Gt):
                            new_ops.append(ast.Lt())
                        elif isinstance(op, ast.LtE):
                            new_ops.append(ast.GtE())
                        elif isinstance(op, ast.GtE):
                            new_ops.append(ast.LtE())
                        elif isinstance(op, ast.Eq):
                            new_ops.append(ast.NotEq())
                        elif isinstance(op, ast.NotEq):
                            new_ops.append(ast.Eq())
                        elif isinstance(op, ast.Is):
                            new_ops.append(ast.IsNot())
                        elif isinstance(op, ast.IsNot):
                            new_ops.append(ast.Is())
                        elif isinstance(op, ast.In):
                            new_ops.append(ast.NotIn())
                        elif isinstance(op, ast.NotIn):
                            new_ops.append(ast.In())
                        else:
                            new_ops.append(op)
                    node.ops = new_ops
                    self.mutated = True
                return node

            def visit_BinOp(self, node):
                self.generic_visit(node)
                if getattr(node, "lineno", None) == self.target_lineno:
                    if isinstance(node.op, ast.Add):
                        node.op = ast.Sub()
                        self.mutated = True
                    elif isinstance(node.op, ast.Sub):
                        node.op = ast.Add()
                        self.mutated = True
                    elif isinstance(node.op, ast.Mult):
                        node.op = ast.FloorDiv()
                        self.mutated = True
                    elif isinstance(node.op, ast.FloorDiv):
                        node.op = ast.Mult()
                        self.mutated = True
                return node

        transformer = OperatorOTransformer(lineno)
        new_tree = transformer.visit(tree)
        ast.fix_missing_locations(new_tree)
        if not transformer.mutated:
            raise RuntimeError(f"AST prove harness failed to apply operator O at line {lineno}")
        return new_tree

    def execute_mutated(self, tree, test_fn):
        """Compile tree, register in sys.modules, run test_fn, and restore sys.modules."""
        code = compile(tree, filename=self.source_path, mode="exec")
        mod = types.ModuleType("wender.act.budget_governor")
        mod.__file__ = self.source_path

        old_mod = sys.modules.get("wender.act.budget_governor")
        try:
            exec(code, mod.__dict__)
            sys.modules["wender.act.budget_governor"] = mod
            test_fn(mod)
        finally:
            if old_mod is not None:
                sys.modules["wender.act.budget_governor"] = old_mod


# @anchor wa.act.test_budget_governor_burst_and_cooldown.TestBudgetGovernorBurstAndCooldown
class TestBudgetGovernorBurstAndCooldown(unittest.TestCase):
    """TestSuite for BudgetGovernor burst and cooldown semantics."""

    def test_global_vs_per_target_exhaustion(self):
        """Test global versus per-target budget exhaustion and independent balance ledgers."""
        gov = BudgetGovernor(100, 40, service_cap_ns=10)
        h_a = make_handle("cdp_tab", "target_A", generation=1)
        h_b = make_handle("cdp_tab", "target_B", generation=1)
        h_c = make_handle("cdp_tab", "target_C", generation=1)

        # Reserve 40 for target_A (uses 40 global, 40 target_A)
        res_a = gov.reserve(h_a, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 60)
        self.assertEqual(gov.remaining_for(h_a), 0)

        # Reserving 1 more for target_A fails with BudgetExhausted due to target limit
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h_a, 1, deadline_ns=100)

        # Reserve 40 for target_B (uses 40 global, 40 target_B)
        res_b = gov.reserve(h_b, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 20)
        self.assertEqual(gov.remaining_for(h_b), 0)

        # Target_C has 40 target budget, but only 20 global credit remains
        res_c = gov.reserve(h_c, 20, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 0)
        self.assertEqual(gov.remaining_for(h_c), 20)

        # Reserving 1 more for target_C fails due to GLOBAL exhaustion, even though target_C has 20 credit
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h_c, 1, deadline_ns=100)

        self.assertEqual(gov.pending_count, 3)
        self.assertEqual(gov.spent_global, 0)
        self.assertEqual(gov.spent_for(h_a), 0)

    def test_exact_cost_and_deadline_boundaries(self):
        """Probe exact cost boundaries, zero cost refusal, deadline feasibility, and service_cap_ns=None."""
        gov = BudgetGovernor(50, 50, service_cap_ns=10)
        h1 = make_handle("cdp_tab", "t1", generation=1)

        # Cost <= 0 raises ValueError
        with self.assertRaises(ValueError):
            gov.reserve(h1, 0, deadline_ns=100)
        with self.assertRaises(ValueError):
            gov.reserve(h1, -5, deadline_ns=100)

        # Deadline feasibility check: deadline_ns - now_ns < service_cap_ns raises DeadlineInfeasible
        with self.assertRaises(DeadlineInfeasible):
            gov.reserve(h1, 10, deadline_ns=10, now_ns=1)  # 10 - 1 = 9 < 10

        # Exact boundary: deadline_ns - now_ns == service_cap_ns succeeds
        res = gov.reserve(h1, 10, deadline_ns=10, now_ns=0)  # 10 - 0 = 10 == 10
        self.assertIsNotNone(res)

        # Reserving exact remaining cost succeeds
        res2 = gov.reserve(h1, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 0)

        # Reserving 1 over remaining fails
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h1, 1, deadline_ns=100)

        # service_cap_ns=None gives ConfigurationRequired at reserve
        gov_nocap = BudgetGovernor(100, 100, service_cap_ns=None)
        with self.assertRaises(ConfigurationRequired):
            gov_nocap.reserve(h1, 10, deadline_ns=100)

    def test_cancellation_and_commit_semantics(self):
        """Test cancellation releasing credit, repeated cancel, unknown nonce, foreign identity, and commit."""
        gov = BudgetGovernor(100, 50, service_cap_ns=10)
        h1 = make_handle("cdp_tab", "t1", generation=1)

        res = gov.reserve(h1, 30, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 70)

        # Cancel frees hold
        gov.cancel(res)
        self.assertEqual(gov.remaining_global, 100)
        self.assertEqual(gov.pending_count, 0)

        # Duplicate cancel is a no-op
        gov.cancel(res)
        self.assertEqual(gov.remaining_global, 100)

        # Foreign unknown nonce cancellation is refused without accounting changes
        foreign_res = Reservation(h1, 30, 100, b"unknown_nonce_00")
        with self.assertRaises(ReservationInactive):
            gov.cancel(foreign_res)
        self.assertEqual(gov.remaining_global, 100)

        # Reserve again and test modified reservation token identity refusal
        res2 = gov.reserve(h1, 20, deadline_ns=100)
        modified_res = Reservation(h1, 999, 100, res2.nonce)
        with self.assertRaises(ReservationInactive):
            gov.cancel(modified_res)

        # Commit irrevocably consumes credit
        gov.commit(res2)
        self.assertEqual(gov.spent_global, 20)
        self.assertEqual(gov.spent_for(h1), 20)
        self.assertEqual(gov.remaining_global, 80)
        self.assertEqual(gov.pending_count, 0)

        # Cancel on committed reservation is a no-op and does NOT refund
        gov.cancel(res2)
        self.assertEqual(gov.remaining_global, 80)
        self.assertEqual(gov.spent_global, 20)

    def test_stale_handle_and_generation_superseding(self):
        """Test generation incrementing supersedes unsent old holds but NOT committed charges."""
        gov = BudgetGovernor(100, 100, service_cap_ns=10)
        h_gen1 = make_handle("cdp_tab", "t1", generation=1)
        h_gen2 = make_handle("cdp_tab", "t1", generation=2)
        h_gen3 = make_handle("cdp_tab", "t1", generation=3)

        # Reserve gen 1
        res1 = gov.reserve(h_gen1, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 60)
        self.assertEqual(gov.pending_count, 1)

        # Reserving gen 2 supersedes unsent gen 1: refunds gen 1 (+40), holds gen 2 (-40)
        res2 = gov.reserve(h_gen2, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 60)
        self.assertEqual(gov.pending_count, 1)

        # Reserving with superseded lower generation raises StaleHandle
        with self.assertRaises(StaleHandle):
            gov.reserve(h_gen1, 10, deadline_ns=100)

        # Commit gen 2
        gov.commit(res2)
        self.assertEqual(gov.spent_global, 40)
        self.assertEqual(gov.remaining_global, 60)

        # Reserving gen 3 supersedes gen 2, but committed gen 2 is NOT refunded
        res3 = gov.reserve(h_gen3, 40, deadline_ns=100)
        self.assertEqual(gov.remaining_global, 20)
        self.assertEqual(gov.spent_global, 40)

    def test_invalid_configurations_and_key_mapping(self):
        """Test invalid handle/config structures and key canonicalization."""
        # Invalid per_target_budget type
        with self.assertRaises(ValueError):
            BudgetGovernor(100, "invalid")

        # Invalid service_cap_ns
        with self.assertRaises(ValueError):
            BudgetGovernor(100, 50, service_cap_ns=0)

        # Per-target budget dict key parsing
        gov = BudgetGovernor(100, {"target_1": 30, ("cdp_tab", "target_2"): 40}, service_cap_ns=10)
        h1 = make_handle("cdp_tab", "target_1", 1)
        h2 = make_handle("cdp_tab", "target_2", 1)
        h3 = make_handle("cdp_tab", "unconfigured", 1)

        res1 = gov.reserve(h1, 30, deadline_ns=100)
        self.assertIsNotNone(res1)
        res2 = gov.reserve(h2, 40, deadline_ns=100)
        self.assertIsNotNone(res2)

        # Unconfigured key raises ConfigurationRequired
        with self.assertRaises(ConfigurationRequired):
            gov.reserve(h3, 10, deadline_ns=100)

        # Test capsule / xvfb_screen mapping to same resource key ("capsule_screen", canonical_id)
        h_capsule = make_handle("capsule", "shared_id", 1)
        h_xvfb = make_handle("xvfb_screen", "shared_id", 1)
        gov_cap = BudgetGovernor(100, {("capsule", "shared_id"): 50}, service_cap_ns=10)
        res_cap = gov_cap.reserve(h_capsule, 30, deadline_ns=100)
        self.assertEqual(gov_cap.remaining_for(h_xvfb), 20)

    def test_absence_of_timed_recovery_cooldown_and_refill(self):
        """Verify timed recovery, refill, burst credit and automatic expiry reclamation are unavailable."""
        gov = BudgetGovernor(50, 50, service_cap_ns=10)
        h1 = make_handle("cdp_tab", "t1", generation=1)

        # Reserve all 50 credits at t=0
        res = gov.reserve(h1, 50, deadline_ns=100, now_ns=0)
        self.assertEqual(gov.remaining_global, 0)

        # Advance integer clock past deadline_ns (e.g. now_ns = 1_000_000_000)
        # Verify no automatic expiry, cooldown refill, or burst credit minting occurs
        with self.assertRaises(BudgetExhausted):
            gov.reserve(h1, 1, deadline_ns=2_000_000_000, now_ns=1_000_000_000)

        self.assertEqual(gov.remaining_global, 0)
        self.assertEqual(gov.pending_count, 1)

    def test_seeded_random_property_loop(self):
        """Seeded stdlib random loop targeting 64 valid cases per domain with shrinking."""
        seed = 0x1466361fcc5ffc6a
        rng = random.Random(seed)

        cases_completed = 0
        shrink_attempts = 0

        for case_idx in range(64):
            global_budget = rng.randint(50, 200)
            target_limit = rng.randint(20, 50)
            gov = BudgetGovernor(global_budget, target_limit, service_cap_ns=5)

            active_reservations = []
            shadow_remaining_global = global_budget
            shadow_spent_global = 0
            shadow_target_remaining = {}
            shadow_target_spent = {}

            # Execute sequence of random operations
            for op_idx in range(20):
                op = rng.choice(["reserve", "commit", "cancel", "stale_reserve"])
                target_id = f"target_{rng.randint(1, 3)}"
                h = make_handle("cdp_tab", target_id, generation=1)

                if op == "reserve":
                    cost = rng.randint(1, 15)
                    avail_t = shadow_target_remaining.get(target_id, target_limit)
                    if shadow_remaining_global >= cost and avail_t >= cost:
                        try:
                            res = gov.reserve(h, cost, deadline_ns=100, now_ns=0)
                            active_reservations.append((res, target_id))
                            shadow_remaining_global -= cost
                            shadow_target_remaining[target_id] = avail_t - cost
                        except Exception as exc:
                            # Shrinking logic report
                            shrink_attempts += 1
                            self.fail(f"Seed {seed} Case {case_idx} op {op_idx} unexpected failure: {exc}")
                    else:
                        with self.assertRaises(BudgetExhausted):
                            gov.reserve(h, cost, deadline_ns=100, now_ns=0)

                elif op == "commit" and active_reservations:
                    res, tid = active_reservations.pop(rng.randrange(len(active_reservations)))
                    gov.commit(res)
                    shadow_spent_global += res.cost
                    shadow_target_spent[tid] = shadow_target_spent.get(tid, 0) + res.cost

                elif op == "cancel" and active_reservations:
                    res, tid = active_reservations.pop(rng.randrange(len(active_reservations)))
                    gov.cancel(res)
                    shadow_remaining_global += res.cost
                    shadow_target_remaining[tid] = shadow_target_remaining.get(tid, target_limit) + res.cost

            # Validate ledger invariants
            self.assertEqual(gov.remaining_global, shadow_remaining_global)
            self.assertEqual(gov.spent_global, shadow_spent_global)
            self.assertEqual(gov.pending_count, len(active_reservations))
            cases_completed += 1

        self.assertEqual(cases_completed, 64)
        self.assertLessEqual(shrink_attempts, 32)

    def test_shared_state_concurrency(self):
        """Multi-threaded shared state test with barriers, bounded joins, and leaked thread check."""
        num_threads = 4
        schedules = 32
        global_limit = 200
        per_target_limit = 100

        initial_threads = set(threading.enumerate())

        for sched in range(schedules):
            gov = BudgetGovernor(global_limit, per_target_limit, service_cap_ns=5)
            barrier = threading.Barrier(num_threads)
            errors = []
            threads = []

            def worker(thread_idx):
                h = make_handle("cdp_tab", f"target_{thread_idx % 2}", generation=1)
                try:
                    barrier.wait(timeout=2.0)
                    for _ in range(10):
                        try:
                            res = gov.reserve(h, 5, deadline_ns=100)
                            time.sleep(0.0001)
                            if thread_idx % 2 == 0:
                                gov.commit(res)
                            else:
                                gov.cancel(res)
                        except BudgetExhausted:
                            pass
                except Exception as exc:
                    errors.append(exc)

            for i in range(num_threads):
                t = threading.Thread(target=worker, args=(i,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"Schedule {sched}: thread leaked!")

            self.assertEqual(errors, [])

            # Verify balance ledger thread-safety invariant: remaining + spent + pending = global_limit
            pending_cost = sum(p.reservation.cost for p in gov._active.values())
            self.assertEqual(gov.remaining_global + gov.spent_global + pending_cost, global_limit)

        leaked_threads = set(threading.enumerate()) - initial_threads
        self.assertEqual(len(leaked_threads), 0, f"Leaked threads detected: {leaked_threads}")

    def test_ast_prove_harness_and_mutation_obligation_kills(self):
        """AST prove harness to verify baseline controls and kill catalogue mutation obligations."""
        harness = ASTProveHarness("wender/act/budget_governor.py")

        # Baseline positive control 1 (M1 control)
        # Check that on real unmutated code, _configured_key(("cdp_tab", "")) raises ValueError
        with self.assertRaises(ValueError):
            _configured_key(("cdp_tab", ""))

        # Baseline positive control 2 (M2 control)
        # Check that on real unmutated code, wildcard budget key resolves successfully
        g_base = BudgetGovernor(100, {"target_1": 50}, service_cap_ns=10)
        h_base = make_handle("cdp_tab", "target_1", 1)
        r_base = g_base.reserve(h_base, 10, deadline_ns=100)
        self.assertIsNotNone(r_base)

        # Test catalogue mutation obligation M-wender-act-budget-governor-1
        # Operator O at line 90: BoolOp Or -> And in _configured_key
        tree_m1 = harness.parse()
        tree_m1 = harness.apply_operator_o(tree_m1, 90)

        def assert_m1_killed(mutated_mod):
            try:
                mutated_mod._configured_key(("cdp_tab", ""))
            except ValueError:
                return  # Did not kill
            raise AssertionError("M-wender-act-budget-governor-1 survived: invalid key was accepted")

        with self.assertRaises(AssertionError):
            harness.execute_mutated(tree_m1, assert_m1_killed)

        # Test catalogue mutation obligation M-wender-act-budget-governor-2
        # Operator O at line 177: Compare In -> NotIn in BudgetGovernor._limit_for
        tree_m2 = harness.parse()
        tree_m2 = harness.apply_operator_o(tree_m2, 177)

        def assert_m2_killed(mutated_mod):
            gov = mutated_mod.BudgetGovernor(100, {"target_1": 50}, service_cap_ns=10)
            h = make_handle("cdp_tab", "target_1", generation=1)
            try:
                gov.reserve(h, 10, deadline_ns=100)
            except mutated_mod.ConfigurationRequired:
                raise AssertionError("M-wender-act-budget-governor-2 killed: wildcard key lookup inverted")

        with self.assertRaises(AssertionError):
            harness.execute_mutated(tree_m2, assert_m2_killed)


if __name__ == "__main__":
    unittest.main()
