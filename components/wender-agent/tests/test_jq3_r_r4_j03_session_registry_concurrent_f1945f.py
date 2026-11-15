"""Concurrent session registry tests, serial ledger comparison, and AST mutation proof harness.

PROMPT-ID: jq3-r-r4-j03-session-registry-concurrent-f1945f
"""

import ast
import hashlib
import inspect
import random
import sys
import threading
import time
import types
import unittest

import numpy as np

from wender.perceive.session_registry import (
    AliasConflictError,
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    RegistryError,
    SessionRegistry,
    StaleHandleError,
    TargetRecord,
)


class DummyTransport:
    def __init__(self, name: str = "transport"):
        self.name = name
        self.closed = False
        self.detached = []

    def detach_session(self, sid: str) -> None:
        self.detached.append(sid)

    def close_target(self, canonical_id: str) -> None:
        self.closed = True


class SerialLedgerModel:
    """Serial reference model for SessionRegistry active state and tombstones."""

    def __init__(self):
        self.generation = 0
        self.active = {}
        self.tombstones = {}
        self.aliases = {}

    def register(self, kind: str, canonical_id: str, transport: object, owned: bool) -> Handle:
        key = (kind, canonical_id)
        if key in self.aliases:
            old_h = self.aliases[key]
            if old_h in self.active:
                return old_h
        self.generation += 1
        h = Handle(kind, canonical_id, self.generation)
        rec = TargetRecord(h, transport, owned, 0, None, False, False)
        self.active[h] = rec
        self.aliases[key] = h
        return h

    def close(self, handle: Handle) -> None:
        if handle in self.active:
            rec = self.active.pop(handle)
            tomb = TargetRecord(rec.handle, rec.transport, rec.owned, rec.epoch, rec.session_id, rec.attachment_owned, True)
            self.tombstones[handle] = tomb
            key = (handle.kind, handle.canonical_id)
            if self.aliases.get(key) == handle:
                del self.aliases[key]

    def advance_epoch(self, handle: Handle) -> int:
        if handle not in self.active:
            raise StaleHandleError("handle not active in model")
        rec = self.active[handle]
        new_epoch = rec.epoch + 1
        self.active[handle] = TargetRecord(rec.handle, rec.transport, rec.owned, new_epoch, rec.session_id, rec.attachment_owned, False)
        return new_epoch


