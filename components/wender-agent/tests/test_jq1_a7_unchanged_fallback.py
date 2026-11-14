"""Unit test reproducing WP1-03 pointer-menu vacuous success defect and verifying unchanged fallback behavior."""
import unittest

from wender.act.cdp_executor import run_ax_action
from wender.perceive import ax


def make_pruned_node(**overrides):
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
    """Transport double recording send(method, params, session_id) and returning scripted replies."""

    def __init__(self, on_mouse_released=None):
        self.calls = []
        self.on_mouse_released = on_mouse_released

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method in ("Runtime.callFunctionOn", "DOM.scrollIntoViewIfNeeded"):
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        if method == "Input.dispatchMouseEvent":
            if payload.get("type") == "mouseReleased" and callable(self.on_mouse_released):
                self.on_mouse_released()
            return {}
        return {}


def noop_sleep(seconds):
    pass


class TestJq1A7UnchangedFallback(unittest.TestCase):
    def test_unchanged_snapshot_triggers_coordinates_fallback_when_no_expect(self):
        # WP1-03 falsy-expect defect: JS click on pointer-menu with no expect vacuously succeeds on attempt 1
        # without verifying state change or trying coordinate fallback.
        released = False

        def on_mouse_released():
            nonlocal released
            released = True

        cdp = FakeCdp(on_mouse_released=on_mouse_released)

        initial_node = make_pruned_node(
            role="menuitem",
            name="Potencia",
            attrs=(("checked", "false"),),
            box=[10, 20, 100, 40],
        )
        initial_nodes = [initial_node]

        def read_nodes():
            state = "true" if released else "false"
            return [
                make_pruned_node(
                    role="menuitem",
                    name="Potencia",
                    attrs=(("checked", state),),
                    box=[10, 20, 100, 40],
                )
            ]

        receipt = run_ax_action(
            cdp,
            "sid-menu",
            initial_nodes,
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
        self.assertEqual(receipt["attempts"], 4)
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

    def test_unchanged_snapshot_fails_verification_when_no_coordinates_and_no_expect(self):
        # WP1-03 falsy-expect defect: JS click on pointer-menu with no expect vacuously succeeds
        # despite unchanged snapshot when allow_coordinates is False.
        cdp = FakeCdp()

        initial_node = make_pruned_node(
            role="menuitem",
            name="Potencia",
            attrs=(("checked", "false"),),
            box=[10, 20, 100, 40],
        )
        initial_nodes = [initial_node]

        def read_nodes():
            return [
                make_pruned_node(
                    role="menuitem",
                    name="Potencia",
                    attrs=(("checked", "false"),),
                    box=[10, 20, 100, 40],
                )
            ]

        receipt = run_ax_action(
            cdp,
            "sid-menu-nocoord",
            initial_nodes,
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


if __name__ == "__main__":
    unittest.main()
