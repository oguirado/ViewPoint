"""Test-first contract for WP1-03 hover operation through run_ax_action."""
import unittest

from wender.act.cdp_executor import _verify_expect, run_ax_action


def _make_node(**overrides):
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
    }
    node.update(overrides)
    return node


class FakeCdp:
    """Recording FakeCdp for hover testing."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append(
            {"method": method, "params": payload, "session_id": session_id}
        )
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"type": "undefined"}}
        if method.startswith("Input."):
            return {}
        return {}


class FakeReader:
    """Deterministic read_nodes sequence toggling expanded on hover mouseMoved."""

    def __init__(self, cdp, target_node):
        self.cdp = cdp
        self.target_node = target_node

    def __call__(self):
        moved_to_target = any(
            c["method"] == "Input.dispatchMouseEvent"
            and c["params"].get("type") == "mouseMoved"
            and c["params"].get("x") == 60
            and c["params"].get("y") == 40
            for c in self.cdp.calls
        )
        expanded_val = "true" if moved_to_target else "false"
        updated = dict(self.target_node)
        updated["attrs"] = (("expanded", expanded_val),)
        return [updated]


class TestJq1A7HoverCentre(unittest.TestCase):
    def test_wp1_03_hover_operation_contract(self):
        # WP1-03: hover operation is absent in run_ax_action / cdp_executor.OPS
        target_node = _make_node(
            role="menuitem",
            name="Submenu",
            box=[10, 20, 100, 40],
            attrs=(("expanded", "false"),),
        )
        nodes = [target_node]
        cdp = FakeCdp()
        reader = FakeReader(cdp, target_node)
        session_id = "sess-hover-001"

        action = {
            "op": "hover",
            "target": "target",
            "expect": {"target_states": {"expanded": "true"}},
            "box": [10, 20, 100, 40],
        }

        receipt = run_ax_action(
            cdp,
            session_id,
            nodes,
            action,
            reader,
            sleep=lambda s: None,
        )

        # Assert verification succeeds
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        # Require a single move event
        mouse_moves = [
            c
            for c in cdp.calls
            if c["method"] == "Input.dispatchMouseEvent"
            and c["params"].get("type") == "mouseMoved"
        ]
        self.assertEqual(len(mouse_moves), 1)
        self.assertEqual(mouse_moves[0]["params"]["x"], 60)
        self.assertEqual(mouse_moves[0]["params"]["y"], 40)

        # Require correct session
        self.assertEqual(mouse_moves[0]["session_id"], session_id)

        # Require no pressed or released events
        click_events = [
            c
            for c in cdp.calls
            if c["method"] == "Input.dispatchMouseEvent"
            and c["params"].get("type") in ("mousePressed", "mouseReleased")
        ]
        self.assertEqual(click_events, [])

        # Require no Runtime.callFunctionOn click
        js_clicks = [
            c
            for c in cdp.calls
            if c["method"] == "Runtime.callFunctionOn"
            and "this.click" in c["params"].get("functionDeclaration", "")
        ]
        self.assertEqual(js_clicks, [])

    def test_unchanged_expanded_state_fails_verification(self):
        """Nominal control outside expectedFailure: unchanged expanded state fails semantic verification."""
        target_node = _make_node(
            role="menuitem",
            name="Submenu",
            box=[10, 20, 100, 40],
            attrs=(("expanded", "false"),),
        )
        unchanged_nodes = [target_node]
        expect = {"target_states": {"expanded": "true"}}

        # Semantic verification must fail when target_states expanded is still false
        verified = _verify_expect(unchanged_nodes, target_node, expect)
        self.assertFalse(verified)

        # When expanded becomes true, semantic verification succeeds
        expanded_node = dict(target_node, attrs=(("expanded", "true"),))
        verified_ok = _verify_expect([expanded_node], target_node, expect)
        self.assertTrue(verified_ok)


if __name__ == "__main__":
    unittest.main()