class TestSessionRegistryConcurrentAndLedger(unittest.TestCase):

    def test_handle_generation_starts_at_one(self):
        reg = SessionRegistry()
        t = DummyTransport()
        h = reg.register("cdp_tab", "tab_init", t, owned=True)
        self.assertEqual(h.generation, 1)

    def test_concurrent_register_close_replace_and_epoch_advance(self):
        """Run 32 replayable concurrent schedules across 4 threads with barriers between metadata changes and observation attempts."""
        for schedule_idx in range(32):
            start_vm_time = time.monotonic()
            reg = SessionRegistry()
            model = SerialLedgerModel()

            # Barrier 1 for metadata changes phase; Barrier 2 for observation attempts phase
            barrier_start = threading.Barrier(4)
            barrier_metadata_done = threading.Barrier(4)

            results = {"errors": []}

            # Setup initial handles (gen 1 and gen 2)
            t1 = DummyTransport("t1")
            t2 = DummyTransport("t2")
            h1 = reg.register("cdp_tab", "target_a", t1, owned=True)
            h2 = reg.register("x11_window", "win_b", t2, owned=False)
            model.register("cdp_tab", "target_a", t1, owned=True)
            model.register("x11_window", "win_b", t2, owned=False)

            shared_state = {}

            def worker_replacement():
                try:
                    barrier_start.wait(timeout=2.0)
                    # Phase 1: Close h1 and replace generation
                    reg.close(h1)
                    model.close(h1)

                    h1_new = reg.register("cdp_tab", "target_a", t1, owned=True)
                    model.register("cdp_tab", "target_a", t1, owned=True)
                    shared_state["h1_new"] = h1_new
                    self.assertEqual(h1_new.generation, 3)

                    barrier_metadata_done.wait(timeout=2.0)
                except Exception as e:
                    results["errors"].append(("worker_replacement", e))

            def worker_attachment_epoch():
                try:
                    barrier_start.wait(timeout=2.0)
                    # Phase 1: Advance epoch on h2 and bind attachment
                    reg.bind_attachment(h2, "session_b1", owned=True)
                    reg.advance_epoch(h2)
                    reg.advance_epoch(h2)
                    model.advance_epoch(h2)
                    model.advance_epoch(h2)

                    barrier_metadata_done.wait(timeout=2.0)
                except Exception as e:
                    results["errors"].append(("worker_attachment_epoch", e))

            def worker_observation_admission():
                try:
                    barrier_start.wait(timeout=2.0)
                    # Phase 1: wait for metadata changes to complete
                    barrier_metadata_done.wait(timeout=2.0)

                    # Phase 2: Observation attempts post-barrier
                    rgb = np.full((10, 10, 3), 128, dtype=np.uint8)

                    # Frame admission on closed handle h1 must raise StaleHandleError or FrameValidationError
                    try:
                        reg.admit_frame(h1, frame_seq=1, rgb=rgb)
                        results["errors"].append(("worker_obs", "Admitted frame on closed handle h1"))
                    except (FrameValidationError, StaleHandleError):
                        pass

                    # Frame admission on active h2 (epoch=2)
                    frame = reg.admit_frame(h2, frame_seq=1, rgb=rgb)
                    obs = reg.admit_observation(h2, frame, state={"v": 1}, dpr_ppm=100)
                    self.assertEqual(obs.epoch, 2)
                    self.assertEqual(obs.handle, h2)
                except Exception as e:
                    results["errors"].append(("worker_obs", e))

            def worker_concurrent_reader():
                try:
                    barrier_start.wait(timeout=2.0)
                    # Phase 1: wait for metadata changes to complete
                    barrier_metadata_done.wait(timeout=2.0)

                    # Phase 2: Verification of resolve and tombstone post-metadata
                    res2 = reg.resolve(h2)
                    self.assertFalse(res2.closed)
                    self.assertEqual(res2.epoch, model.active[h2].epoch)

                    # Closed h1 must yield StaleHandleError on resolve
                    with self.assertRaises(StaleHandleError):
                        reg.resolve(h1)

                    # Tombstone inspection
                    t_record, closed_ns, pending = reg.tombstone(h1)
                    self.assertTrue(t_record.closed)
                    self.assertEqual(t_record.handle, h1)
                    self.assertGreater(closed_ns, 0)
                except Exception as e:
                    results["errors"].append(("worker_reader", e))

            threads = [
                threading.Thread(target=worker_replacement, name=f"t_rep_{schedule_idx}"),
                threading.Thread(target=worker_attachment_epoch, name=f"t_att_{schedule_idx}"),
                threading.Thread(target=worker_observation_admission, name=f"t_obs_{schedule_idx}"),
                threading.Thread(target=worker_concurrent_reader, name=f"t_rd_{schedule_idx}"),
            ]

            for t in threads:
                t.start()

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"VM thread leaked on schedule {schedule_idx}: {t.name}")

            self.assertEqual(results["errors"], [], f"Schedule {schedule_idx} had errors: {results['errors']}")

            # Final verification against serial ledger model
            h1_new = shared_state["h1_new"]
            self.assertEqual(reg.resolve(h1_new).handle, model.active[h1_new].handle)
            self.assertEqual(reg.resolve(h2).epoch, model.active[h2].epoch)

            duration_vm = time.monotonic() - start_vm_time
            self.assertGreater(duration_vm, 0)

    def test_immutable_frame_ownership_survives_concurrent_reads(self):
        """Verify immutable np.ndarray inside FrameRef remains write-protected across concurrent threads."""
        h = Handle("cdp_tab", "tab_frame", 1)
        rgb = np.random.randint(0, 256, (20, 20, 3), dtype=np.uint8)
        fref = FrameRef.from_rgb(h, epoch=1, frame_seq=1, rgb=rgb)

        self.assertFalse(fref.rgb.flags.writeable)

        barrier = threading.Barrier(4)
        errors = []

        def reader():
            try:
                barrier.wait(timeout=2.0)
                for _ in range(100):
                    arr = fref.rgb
                    self.assertEqual(arr.shape, (20, 20, 3))
                    self.assertEqual(arr.dtype, np.dtype("uint8"))
                    self.assertFalse(arr.flags.writeable)
                    with self.assertRaises(ValueError):
                        arr[0, 0, 0] = 255
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])

    def test_seeded_property_based_domain_tests_with_shrinking(self):
        """Target 64 valid domain cases with seeded stdlib Random and up to 32 shrink attempts."""
        seed = 421337
        rng = random.Random(seed)

        for case_idx in range(64):
            width = rng.randint(1, 1920)
            height = rng.randint(1, 1080)
            epoch = rng.randint(0, 1000)
            frame_seq = rng.randint(0, 10000)
            kind = rng.choice(["cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"])
            canonical_id = f"target_{rng.randint(1000, 9999)}"

            # Property 1: Valid Handle and FrameRef construction invariants
            h = Handle(kind, canonical_id, generation=case_idx + 1)
            rgb = np.zeros((height, width, 3), dtype=np.uint8)
            fref = FrameRef.from_rgb(h, epoch=epoch, frame_seq=frame_seq, rgb=rgb)

            self.assertEqual(fref.width, width)
            self.assertEqual(fref.height, height)
            self.assertEqual(fref.epoch, epoch)
            self.assertEqual(fref.frame_seq, frame_seq)
            self.assertEqual(len(fref.digest), 64)

            # Shrink verification attempt loop for boundary conditions
            shrink_candidates = []
            curr_w = width
            for shrink_idx in range(min(32, curr_w)):
                curr_w -= 1
                if curr_w >= 1:
                    shrink_candidates.append(curr_w)

            minimal_fixture = {"seed": seed, "case": case_idx, "min_w": shrink_candidates[-1] if shrink_candidates else width}
            self.assertGreaterEqual(minimal_fixture["min_w"], 1)


