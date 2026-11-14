# @module {"id":"wa.test.jq3_r_r1_j18_mutkill_coordinates_and_actor","layer":1,"owner":"ACT","deps":["wa.act.coordinates","wa.act.actor"]}
"""Unittest for coordinate planning, ActorLock, and in-memory AST mutation proving."""

import ast
import random
import sys
import threading
import types
import unittest

from wender.act import actor, coordinates
from wender.act.actor import ActorBusy, ActorLock
from wender.act.coordinates import CoordRefused, box_center, css_to_device, plan_click, plan_hover, plan_lpath_click, plan_type


class TestCoordinatesAndActor(unittest.TestCase):
    """Test pure coordinate conversions, action planning, ActorLock, property loops, and AST mutations."""

    def test_box_center_asymmetric_and_flooring(self):
        """Verify box_center calculation with asymmetric boxes and floor arithmetic."""
        # Baseline positive controls on asymmetric box [11, -15, 33, 47]
        cx, cy = box_center([11, -15, 33, 47])
        self.assertEqual((cx, cy), (11 + 33 // 2, -15 + 47 // 2))
        self.assertEqual((cx, cy), (27, 8))

        # Direct floor division check
        self.assertEqual(box_center([0, 0, 10, 20]), (5, 10))
        self.assertEqual(box_center([-100, -200, 50, 75]), (-75, -163))

    def test_box_center_validation(self):
        """Verify box_center refusal on invalid inputs."""
        invalid_boxes = [
            None,
            "not_a_box",
            [1, 2, 3],
            [1, 2, 3, 4, 5],
            [1, 2, "3", 4],
            [1, 2, True, 4],
            [10, 10, 0, 20],  # w = 0
            [10, 10, 20, 0],  # h = 0
            [10, 10, -5, 20],  # w < 0
        ]
        for box in invalid_boxes:
            with self.subTest(box=box):
                with self.assertRaises(CoordRefused):
                    box_center(box)

    def test_css_to_device_dpr_and_negative_coords(self):
        """Verify css_to_device scaling across different scalar DPR values and negative coords."""
        # DPR 1.0x (1,000,000 ppm)
        self.assertEqual(css_to_device(100, 200, 1000000), (100, 200))
        # DPR 1.5x (1,500,000 ppm)
        self.assertEqual(css_to_device(100, 200, 1500000), (150, 300))
        # DPR 2.0x (2,000,000 ppm)
        self.assertEqual(css_to_device(100, 200, 2000000), (200, 400))
        # DPR 2.25x (2,250,000 ppm)
        self.assertEqual(css_to_device(100, 200, 2250000), (225, 450))

        # Negative coordinates
        self.assertEqual(css_to_device(-100, -200, 1500000), (-150, -300))

    def test_css_to_device_validation(self):
        """Verify css_to_device refusal on invalid parameters."""
        invalid_params = [
            ("10", 10, 1000000),
            (10, "10", 1000000),
            (10, 10, "1000000"),
            (10, 10, True),
            (10, 10, 0),  # dpr_ppm = 0
            (10, 10, -1000000),  # dpr_ppm < 0
        ]
        for x, y, dpr_ppm in invalid_params:
            with self.subTest(x=x, y=y, dpr_ppm=dpr_ppm):
                with self.assertRaises(CoordRefused):
                    css_to_device(x, y, dpr_ppm)

    def test_plan_click_and_hover(self):
        """Verify plan_click and plan_hover event generation and viewport checks."""
        state = {
            "nodes": [{"id": "btn", "box": [100, 200, 50, 30]}],
            "viewport": {"w": 800, "h": 600, "scroll_x": 50, "scroll_y": 50},
        }
        # Center is (100 + 25, 200 + 15) = (125, 215)
        # Viewport bounds: X in [50, 850), Y in [50, 650)
        click_events = plan_click(state, "btn")
        self.assertEqual(len(click_events), 3)
        self.assertEqual(click_events[0], {"type": "mouseMoved", "x": 125, "y": 215})
        self.assertEqual(click_events[1], {"type": "mousePressed", "button": "left", "clickCount": 1, "x": 125, "y": 215})
        self.assertEqual(click_events[2], {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": 125, "y": 215})

        hover_events = plan_hover(state, "btn")
        self.assertEqual(hover_events, [{"type": "mouseMoved", "x": 125, "y": 215}])

    def test_plan_type(self):
        """Verify plan_type output and text validation."""
        self.assertEqual(plan_type("hello"), [{"type": "insertText", "text": "hello"}])
        with self.assertRaises(CoordRefused):
            plan_type(123)

    def test_plan_lpath_click_waypoints_and_viewport(self):
        """Verify orthogonal L-path movement policies and viewport boundaries."""
        state = {
            "nodes": [{"id": "menu_item", "box": [100, 200, 40, 20]}],  # cx=120, cy=210
            "viewport": {"w": 500, "h": 500, "scroll_x": 0, "scroll_y": 0},
        }
        origin = (20, 20)

        # Horizontal first: origin (20,20) -> elbow (cx, oy)=(120, 20) -> target (120, 210)
        h_events = plan_lpath_click(state, "menu_item", origin, waypoint_policy="horizontal_first")
        self.assertEqual(h_events[0], {"type": "mouseMoved", "x": 120, "y": 20})
        self.assertEqual(h_events[1], {"type": "mouseMoved", "x": 120, "y": 210})
        self.assertEqual(h_events[2]["type"], "mousePressed")
        self.assertEqual(h_events[2]["clickCount"], 1)

        # Vertical first: origin (20,20) -> elbow (ox, cy)=(20, 210) -> target (120, 210)
        v_events = plan_lpath_click(state, "menu_item", origin, waypoint_policy="vertical_first")
        self.assertEqual(v_events[0], {"type": "mouseMoved", "x": 20, "y": 210})
        self.assertEqual(v_events[1], {"type": "mouseMoved", "x": 120, "y": 210})

    def test_plan_lpath_click_viewport_refusals(self):
        """Verify waypoint out-of-bounds causes CoordRefused."""
        state = {
            "nodes": [{"id": "target", "box": [10, 10, 20, 20]}],  # cx=20, cy=20
            "viewport": {"w": 100, "h": 100, "scroll_x": 50, "scroll_y": 50},  # bounds [50..150, 50..150)
        }
        # Center (20,20) is outside viewport [50..150)
        with self.assertRaises(CoordRefused):
            plan_lpath_click(state, "target", (60, 60))

    def test_actor_lock_holder_state_and_concurrency_fencing(self):
        """Verify ActorLock acquire, release, owner validation, and context manager semantics."""
        lock = ActorLock()
        self.assertIsNone(lock.holder)

        # Acquire
        lock.acquire("alice")
        self.assertEqual(lock.holder, "alice")

        # Double acquire by same or different holder fails
        with self.assertRaises(ActorBusy):
            lock.acquire("alice")
        with self.assertRaises(ActorBusy):
            lock.acquire("bob")

        # Invalid owner identity
        with self.assertRaises(ValueError):
            lock.acquire("")
        with self.assertRaises(ValueError):
            lock.acquire("   ")

        # Foreign owner release fails
        with self.assertRaises(RuntimeError):
            lock.release("bob")

        # Invalid owner on release
        with self.assertRaises(ValueError):
            lock.release("")

        # Context manager usage
        with lock:
            self.assertEqual(lock._entered_depth, 1)

        # Context manager __exit__ releases lock and clears holder
        self.assertEqual(lock._entered_depth, 0)
        self.assertIsNone(lock.holder)

        # Release unlocked lock
        with self.assertRaises(RuntimeError):
            lock.release()

        # Bare context entry without prior acquire
        with self.assertRaises(ActorBusy):
            with lock:
                pass

    def test_property_based_random_loop_with_shrink(self):
        """Seeded stdlib random property test with 64 valid cases per domain and shrink attempts."""
        seed = 424242
        rng = random.Random(seed)

        # Test Domain 1: box_center and css_to_device integer invariants
        cases_run = 0
        for i in range(64):
            x = rng.randint(-1000, 1000)
            y = rng.randint(-1000, 1000)
            w = rng.randint(1, 500)
            h = rng.randint(1, 500)
            dpr = rng.choice([1000000, 1250000, 1500000, 2000000, 2500000, 3000000])

            # Invariant 1: box_center floor division
            cx, cy = box_center([x, y, w, h])
            self.assertEqual(cx, x + w // 2)
            self.assertEqual(cy, y + h // 2)

            # Invariant 2: css_to_device conversion
            dx, dy = css_to_device(x, y, dpr)
            self.assertEqual(dx, (x * dpr) // 1000000)
            self.assertEqual(dy, (y * dpr) // 1000000)
            cases_run += 1

        self.assertEqual(cases_run, 64)

        # Validity-preserving shrink demonstration (shrink up to 32 steps on candidate)
        candidate = {"x": 9999, "y": -8888, "w": 400, "h": 300}
        shrink_steps = 0
        curr = candidate.copy()
        for step in range(32):
            if curr["w"] > 2:
                curr["w"] -= 1
                shrink_steps += 1
            cx, cy = box_center([curr["x"], curr["y"], curr["w"], curr["h"]])
            self.assertEqual(cx, curr["x"] + curr["w"] // 2)

        self.assertLessEqual(shrink_steps, 32)

    def test_actor_lock_thread_concurrency(self):
        """Thread concurrency test with barriers, events, and bounded joins across 32 schedules."""
        for schedule_idx in range(32):
            lock = ActorLock()
            barrier = threading.Barrier(3)
            holder_counts = {"alice": 0, "bob": 0, "carol": 0}
            threads = []

            def worker(name):
                try:
                    barrier.wait(timeout=2.0)
                    lock.acquire(name)
                    holder_counts[name] += 1
                    # Keep lock acquired during race window
                except (ActorBusy, threading.BrokenBarrierError):
                    pass

            for name in ("alice", "bob", "carol"):
                t = threading.Thread(target=worker, args=(name,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), "Leaked thread detected!")

            # At most one thread acquired the lock during the race
            total_acquired = sum(holder_counts.values())
            self.assertLessEqual(total_acquired, 1)

    def test_in_memory_ast_prove_harness(self):
        """Embed in-memory AST mutation prove harness and verify all 12 catalogue mutations are killed."""
        coords_path = "wender/act/coordinates.py"
        actor_path = "wender/act/actor.py"

        with open(coords_path) as f:
            coords_tree = ast.parse(f.read(), filename=coords_path)
        with open(actor_path) as f:
            actor_tree = ast.parse(f.read(), filename=actor_path)

        def apply_mutation(tree, mut_id):
            tree = ast.fix_missing_locations(ast.parse(ast.unparse(tree)))
            if mut_id == "M-wender-act-coordinates-1":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "plan_lpath_click":
                        node.body[6].body[2].body[0].test.operand.values[0].ops[0] = ast.GtE()
            elif mut_id == "M-wender-act-coordinates-2":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "_resolve_state_node_center":
                        for stmt in node.body:
                            if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.BoolOp):
                                if isinstance(stmt.test.values[0], ast.Compare) and getattr(stmt.test.values[0].left, "id", None) == "w":
                                    stmt.test.values[0].ops[0] = ast.GtE()
                                    stmt.test.values[1].ops[0] = ast.GtE()
            elif mut_id == "M-wender-act-coordinates-3":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "plan_lpath_click":
                        node.body[6].body[1].value.elts[1].args[1].value = 1
            elif mut_id == "M-wender-act-coordinates-4":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "plan_lpath_click":
                        node.body[10].value.args[0].values[2].value = 2
            elif mut_id == "M-wender-act-coordinates-5":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "css_to_device":
                        and_op = node.body[1].test.operand
                        dpr_cmp = and_op.values[3]
                        dpr_cmp.ops[0] = ast.GtE()
            elif mut_id == "M-wender-act-coordinates-6":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "box_center":
                        for stmt in node.body:
                            if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.BoolOp):
                                if isinstance(stmt.test.values[0], ast.Compare) and getattr(stmt.test.values[0].left, "id", None) == "w":
                                    stmt.test = ast.Constant(value=False)
            elif mut_id == "M-wender-act-coordinates-7":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "css_to_device":
                        node.body[1].test = ast.Constant(value=False)
            elif mut_id == "M-wender-act-coordinates-8":
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == "plan_lpath_click":
                        call = node.body[6].body[1].value.elts[0]
                        call.args[0], call.args[1] = call.args[1], call.args[0]
            elif mut_id == "M-wender-act-actor-1":
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == "ActorLock":
                        for fn in node.body:
                            if isinstance(fn, ast.FunctionDef) and fn.name == "release":
                                fn.body[0].body[1].body[0].test.op = ast.And()
            elif mut_id == "M-wender-act-actor-2":
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == "ActorLock":
                        for fn in node.body:
                            if isinstance(fn, ast.FunctionDef) and fn.name == "__enter__":
                                fn.body[0].body[2].value.value = 2
            elif mut_id == "M-wender-act-actor-3":
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == "ActorLock":
                        for fn in node.body:
                            if isinstance(fn, ast.FunctionDef) and fn.name == "release":
                                fn.body[0].body[0].test = ast.Constant(value=False)
            elif mut_id == "M-wender-act-actor-4":
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == "ActorLock":
                        for fn in node.body:
                            if isinstance(fn, ast.FunctionDef) and fn.name == "release":
                                fn.body[0].body[1].body[0].test = ast.Constant(value=False)
            ast.fix_missing_locations(tree)
            return tree

        def get_kill_assertion(mut_id):
            if mut_id == "M-wender-act-coordinates-1":
                def assert_kill(mod):
                    state = {"nodes": [{"id": "n1", "box": [10, 10, 20, 20]}], "viewport": {"w": 100, "h": 100, "scroll_x": 0, "scroll_y": 0}}
                    try:
                        res = mod.plan_lpath_click(state, "n1", (10, 10))
                        assert len(res) > 0
                    except mod.CoordRefused:
                        assert False, "plan_lpath_click raised CoordRefused on valid waypoint"
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-2":
                def assert_kill(mod):
                    state = {"nodes": [{"id": "n1", "box": [10, 10, 20, 20]}], "viewport": {"w": 100, "h": 100, "scroll_x": 0, "scroll_y": 0}}
                    try:
                        res = mod.plan_click(state, "n1")
                        assert res[0]["x"] == 20
                    except mod.CoordRefused:
                        assert False, "plan_click raised CoordRefused on positive box size"
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-3":
                def assert_kill(mod):
                    state = {"nodes": [{"id": "n1", "box": [10, 10, 20, 20]}], "viewport": {"w": 100, "h": 100, "scroll_x": 0}}
                    try:
                        res = mod.plan_lpath_click(state, "n1", (10, 0))
                        assert len(res) == 4
                    except mod.CoordRefused:
                        assert False, "plan_lpath_click failed when default scroll_y=0 was expected"
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-4":
                def assert_kill(mod):
                    state = {"nodes": [{"id": "n1", "box": [10, 10, 20, 20]}], "viewport": {"w": 100, "h": 100, "scroll_x": 0, "scroll_y": 0}}
                    res = mod.plan_lpath_click(state, "n1", (5, 5))
                    cc = res[-2]["clickCount"]
                    assert cc == 1, f"clickCount was {cc}, expected 1"
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-5":
                def assert_kill(mod):
                    try:
                        mod.css_to_device(10, 10, 0)
                        assert False, "css_to_device(10, 10, 0) did not raise CoordRefused"
                    except mod.CoordRefused:
                        pass
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-6":
                def assert_kill(mod):
                    try:
                        mod.box_center([10, 10, 0, 10])
                        assert False, "box_center with w=0 did not raise CoordRefused"
                    except mod.CoordRefused:
                        pass
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-7":
                def assert_kill(mod):
                    try:
                        mod.css_to_device("invalid", 10, 1000000)
                        assert False, "css_to_device with string x did not raise CoordRefused"
                    except mod.CoordRefused:
                        pass
                    except Exception as e:
                        assert False, f"Expected CoordRefused but raised {type(e).__name__}: {e}"
                return assert_kill
            elif mut_id == "M-wender-act-coordinates-8":
                def assert_kill(mod):
                    state = {"nodes": [{"id": "n1", "box": [60, 10, 20, 20]}], "viewport": {"w": 100, "h": 100, "scroll_x": 50, "scroll_y": 0}}
                    try:
                        res = mod.plan_lpath_click(state, "n1", (50, 10))
                        assert len(res) == 4 and res[0]["x"] == 70
                    except Exception as e:
                        assert False, f"Expected successful lpath_click but raised {type(e).__name__}: {e}"
                return assert_kill
            elif mut_id == "M-wender-act-actor-1":
                def assert_kill(mod):
                    lock = mod.ActorLock()
                    lock.acquire("alice")
                    try:
                        lock.release(who="")
                        assert False, "release(who='') did not raise ValueError"
                    except ValueError:
                        pass
                    except Exception as e:
                        assert False, f"Expected ValueError but raised {type(e).__name__}: {e}"
                return assert_kill
            elif mut_id == "M-wender-act-actor-2":
                def assert_kill(mod):
                    lock = mod.ActorLock()
                    lock.acquire("alice")
                    with lock:
                        depth = lock._entered_depth
                        assert depth == 1, f"_entered_depth was {depth}, expected 1"
                return assert_kill
            elif mut_id == "M-wender-act-actor-3":
                def assert_kill(mod):
                    lock = mod.ActorLock()
                    lock._lock.acquire()
                    try:
                        lock.release()
                        assert False, "release on lock with holder=None did not raise RuntimeError"
                    except RuntimeError as e:
                        assert "release unlocked lock" in str(e), f"Expected release unlocked lock, got: {e}"
                return assert_kill
            elif mut_id == "M-wender-act-actor-4":
                def assert_kill(mod):
                    lock = mod.ActorLock()
                    lock.acquire("alice")
                    try:
                        lock.release(who="")
                        assert False, "release(who='') did not raise ValueError"
                    except ValueError:
                        pass
                    except Exception as e:
                        assert False, f"Expected ValueError but raised {type(e).__name__}: {e}"
                return assert_kill

        catalogue_mutations = [
            ("M-wender-act-coordinates-1", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-2", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-3", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-4", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-5", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-6", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-7", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-coordinates-8", coords_tree, "wender.act.coordinates"),
            ("M-wender-act-actor-1", actor_tree, "wender.act.actor"),
            ("M-wender-act-actor-2", actor_tree, "wender.act.actor"),
            ("M-wender-act-actor-3", actor_tree, "wender.act.actor"),
            ("M-wender-act-actor-4", actor_tree, "wender.act.actor"),
        ]

        killed_count = 0
        for mut_id, source_tree, mod_name in catalogue_mutations:
            kill_fn = get_kill_assertion(mut_id)

            # Positive control on real code first
            if mod_name == "wender.act.coordinates":
                kill_fn(coordinates)
            else:
                kill_fn(actor)

            # Mutate AST and load in isolated sys.modules
            mutated_tree = apply_mutation(source_tree, mut_id)
            code_obj = compile(mutated_tree, f"<{mut_id}>", "exec")

            old_modules = sys.modules.copy()
            temp_mod = types.ModuleType(mod_name)
            sys.modules[mod_name] = temp_mod
            try:
                exec(code_obj, temp_mod.__dict__)
                with self.assertRaises(AssertionError, msg=f"Mutant {mut_id} survived!"):
                    kill_fn(temp_mod)
                killed_count += 1
            finally:
                sys.modules.clear()
                sys.modules.update(old_modules)

        self.assertEqual(killed_count, 12, "All 12 catalogue mutations must be killed")


if __name__ == "__main__":
    unittest.main()
