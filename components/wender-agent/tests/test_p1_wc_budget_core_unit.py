"""Offline C-budget/C-ports contracts against the real merged providers.

Host performance goals are targets, not VM assertions or measured evidence.
The only fake collaborators are in-memory external transport endpoints.
"""

from dataclasses import fields, replace
import hashlib
import importlib
import inspect
import sys
import threading
import types
import unittest


class TestBudgetCore(unittest.TestCase):
    def _balances(self, governor, handles):
        """Public ledger snapshot; never introspect governor implementation keys."""
        return (
            governor.remaining_global, governor.pending_count,
            governor.spent_global,
            tuple((h.kind, h.canonical_id, governor.remaining_for(h),
                   governor.spent_for(h)) for h in handles),
        )

    def _refused(self, governor, handles, error_type, operation):
        before = self._balances(governor, handles)
        with self.assertRaises(error_type):
            operation()
        self.assertEqual(self._balances(governor, handles), before)

    def _conserved(self, governor, global_limit, target_limits, active):
        """Independent ledger oracle from caller-tracked live reservations."""
        pending = sum(item.cost for item in active)
        self.assertEqual(global_limit,
                         governor.remaining_global + governor.spent_global + pending)
        self.assertEqual(governor.pending_count, len(active))
        for representative, limit in target_limits:
            # This oracle is deliberately not the governor's private key mapper.
            family = ("capsule_screen" if representative.kind in
                      ("capsule", "xvfb_screen") else representative.kind)
            held = sum(item.cost for item in active
                       if (item.handle.canonical_id == representative.canonical_id
                           and ("capsule_screen" if item.handle.kind in
                                ("capsule", "xvfb_screen") else item.handle.kind)
                           == family))
            self.assertEqual(limit, governor.remaining_for(representative)
                             + governor.spent_for(representative) + held)

    def test_public_contract_and_structural_handle(self):
        budget = importlib.import_module("wender.act.budget_governor")
        registry = importlib.import_module("wender.perceive.session_registry")
        self.assertEqual([item.name for item in fields(budget.Reservation)],
                         ["handle", "cost", "deadline_ns", "nonce"])
        self.assertEqual([item.name for item in fields(registry.Handle)],
                         ["kind", "canonical_id", "generation"])
        self.assertEqual(list(inspect.signature(budget.BudgetGovernor).parameters),
                         ["global_budget", "per_target_budget", "service_cap_ns", "clock", "telemetry"])
        self.assertEqual(list(inspect.signature(budget.BudgetGovernor.reserve).parameters),
                         ["self", "handle", "cost", "deadline_ns", "now_ns", "action_id"])
        self.assertEqual(list(inspect.signature(budget.BudgetGovernor.commit).parameters),
                         ["self", "reservation"])
        self.assertEqual(list(inspect.signature(budget.BudgetGovernor.cancel).parameters),
                         ["self", "reservation"])

        class StructuralHandle:
            def __init__(self, kind, canonical_id, generation):
                self.kind, self.canonical_id, self.generation = kind, canonical_id, generation

        g = budget.BudgetGovernor(4, 4, service_cap_ns=10)
        h = registry.Handle("cdp_tab", "port", 1)
        r = g.reserve(StructuralHandle("cdp_tab", "port", 1), 1,
                      deadline_ns=10, now_ns=0)
        self.assertEqual(r.handle, h)
        self._conserved(g, 4, [(h, 4)], [r])
        for bad in (0, True, -1, 1.5):
            self._refused(g, [h], ValueError,
                          lambda bad=bad: g.reserve(
                              StructuralHandle("cdp_tab", "port", bad), 1,
                              deadline_ns=10, now_ns=0))
        self._refused(g, [h], ValueError,
                      lambda: g.reserve(StructuralHandle("cdp_tab", " port", 1), 1,
                                        deadline_ns=10, now_ns=0))
        g.cancel(r)
        self._conserved(g, 4, [(h, 4)], [])

    def test_cost_deadline_and_account_refusals(self):
        budget = importlib.import_module("wender.act.budget_governor")
        h1 = budget.Handle("cdp_tab", "A", 1)
        h2 = budget.Handle("cdp_tab", "B", 1)
        g = budget.BudgetGovernor(3, 2, service_cap_ns=10)
        hold = g.reserve(h1, 2, deadline_ns=25, now_ns=15)
        self._conserved(g, 3, [(h1, 2), (h2, 2)], [hold])
        for cost in (True, False, 0, -1, 1.0, "1"):
            self._refused(g, [h1, h2], ValueError,
                          lambda cost=cost: g.reserve(h1, cost, deadline_ns=25, now_ns=15))
        self._refused(g, [h1, h2], budget.DeadlineInfeasible,
                      lambda: g.reserve(h2, 1, deadline_ns=24, now_ns=15))
        self._refused(g, [h1, h2], budget.BudgetExhausted,
                      lambda: g.reserve(h1, 1, deadline_ns=25, now_ns=15))
        self._refused(g, [h1, h2], budget.BudgetExhausted,
                      lambda: g.reserve(h2, 2, deadline_ns=25, now_ns=15))
        self._conserved(g, 3, [(h1, 2), (h2, 2)], [hold])
        r = g.reserve(h2, 1, deadline_ns=25, now_ns=15)
        self._conserved(g, 3, [(h1, 2), (h2, 2)], [hold, r])
        g.cancel(hold)
        g.commit(r)
        self._conserved(g, 3, [(h1, 2), (h2, 2)], [])
        absent = budget.BudgetGovernor(3, 3)
        self._refused(absent, [h1], budget.ConfigurationRequired,
                      lambda: absent.reserve(h1, 1, deadline_ns=50))

    def test_issued_identity_replays_and_unknown_send(self):
        budget = importlib.import_module("wender.act.budget_governor")
        h = budget.Handle("cdp_tab", "A", 1)
        g = budget.BudgetGovernor(3, 3, service_cap_ns=10)
        other = budget.BudgetGovernor(3, 3, service_cap_ns=10)
        r = g.reserve(h, 1, deadline_ns=10)
        copied = replace(r)
        foreign = other.reserve(h, 1, deadline_ns=10)
        for token in (copied, foreign):
            self._refused(g, [h], budget.ReservationInactive, lambda token=token: g.commit(token))
            self._refused(g, [h], budget.ReservationInactive, lambda token=token: g.cancel(token))
        self._conserved(g, 3, [(h, 3)], [r])
        g.cancel(r)  # Proved unsent; one credit is returned.
        g.cancel(r)
        g.release(r)
        self._conserved(g, 3, [(h, 3)], [])
        self._refused(g, [h], budget.ReservationInactive, lambda: g.cancel(copied))
        send = g.reserve(h, 2, deadline_ns=10)
        g.commit(send)  # Possible send precedes an UNKNOWN reply.
        outcome = "UNKNOWN"
        self.assertEqual(outcome, "UNKNOWN")
        g.cancel(send)  # Not evidence that an already possible send was unsent.
        self._refused(g, [h], budget.ReservationInactive, lambda: g.commit(send))
        self._refused(g, [h], budget.ReservationInactive,
                      lambda: g.settle(send, consumed=False))
        self.assertEqual((g.remaining_global, g.spent_global), (1, 2))
        self._conserved(g, 3, [(h, 3)], [])
        other.cancel(foreign)

    def test_generation_rollover_preflight_and_spent_fence(self):
        budget = importlib.import_module("wender.act.budget_governor")
        old = budget.Handle("cdp_tab", "same", 1)
        new = budget.Handle("cdp_tab", "same", 2)
        g = budget.BudgetGovernor(5, 5, service_cap_ns=10)
        sent = g.reserve(old, 3, deadline_ns=10)
        g.commit(sent)
        unsent = g.reserve(old, 2, deadline_ns=10)
        self._conserved(g, 5, [(old, 5)], [unsent])
        # Insufficient even after potential reclamation: no cancellation, no fence advance.
        self._refused(g, [old, new], budget.BudgetExhausted,
                      lambda: g.reserve(new, 3, deadline_ns=10))
        self._conserved(g, 5, [(old, 5)], [unsent])
        promoted = g.reserve(new, 2, deadline_ns=10)
        self._conserved(g, 5, [(new, 5)], [promoted])
        g.cancel(unsent)  # Retired, legitimate repeated cancellation is harmless.
        self._refused(g, [old, new], budget.StaleHandle,
                      lambda: g.reserve(old, 1, deadline_ns=10))
        g.commit(promoted)
        self.assertEqual(g.spent_global, 5)
        self._conserved(g, 5, [(new, 5)], [])
        self._refused(g, [old, new], budget.BudgetExhausted,
                      lambda: g.reserve(new, 1, deadline_ns=10))

    def test_alias_credit_is_shared_but_incarnations_are_distinct(self):
        budget = importlib.import_module("wender.act.budget_governor")
        capsule = budget.Handle("capsule", "surface", 1)
        screen = budget.Handle("xvfb_screen", "surface", 1)
        newer_screen = budget.Handle("xvfb_screen", "surface", 2)
        g = budget.BudgetGovernor(4, {("capsule", "surface"): 2}, service_cap_ns=10)
        cap_hold = g.reserve(capsule, 1, deadline_ns=10)
        screen_hold = g.reserve(screen, 1, deadline_ns=10)
        self._conserved(g, 4, [(capsule, 2), (screen, 2)], [cap_hold, screen_hold])
        self._refused(g, [capsule, screen], budget.BudgetExhausted,
                      lambda: g.reserve(capsule, 1, deadline_ns=10))
        moved = g.reserve(newer_screen, 1, deadline_ns=10)
        self.assertEqual(g.pending_count, 2)  # Only screen's old hold retired.
        self._conserved(g, 4, [(capsule, 2), (screen, 2)], [cap_hold, moved])
        g.cancel(screen_hold)
        g.commit(cap_hold)
        g.cancel(moved)
        self._conserved(g, 4, [(capsule, 2), (screen, 2)], [])
        self.assertEqual(g.spent_for(screen), 1)

    def test_action_id_clock_telemetry_and_settlement(self):
        budget = importlib.import_module("wender.act.budget_governor")
        h = budget.Handle("cdp_tab", "action", 1)
        event_log = []
        g = budget.BudgetGovernor(3, 3, service_cap_ns=10,
                                  clock=lambda: 20, telemetry=event_log)
        a = g.reserve(h, 1, action_id="action-A", deadline_ns=30)
        self.assertEqual([e["event"] for e in event_log], ["reserve"])
        self._conserved(g, 3, [(h, 3)], [a])
        g.settle(a, consumed=False)  # Positive proof of no send.
        self._conserved(g, 3, [(h, 3)], [])
        self._refused(g, [h], budget.ReservationInactive,
                      lambda: g.settle(a, consumed=False))
        g.cancel(a)  # Unlike settle, repeated cancel is an idempotent no-op.
        b = g.reserve(h, 2, action_id="action-B", deadline_ns=30)
        g.settle(b, consumed=True)  # An uncertain possible send stays charged.
        g.cancel(b)
        self._conserved(g, 3, [(h, 3)], [])
        self.assertEqual(g.spent_global, 2)
        self.assertEqual([e["event"] for e in event_log],
                         ["reserve", "settle", "reserve", "settle"])
        self.assertEqual([e["consumed"] for e in event_log if e["event"] == "settle"],
                         [False, True])
        self._refused(g, [h], ValueError,
                      lambda: g.reserve(h, 1, deadline_ns=30, action_id=" "))
        self._refused(g, [h], ValueError,
                      lambda: g.settle(b, consumed=1))
        lacking_clock = budget.BudgetGovernor(1, 1, service_cap_ns=10, telemetry=[])
        self._refused(lacking_clock, [h], budget.ConfigurationRequired,
                      lambda: lacking_clock.reserve(h, 1, deadline_ns=30, action_id="x"))
        lacking_events = budget.BudgetGovernor(1, 1, service_cap_ns=10, clock=lambda: 20)
        self._refused(lacking_events, [h], budget.ConfigurationRequired,
                      lambda: lacking_events.reserve(h, 1, deadline_ns=30, action_id="x"))

    def test_parallel_global_and_target_last_credit(self):
        budget = importlib.import_module("wender.act.budget_governor")
        for same_target in (False, True):
            count = 8
            g = budget.BudgetGovernor(8 if same_target else 1, 1, service_cap_ns=10)
            barrier = threading.Barrier(count)
            successes, denials, unexpected = [], [], []

            def worker(index):
                h = budget.Handle("cdp_tab", "same" if same_target else str(index), 1)
                try:
                    barrier.wait(timeout=10)
                    successes.append(g.reserve(h, 1, deadline_ns=10))
                except budget.BudgetExhausted:
                    denials.append(index)
                except BaseException as exc:
                    unexpected.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual([t for t in threads if t.is_alive()], [])
            self.assertEqual(unexpected, [])
            self.assertEqual((len(successes), len(denials)), (1, count - 1))
            representative = budget.Handle("cdp_tab", "same" if same_target else
                                            successes[0].handle.canonical_id, 1)
            limits = [(representative, 1)] if same_target else [
                (budget.Handle("cdp_tab", str(index), 1), 1) for index in range(count)]
            self._conserved(g, 8 if same_target else 1, limits, successes)
            winner = successes[0]
            g.commit(winner)
            self._conserved(g, 8 if same_target else 1, limits, [])
            self.assertEqual(g.spent_global, 1)

    def test_ports_frame_provenance_ram_and_cleanup(self):
        registry = importlib.import_module("wender.perceive.session_registry")
        import numpy as np  # Producer already depends on NumPy; no extra package.

        class ExternalTransport:
            def __init__(self):
                self.sends = []
                self.cleanup = []

            def send(self, command, body):
                self.sends.append((command, body))

            def close_target(self, identity):
                self.cleanup.append(identity)

        transport = ExternalTransport()
        manager = registry.SessionRegistry(clock=lambda: 17)
        h = manager.register("capsule", "port", transport, owned=True)
        self.assertEqual(tuple(vars(h)), ("kind", "canonical_id", "generation"))
        self.assertGreaterEqual(h.generation, 1)
        self.assertEqual(manager.bind_alias("xvfb_screen", "port", h), h)
        original = np.arange(3 * 4 * 3, dtype=np.uint8).reshape(3, 4, 3)
        raw = original.tobytes(order="C")
        source_sha256 = hashlib.sha256(raw).hexdigest()
        frame = manager.admit_frame(h, 1, original, epoch=0)
        original.fill(0)
        self.assertEqual((frame.epoch, frame.frame_seq, frame.width, frame.height),
                         (0, 1, 4, 3))
        self.assertEqual(frame.rgb.tobytes(order="C"), raw)
        self.assertEqual(frame.digest, source_sha256)
        self.assertFalse(frame.rgb.flags.writeable)
        with self.assertRaises(ValueError):
            frame.rgb.setflags(write=True)
        observation = manager.admit_observation(
            h, frame, state={"settled": False}, dpr_ppm=1_000_000)
        self.assertEqual((observation.handle, observation.epoch, observation.frame_seq),
                         (h, 0, 1))
        with self.assertRaises(registry.FrameValidationError):
            manager.admit_frame(h, 1, original, epoch=0)
        frame2 = manager.admit_frame(h, 2, original, epoch=0)
        with self.assertRaises(registry.FrameValidationError):
            manager.admit_observation(h, frame, state={}, dpr_ppm=1_000_000)
        self.assertEqual(manager.admit_observation(
            h, frame2, state={"settled": False}, dpr_ppm=1_000_000).frame_seq, 2)
        self.assertEqual(manager.advance_epoch(h), 1)
        with self.assertRaises(registry.FrameValidationError):
            manager.admit_observation(h, frame2, state={}, dpr_ppm=1_000_000)
        receipt = manager.stop(h)
        self.assertEqual(receipt.status, "stopped")
        self.assertEqual((transport.sends, transport.cleanup), ([], []))
        with self.assertRaises(registry.StaleHandleError):
            manager.resolve(h)
        manager.acknowledge_cleanup(h)
        reincarnated = manager.register("capsule", "port", transport, owned=True)
        self.assertGreater(reincarnated.generation, h.generation)

    def test_ports_real_writer_fence_possible_send_then_stop(self):
        budget = importlib.import_module("wender.act.budget_governor")
        scheduler = importlib.import_module("wender.act.window_scheduler")
        registry = importlib.import_module("wender.perceive.session_registry")

        class ExternalAdapter:
            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(payload)
                return "UNKNOWN"

        h = registry.Handle("cdp_tab", "writer", 1)
        g = budget.BudgetGovernor(1, 1, service_cap_ns=10)
        fence = scheduler.WriterFence(clock=lambda: 20)
        adapter = ExternalAdapter()
        ticket = registry.Ticket("action", h)
        fence.bind(h, "writer", epoch=0)
        fence.advance(20)
        permit = fence.authorize(ticket, "writer", deadline_ns=30, epoch=0)
        self.assertIsNotNone(permit)
        reservation = g.reserve(h, 1, deadline_ns=30, now_ns=20)
        with fence.metadata_lock:  # Simulated host writer transaction, no I/O under metadata lock.
            allowed = fence.check(h, ticket=ticket, permit=permit, now_ns=20)
            self.assertTrue(allowed)
            g.commit(reservation)
        response = adapter.send(b"command")  # Fake external transport, no network.
        self.assertEqual(response, "UNKNOWN")
        fence.stop(h)
        self.assertFalse(fence.check(h, ticket=ticket, permit=permit, now_ns=21))
        self.assertEqual(adapter.sent, [b"command"])
        self._refused(g, [h], budget.BudgetExhausted,
                      lambda: g.reserve(h, 1, deadline_ns=30, now_ns=20))
        g.cancel(reservation)  # STOP and UNKNOWN do not refund possible sends.
        self._conserved(g, 1, [(h, 1)], [])

    def test_semantic_mutants_are_counted_only_after_green_controls(self):
        actual = importlib.import_module("wender.act.budget_governor")
        source = inspect.getsource(actual)

        def global_limit(m):
            g = m.BudgetGovernor(1, 1, service_cap_ns=5)
            g.reserve(m.Handle("cdp_tab", "A", 1), 1, deadline_ns=5)
            try:
                g.reserve(m.Handle("cdp_tab", "B", 1), 1, deadline_ns=5)
            except m.BudgetExhausted:
                return g.remaining_global == 0 and g.pending_count == 1
            return False

        def target_limit(m):
            g = m.BudgetGovernor(2, 1, service_cap_ns=5)
            h = m.Handle("cdp_tab", "A", 1)
            g.reserve(h, 1, deadline_ns=5)
            try:
                g.reserve(h, 1, deadline_ns=5)
            except m.BudgetExhausted:
                return g.remaining_for(h) == 0 and g.pending_count == 1
            return False

        def refund_once(m):
            g = m.BudgetGovernor(2, 2, service_cap_ns=5)
            h = m.Handle("cdp_tab", "A", 1)
            r = g.reserve(h, 1, deadline_ns=5)
            g.cancel(r)
            return (g.remaining_global, g.remaining_for(h), g.pending_count) == (2, 2, 0)

        def spend_once(m):
            g = m.BudgetGovernor(2, 2, service_cap_ns=5)
            h = m.Handle("cdp_tab", "A", 1)
            r = g.reserve(h, 1, deadline_ns=5)
            g.commit(r)
            return (g.spent_global, g.spent_for(h), g.pending_count) == (1, 1, 0)

        def exact_cap(m):
            g = m.BudgetGovernor(1, 1, service_cap_ns=5)
            try:
                r = g.reserve(m.Handle("cdp_tab", "A", 1), 1,
                              deadline_ns=15, now_ns=10)
            except m.DeadlineInfeasible:
                return False
            return r.deadline_ns == 15 and g.pending_count == 1

        def cloned_token(m):
            g = m.BudgetGovernor(1, 1, service_cap_ns=5)
            r = g.reserve(m.Handle("cdp_tab", "A", 1), 1, deadline_ns=5)
            try:
                g.commit(replace(r))
            except m.ReservationInactive:
                return g.spent_global == 0 and g.pending_count == 1
            return False

        def stale_fence(m):
            g = m.BudgetGovernor(2, 2, service_cap_ns=5)
            g.reserve(m.Handle("cdp_tab", "A", 2), 1, deadline_ns=5)
            try:
                g.reserve(m.Handle("cdp_tab", "A", 1), 1, deadline_ns=5)
            except m.StaleHandle:
                return g.pending_count == 1 and g.remaining_global == 1
            return False

        # (name, unique source anchor, mutated anchor, semantic control)
        cases = [
            ("global_bypass", "if self._remaining_global + refundable < cost:",
             "if False and self._remaining_global + refundable < cost:", global_limit),
            ("target_bypass", "if remaining_target + refundable < cost:",
             "if False and remaining_target + refundable < cost:", target_limit),
            ("double_refund", "self._remaining_global += cost",
             "self._remaining_global += 2 * cost", refund_once),
            ("missing_spend", "self._spent_global += cost",
             "self._spent_global += 0", spend_once),
            ("deadline_off_by_one", "if deadline_ns - now_ns < self._service_cap_ns:",
             "if deadline_ns - now_ns <= self._service_cap_ns:", exact_cap),
            ("token_equal_not_identical", "pending.reservation is not reservation",
             "pending.reservation != reservation", cloned_token),
            ("stale_generation_bypass",
             "if old_generation is not None and h.generation < old_generation:",
             "if old_generation is not None and False and h.generation < old_generation:", stale_fence),
        ]
        # Every original positive/negative control must pass before any mutant.
        for name, _anchor, _replacement, control in cases:
            with self.subTest(positive_control=name):
                self.assertTrue(control(actual))

        killed, survived, invalid, missing, equivalent = [], [], [], [], []
        for name, anchor, replacement, control in cases:
            if source.count(anchor) < 1:
                missing.append(name)
                continue
            module_name = "_p1_wc_mutant_" + name
            mutant = types.ModuleType(module_name)
            mutant.__package__ = "wender.act"
            sys.modules[module_name] = mutant  # Required by frozen dataclasses.
            try:
                exec(compile(source.replace(anchor, replacement, 1),
                             actual.__file__, "exec"), mutant.__dict__)
                try:
                    valid_response = control(mutant)
                except Exception as exc:
                    invalid.append((name, type(exc).__name__))
                else:
                    if valid_response:
                        survived.append(name)
                    else:
                        killed.append(name)
            except Exception as exc:
                invalid.append((name, type(exc).__name__))
            finally:
                sys.modules.pop(module_name, None)
        valid_count = len(killed) + len(survived) + len(equivalent)
        self.assertEqual(missing, [], "missing anchors are never mutant kills")
        self.assertEqual(invalid, [], "runtime/import failures are not semantic kills")
        self.assertEqual(equivalent, [])
        self.assertEqual(survived, [], "semantic survivors: " + repr(survived))
        self.assertEqual((len(killed), valid_count), (7, 7))
        print("SEMANTIC_MUTANTS killed=7 valid=7 survived=0 equivalents=0 "
              "invalid=0 missing_anchors=0")


if __name__ == "__main__":
    unittest.main()