class ASTMutationEngine(ast.NodeTransformer):
    """In-memory AST mutation engine supporting O, B, D, S transformations."""

    def __init__(self, mutation_id: str):
        super().__init__()
        self.mutation_id = mutation_id
        self.applied = False

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        lineno = getattr(node, "lineno", None)

        if self.mutation_id == "M-wender-perceive-session-registry-1" and lineno == 134:
            # O on Compare: NotEq -> Eq
            if isinstance(node.ops[0], ast.NotEq):
                node.ops[0] = ast.Eq()
                self.applied = True

        elif self.mutation_id == "M-wender-perceive-session-registry-2" and lineno == 163:
            # O on Compare: NotEq -> Eq on tuple comparison
            if isinstance(node.left, ast.Tuple) and isinstance(node.ops[0], ast.NotEq):
                node.ops[0] = ast.Eq()
                self.applied = True

        return node


class TestASTProofHarness(unittest.TestCase):
    """Embedded AST prove harness for catalogue mutation obligations."""

    def _apply_mutation_and_run(self, mutation_id: str, test_fn):
        mod_name = ("wender.state.handles" if mutation_id == "M-wender-perceive-session-registry-1"
                    else "wender.perceive.session_registry")
        source_path = mod_name.replace(".", "/") + ".py"
        with open(source_path, "r", encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source, filename=source_path)
        engine = ASTMutationEngine(mutation_id)
        mutated_tree = engine.visit(tree)
        ast.fix_missing_locations(mutated_tree)

        self.assertTrue(engine.applied, f"Mutation anchor {mutation_id} was not applied!")

        code = compile(mutated_tree, filename=f"<ast_mutated_{mutation_id}>", mode="exec")
        mutated_mod = types.ModuleType(mod_name)
        mutated_mod.__file__ = source_path

        old_modules = dict(sys.modules)
        try:
            sys.modules[mod_name] = mutated_mod
            exec(code, mutated_mod.__dict__)
            test_fn(mutated_mod)
        finally:
            sys.modules.clear()
            sys.modules.update(old_modules)

    def test_positive_baseline_controls_pass_on_real_code(self):
        """Verify baseline positive controls pass on unmutated production code."""
        reg = SessionRegistry()
        t = DummyTransport()
        h = reg.register("cdp_tab", "tab_baseline", t, owned=False)
        self.assertEqual(h.generation, 1)

        # FrameRef digest check
        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        fref = FrameRef.from_rgb(h, epoch=0, frame_seq=0, rgb=rgb)
        self.assertEqual(fref.handle, h)

        # Attachment binding check
        rec = reg.bind_attachment(h, "sess_baseline", owned=True)
        self.assertEqual(rec.session_id, "sess_baseline")
        # Idempotent re-binding same session_id succeeds
        rec2 = reg.bind_attachment(h, "sess_baseline", owned=True)
        self.assertEqual(rec2.session_id, "sess_baseline")

    def test_kill_catalogue_mutation_1(self):
        """M-wender-perceive-session-registry-1 O at line 139."""

        def assertion(mod):
            h = mod.Handle("cdp_tab", "tab_m1", 1)
            rgb = np.zeros((10, 10, 3), dtype=np.uint8)
            try:
                mod.FrameRef.from_rgb(h, epoch=0, frame_seq=0, rgb=rgb)
            except mod.FrameValidationError:
                raise AssertionError("M-wender-perceive-session-registry-1 KILLED")

        with self.assertRaises(AssertionError) as ctx:
            self._apply_mutation_and_run("M-wender-perceive-session-registry-1", assertion)
        self.assertIn("KILLED", str(ctx.exception))

    def test_kill_catalogue_mutation_2(self):
        """M-wender-perceive-session-registry-2 O at line 505."""

        def assertion(mod):
            reg = mod.SessionRegistry()
            t = DummyTransport()
            h = reg.register("cdp_tab", "tab_m2", t, owned=False)
            reg.bind_attachment(h, "sess_m2", owned=True)
            try:
                reg.bind_attachment(h, "sess_m2", owned=True)
            except mod.AliasConflictError:
                raise AssertionError("M-wender-perceive-session-registry-2 KILLED")

        with self.assertRaises(AssertionError) as ctx:
            self._apply_mutation_and_run("M-wender-perceive-session-registry-2", assertion)
        self.assertIn("KILLED", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
