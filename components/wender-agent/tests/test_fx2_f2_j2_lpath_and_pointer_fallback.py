# @module {"id":"wa.tests.test_fx2_f2_j2_lpath_and_pointer_fallback","layer":2,"owner":"ACT","deps":["wa.act.cdp_executor","wa.perceive.ax"],"book":"HB-WA-s2-act"}
"""@module wa.tests.test_fx2_f2_j2_lpath_and_pointer_fallback - Unit tests pinning hover, lpath_click and reversible pointer fallback.

Pins requirements for:
- Production hover and lpath_click operations.
- Mirrored, vertical, degenerate integer CSS corridors, odd extents, and visible centers from clipped origins.
- Orthogonal retained legs, no duplicate moves, matching final center and press/release coordinates, no DPR scaling.
- Fresh snapshot coupling for reversible fallback (unchanged JS click fails without permission, permitted fallback requires changed post-state, irreversible actions never use pointer fallback).
- Rejection of malformed geometry before mouse dispatch and swapped-elbow mutants.
"""
import unittest

from wender.act.cdp_executor import run_ax_action, ActionError
from wender.perceive import ax


def _make_node(node_id="target", role="button", name="Send", box=None, attrs=None):
    if box is None:
        box = [90, 70, 20, 20]
    if attrs is None:
        attrs = ()
    return {
        "id": node_id,
        "backend_id": 101,
        "role": role,
        "name": name,
        "depth": 1,
        "ancestors": ("main",),
        "attrs": attrs,
        "ordinal": 0,
        "cardinality": 1,
        "box": box,
    }


class FakeCdp:
    """Offline recording FakeCdp transport with corridor tracking and scripted replies."""

    def __init__(self, initial_origin=None, on_mouse_released=None):
        self.calls = []
        self.menu_closed = False
        self.checked = "false"
        self.current_pos = initial_origin
        self.on_mouse_released = on_mouse_released

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})

        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-101"}}
        if method in ("Runtime.callFunctionOn", "DOM.scrollIntoViewIfNeeded"):
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [90, 70, 110, 70, 110, 90, 90, 90]}}
        if method == "Input.dispatchMouseEvent":
            mtype = payload.get("type")
            x = payload.get("x")
            y = payload.get("y")

            if mtype == "mouseMoved" and x is not None and y is not None:
                if self.current_pos is not None:
                    x0, y0 = self.current_pos
                    x1, y1 = x, y
                    # Parent menu row zone crossing check: y in [30, 65] outside target corridor [90, 110].
                    y_min, y_max = min(y0, y1), max(y0, y1)
                    crosses_parent_row = (y_min <= 65 and y_max >= 30)
                    if (x0 < 90 or x0 > 110 or x1 < 90 or x1 > 110) and crosses_parent_row:
                        self.menu_closed = True
                self.current_pos = (x, y)

            elif mtype == "mouseReleased" and x is not None and y is not None:
                if not self.menu_closed and (x, y) == (100, 80):
                    self.checked = "true"
                if callable(self.on_mouse_released):
                    self.on_mouse_released()
            return {}

        return {}


def noop_sleep(seconds):
    pass


