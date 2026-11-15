"""Pins for proposed lpath_click action and corridor navigation in wender/act/cdp_executor.py.

Contract / Proposal:
- Action op: 'lpath_click'
- Action origin: integer CSS coordinate pair, e.g. origin=(20, 20) or origin=(180, 20)
- Target box: [90, 70, 20, 20], centre (100, 80)
- Right-opening submenu: origin (20, 20) -> elbow (100, 20) -> target (100, 80)
- Left-opening submenu: origin (180, 20) -> elbow (100, 20) -> target (100, 80)
- Movement policy: horizontal-first orthogonal segments along menu row before vertical entry into submenu corridor
- Menu closure: crossing unrelated parent row before corridor entry closes menu and fails post-state verification
"""
import unittest

from wender.act.cdp_executor import run_ax_action


def _make_target_node(**overrides):
    node = {
        "id": "target",
        "backend_id": 101,
        "role": "button",
        "name": "Send",
        "depth": 1,
        "ancestors": ("main",),
        "attrs": (),
        "ordinal": 0,
        "cardinality": 1,
        "box": [90, 70, 20, 20],
    }
    node.update(overrides)
    return node


class FakeCdp:
    """Recording FakeCdp transport with scripted replies and menu corridor state tracking."""

    def __init__(self, initial_origin=None):
        self.calls = []
        self.menu_closed = False
        self.checked = "false"
        self.current_pos = initial_origin

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})

        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
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
                    # Unrelated parent row zone: y spanning [30, 65] outside target corridor [90, 110].
                    # If vertical move crosses y=30..65 at x < 90 or x > 110, menu closes.
                    y_min, y_max = min(y0, y1), max(y0, y1)
                    crosses_parent_row = (y_min <= 65 and y_max >= 30)
                    if (x0 < 90 or x0 > 110 or x1 < 90 or x1 > 110) and crosses_parent_row:
                        self.menu_closed = True
                self.current_pos = (x, y)

            elif mtype == "mouseReleased" and x is not None and y is not None:
                if not self.menu_closed and (x, y) == (100, 80):
                    self.checked = "true"
            return {}

        return {}


class TestJq1A7LpathCorridor(unittest.TestCase):
    """Test suite pinning lpath_click action proposals and corridor navigation."""

    def test_nominal_node_fixture_and_fake_cdp(self):
        """Nominal control: verify node fixture structure, box centre, and FakeCdp tracking logic."""
        node = _make_target_node()
        self.assertEqual(node["id"], "target")
        self.assertEqual(node["backend_id"], 101)
        self.assertEqual(node["role"], "button")
        self.assertEqual(node["name"], "Send")
        self.assertEqual(node["depth"], 1)
        self.assertEqual(node["ancestors"], ("main",))
        self.assertEqual(node["attrs"], ())
        self.assertEqual(node["ordinal"], 0)
        self.assertEqual(node["cardinality"], 1)

        box = node["box"]
        self.assertEqual(box, [90, 70, 20, 20])
        cx = box[0] + box[2] // 2
        cy = box[1] + box[3] // 2
        self.assertEqual((cx, cy), (100, 80))

        fake_cdp = FakeCdp(initial_origin=(20, 20))
        res_node = fake_cdp.send("DOM.resolveNode", {"backendNodeId": 101})
        self.assertEqual(res_node, {"object": {"objectId": "obj"}})

        box_reply = fake_cdp.send("DOM.getBoxModel", {"backendNodeId": 101})
        self.assertEqual(box_reply, {"model": {"border": [90, 70, 110, 70, 110, 90, 90, 90]}})

        # Test vertical-first segment closes menu in FakeCdp logic
        fake_cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 20, "y": 80})
        self.assertTrue(fake_cdp.menu_closed)

    def test_lpath_click_right_opening_submenu(self):
        # Defect: lpath_click op and origin coordinate proposal are absent in cdp_executor.OPS and run_ax_action
        node = _make_target_node()
        fake_cdp = FakeCdp(initial_origin=(20, 20))

        def read_nodes():
            return [_make_target_node(attrs=(("checked", fake_cdp.checked),))]

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
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        # Expected sequence:
        # 1. mouseMoved to elbow (100, 20)
        # 2. mouseMoved to target centre (100, 80)
        # 3. mousePressed at (100, 80)
        # 4. mouseReleased at (100, 80)
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

        # Check orthogonal consecutive movement segments:
        # Segment 1: (20, 20) -> (100, 20) (horizontal: dx=80, dy=0)
        # Segment 2: (100, 20) -> (100, 80) (vertical: dx=0, dy=60)
        self.assertEqual(move1["params"]["y"], 20)  # y unchanged during horizontal move
        self.assertEqual(move2["params"]["x"], 100)  # x unchanged during vertical move

        # Require no duplicate synthetic JS click
        runtime_calls = [c for c in fake_cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        self.assertEqual(runtime_calls, [])

    def test_lpath_click_left_opening_submenu(self):
        # Defect: lpath_click op with left-opening origin 180,20 absent in cdp_executor
        node = _make_target_node()
        fake_cdp = FakeCdp(initial_origin=(180, 20))

        def read_nodes():
            return [_make_target_node(attrs=(("checked", fake_cdp.checked),))]

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
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        mouse_calls = [c for c in fake_cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse_calls), 4)

        move1, move2, press, release = mouse_calls
        # Left-opening origin 180,20 -> elbow (100, 20) -> target (100, 80)
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

    def test_swapped_elbow_causes_closure_and_failed_verification(self):
        # Defect: policy error or vertical-first movement crossing parent row closes menu and fails post-state verification
        node = _make_target_node()
        fake_cdp = FakeCdp(initial_origin=(20, 20))

        def read_nodes():
            return [_make_target_node(attrs=(("checked", fake_cdp.checked),))]

        # Proposed action with invalid/swapped elbow waypoint policy (vertical-first: 20,20 -> 20,80 -> 100,80)
        action = {
            "op": "lpath_click",
            "target": "target",
            "origin": (20, 20),
            "waypoint_policy": "vertical_first",  # Swapped axes proposal
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
            sleep=lambda s: None,
            allow_coordinates=True,
        )

        # Should fail verification because menu closed when crossing parent row
        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)
        self.assertTrue(fake_cdp.menu_closed)
        self.assertEqual(fake_cdp.checked, "false")


if __name__ == "__main__":
    unittest.main()