class TestFx2F2J2LpathAndPointerFallback(unittest.TestCase):
    """Test suite for hover, lpath_click corridor geometry, and reversible pointer fallback."""

    # -------------------------------------------------------------------------
    # PASSING CONTROLS (Ordinary test methods, no expectedFailure)
    # -------------------------------------------------------------------------

    def test_control_node_fixture_and_odd_extent_center(self):
        """Passing control: verify node fixture structure and integer floor division center calculation."""
        node = _make_node(box=[91, 71, 21, 21])
        box = node["box"]
        cx = box[0] + box[2] // 2
        cy = box[1] + box[3] // 2
        self.assertEqual((cx, cy), (101, 81))
        self.assertIsInstance(cx, int)
        self.assertIsInstance(cy, int)

    def test_control_ordinary_ax_click_success(self):
        """Passing control: ordinary AX click with changing state succeeds on ax path."""
        fake_cdp = FakeCdp()
        state_holder = ["false"]

        def read_nodes():
            return [_make_node(attrs=(("checked", state_holder[0]),))]

        nodes = [_make_node(attrs=(("checked", "false"),))]

        def on_call():
            state_holder[0] = "true"

        fake_cdp.on_mouse_released = on_call  # not called for ax path, state changed by JS function in real app or mock
        # Simulate JS click causing state change on read_nodes:
        def read_nodes_changing():
            # simulate state changed after iteration
            if len(fake_cdp.calls) > 0:
                return [_make_node(attrs=(("checked", "true"),))]
            return [_make_node(attrs=(("checked", "false"),))]

        receipt = run_ax_action(
            fake_cdp,
            "sid-control",
            nodes,
            {"op": "click", "target": "target", "expect": {"target_states": {"checked": "true"}}},
            read_nodes_changing,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=False,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["path"], "ax")

    def test_control_fake_cdp_corridor_tracker(self):
        """Passing control: verify FakeCdp corridor tracking detects vertical-first parent row crossing."""
        cdp = FakeCdp(initial_origin=(20, 20))
        # Horizontal-first move to elbow (100, 20): does not cross y=30..65 at x < 90
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 100, "y": 20})
        self.assertFalse(cdp.menu_closed)

        # Vertical entry to target (100, 80): stays inside corridor x in [90, 110]
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 100, "y": 80})
        self.assertFalse(cdp.menu_closed)

        # Reset and test vertical-first move (20, 20) -> (20, 80): crosses y=30..65 at x=20 (< 90) -> closes menu
        cdp2 = FakeCdp(initial_origin=(20, 20))
        cdp2.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 20, "y": 80})
        self.assertTrue(cdp2.menu_closed)

    # -------------------------------------------------------------------------
    # DEMONSTRATED FAILING CONTRACTS (@unittest.expectedFailure)
    # -------------------------------------------------------------------------

    def test_lpath_click_right_opening_corridor(self):
        """Contract: lpath_click for right-opening submenu (origin 20,20 -> elbow 100,20 -> target 100,80)."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(20, 20))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (20, 20),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-right",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 4)

        move1, move2, press, release = mouse_calls
        self.assertEqual(move1["params"], {"type": "mouseMoved", "x": 100, "y": 20})
        self.assertEqual(move2["params"], {"type": "mouseMoved", "x": 100, "y": 80})
        self.assertEqual(
            press["params"],
            {"type": "mousePressed", "x": 100, "y": 80, "button": "left", "clickCount": 1},
        )
        self.assertEqual(
            release["params"],
            {"type": "mouseReleased", "x": 100, "y": 80, "button": "left", "clickCount": 1},
        )

        # Check orthogonal consecutive legs
        self.assertEqual(move1["params"]["y"], 20)  # dx=80, dy=0
        self.assertEqual(move2["params"]["x"], 100)  # dx=0, dy=60

        # No synthetic JS function call on lpath_click
        runtime_calls = [c for c in fake_cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(runtime_calls, [])

    def test_lpath_click_left_opening_corridor_mirrored(self):
        """Contract: lpath_click for left-opening submenu (origin 180,20 -> elbow 100,20 -> target 100,80)."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(180, 20))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (180, 20),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-left",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 4)

        move1, move2, press, release = mouse_calls
        self.assertEqual(move1["params"], {"type": "mouseMoved", "x": 100, "y": 20})
        self.assertEqual(move2["params"], {"type": "mouseMoved", "x": 100, "y": 80})
        self.assertEqual(
            press["params"],
            {"type": "mousePressed", "x": 100, "y": 80, "button": "left", "clickCount": 1},
        )
        self.assertEqual(
            release["params"],
            {"type": "mouseReleased", "x": 100, "y": 80, "button": "left", "clickCount": 1},
        )

    @unittest.expectedFailure
    def test_lpath_click_vertical_corridor(self):
        """Contract: lpath_click with vertically aligned origin (100,20) omits duplicate waypoint."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(100, 20))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (100, 20),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-vert",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        # Origin (100, 20) -> elbow (100, 20) is duplicate -> omitted.
        # Sequence: mouseMoved to (100, 80), mousePressed at (100, 80), mouseReleased at (100, 80).
        self.assertEqual(len(mouse_calls), 3)
        move, press, release = mouse_calls
        self.assertEqual(move["params"], {"type": "mouseMoved", "x": 100, "y": 80})
        self.assertEqual(press["params"]["x"], 100)
        self.assertEqual(press["params"]["y"], 80)
        self.assertEqual(release["params"]["x"], 100)
        self.assertEqual(release["params"]["y"], 80)

    def test_lpath_click_degenerate_corridor(self):
        """Contract: lpath_click with origin matching target center (100,80) omits duplicate moves."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(100, 80))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (100, 80),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-degen",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        # Origin equals center: waypoints (100,80) are duplicates. Press and release directly at (100,80).
        for call in mouse_calls:
            self.assertEqual((call["params"]["x"], call["params"]["y"]), (100, 80))

    def test_lpath_click_clipped_origin_visible_center(self):
        """Contract: lpath_click from clipped origin (-20, 20) to visible center (100, 80)."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(-20, 20))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (-20, 20),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-clipped",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 4)
        move1, move2, press, release = mouse_calls
        self.assertEqual(move1["params"], {"type": "mouseMoved", "x": 100, "y": 20})
        self.assertEqual(move2["params"], {"type": "mouseMoved", "x": 100, "y": 80})

    def test_lpath_click_odd_extents_floor_division_no_dpr_scaling(self):
        """Contract: odd extent box [91,71,21,21] uses integer floor division (101,81) without DPR scaling."""
        node = _make_node(box=[91, 71, 21, 21])
        fake_cdp = FakeCdp(initial_origin=(21, 21))

        def read_nodes():
            return [_make_node(box=[91, 71, 21, 21], attrs=(("checked", "true"),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (21, 21),
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-odd",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        move1, move2, press, release = mouse_calls
        # Target center: 91 + 21//2 = 101, 71 + 21//2 = 81
        self.assertEqual(move1["params"], {"type": "mouseMoved", "x": 101, "y": 21})
        self.assertEqual(move2["params"], {"type": "mouseMoved", "x": 101, "y": 81})
        self.assertEqual(press["params"]["x"], 101)
        self.assertEqual(press["params"]["y"], 81)
        self.assertEqual(release["params"]["x"], 101)
        self.assertEqual(release["params"]["y"], 81)
        # Verify non-float integer coordinates
        self.assertIsInstance(press["params"]["x"], int)
        self.assertIsInstance(press["params"]["y"], int)

    @unittest.expectedFailure
    def test_hover_action_emits_movement_without_button_press(self):
        """Contract: hover action emits mouseMoved to target center without mousePressed or mouseReleased."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(20, 20))

        def read_nodes():
            return [_make_node(attrs=(("expanded", "true"),))]

        action = {
            "op": "hover",
            "target": "target",
            "origin": (20, 20),
            "expect": {"target_states": {"expanded": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-hover",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        # Hover sequence: mouseMoved waypoints only, NO mousePressed or mouseReleased
        types = [c["params"]["type"] for c in mouse_calls]
        self.assertIn("mouseMoved", types)
        self.assertNotIn("mousePressed", types)
        self.assertNotIn("mouseReleased", types)

    def test_unchanged_js_click_fails_without_coordinate_permission(self):
        """WP1-03 defect test: JS click leaving snapshot unchanged must fail when allow_coordinates=False."""
        cdp = FakeCdp()

        initial_node = _make_node(
            role="menuitem",
            name="Potencia",
            attrs=(("checked", "false"),),
            box=[10, 20, 100, 40],
        )

        def read_nodes():
            return [
                _make_node(
                    role="menuitem",
                    name="Potencia",
                    attrs=(("checked", "false"),),
                    box=[10, 20, 100, 40],
                )
            ]

        receipt = run_ax_action(
            cdp,
            "sid-menu-nocoord",
            [initial_node],
            {
                "op": "click",
                "target": "target",
                "irreversible": False,
            },
            read_nodes,
            retries=3,
            allow_coordinates=False,
            sleep=noop_sleep,
        )

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertEqual(receipt["attempts"], 3)
        self.assertEqual(receipt["path"], "ax")
        self.assertEqual(receipt["pre_sha"], receipt["post_sha"])

    def test_unchanged_js_click_triggers_permitted_fallback_requiring_changed_post_state(self):
        """WP1-03 defect test: JS click leaving snapshot unchanged falls back to coordinates and requires post-state change."""
        released = False

        def on_mouse_released():
            nonlocal released
            released = True

        cdp = FakeCdp(on_mouse_released=on_mouse_released)

        initial_node = _make_node(
            role="menuitem",
            name="Potencia",
            attrs=(("checked", "false"),),
            box=[10, 20, 100, 40],
        )

        def read_nodes():
            state = "true" if released else "false"
            return [
                _make_node(
                    role="menuitem",
                    name="Potencia",
                    attrs=(("checked", state),),
                    box=[10, 20, 100, 40],
                )
            ]

        receipt = run_ax_action(
            cdp,
            "sid-menu-fallback",
            [initial_node],
            {
                "op": "click",
                "target": "target",
                "irreversible": False,
            },
            read_nodes,
            retries=3,
            allow_coordinates=True,
            sleep=noop_sleep,
        )

        self.assertEqual(receipt["path"], "coordinates")
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["ok"], 1)
        self.assertNotEqual(receipt["pre_sha"], receipt["post_sha"])

        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(
            [c["params"]["type"] for c in mouse_calls],
            ["mouseMoved", "mousePressed", "mouseReleased"],
        )
        for call in mouse_calls:
            self.assertEqual((call["params"]["x"], call["params"]["y"]), (60, 40))

    @unittest.expectedFailure
    def test_irreversible_action_never_uses_pointer_fallback(self):
        """Contract: irreversible actions must never use pointer fallback even if allow_coordinates=True."""
        cdp = FakeCdp()

        initial_node = _make_node(
            role="button",
            name="Delete Account",
            attrs=(("disabled", "false"),),
            box=[10, 20, 100, 40],
        )

        def read_nodes():
            return [
                _make_node(
                    role="button",
                    name="Delete Account",
                    attrs=(("disabled", "false"),),
                    box=[10, 20, 100, 40],
                )
            ]

        receipt = run_ax_action(
            cdp,
            "sid-irreversible",
            [initial_node],
            {
                "op": "click",
                "target": "target",
                "irreversible": True,
            },
            read_nodes,
            retries=2,
            allow_coordinates=True,
            sleep=noop_sleep,
        )

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["path"], "ax")
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse_calls, [])

    def test_swapped_elbow_mutant_causes_menu_closure_and_failed_verification(self):
        """Contract: swapped elbow waypoint policy (vertical-first) crosses parent row, closing menu and failing verification."""
        node = _make_node(box=[90, 70, 20, 20])
        fake_cdp = FakeCdp(initial_origin=(20, 20))

        def read_nodes():
            return [_make_node(attrs=(("checked", fake_cdp.checked),))]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (20, 20),
            "waypoint_policy": "vertical_first",  # Swapped elbow mutant proposal
            "expect": {"target_states": {"checked": "true"}},
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-lpath-swapped",
            [node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertTrue(fake_cdp.menu_closed)
        self.assertEqual(fake_cdp.checked, "false")

    @unittest.expectedFailure
    def test_reject_malformed_geometry_before_mouse_dispatch(self):
        """Contract: malformed geometry (e.g. negative box dimensions or float coordinates) is rejected before mouse dispatch."""
        fake_cdp = FakeCdp()
        invalid_box_node = _make_node(box=[90, 70, -20, 20])

        def read_nodes():
            return [invalid_box_node]

        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (20.5, 20),  # Float origin invalid
            "irreversible": False,
        }

        receipt = run_ax_action(
            fake_cdp,
            "sid-malformed",
            [invalid_box_node],
            action,
            read_nodes,
            retries=1,
            sleep=noop_sleep,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 0)
        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(mouse_calls, [])


if __name__ == "__main__":
    unittest.main()
