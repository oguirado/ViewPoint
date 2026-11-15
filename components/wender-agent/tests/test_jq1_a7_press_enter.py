"""Tests for WP1-03 semantic Enter press dispatch via run_ax_action."""
import unittest

from wender.act.cdp_executor import run_ax_action
from wender.perceive import ax


class FakeCdp:
    """Transport double recording send(method, params, session_id) and returning mock CDP replies."""

    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = dict(scripted or {})

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        queue = self.scripted.get(method)
        if queue:
            item = queue.pop(0) if isinstance(queue, list) else queue
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(payload, session_id)
            return dict(item)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method in (
            "Runtime.callFunctionOn",
            "Input.dispatchKeyEvent",
            "Input.dispatchMouseEvent",
            "Input.insertText",
        ):
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}


class ScriptedReadNodes:
    """Callable returning scripted prune node lists in sequence."""

    def __init__(self, snapshots):
        self.snapshots = [list(s) for s in snapshots]

    def __call__(self):
        if not self.snapshots:
            raise IndexError("no more scripted snapshots")
        if len(self.snapshots) == 1:
            return list(self.snapshots[0])
        return list(self.snapshots.pop(0))


def _sleep_noop(seconds):
    pass


def _make_target_node(pressed="false"):
    attrs = (("pressed", pressed),) if pressed is not None else ()
    return {
        "id": "target",
        "backend_id": 101,
        "role": "button",
        "name": "Send",
        "depth": 1,
        "ancestors": ("main",),
        "attrs": attrs,
        "ordinal": 0,
        "cardinality": 1,
    }


def _make_nodes(pressed="false"):
    main_node = {
        "id": "main",
        "backend_id": 1,
        "role": "main",
        "name": "Main",
        "depth": 0,
        "ancestors": (),
        "attrs": (),
        "ordinal": 0,
        "cardinality": 1,
    }
    return [main_node, _make_target_node(pressed=pressed)]


class TestJq1A7PressEnter(unittest.TestCase):
    def test_fake_cdp_and_nodes_nominal_contract(self):
        """Nominal control: verify FakeCdp recording and target node structure."""
        cdp = FakeCdp()
        res_node = cdp.send("DOM.resolveNode", {"backendNodeId": 101}, session_id="s1")
        self.assertEqual(res_node, {"object": {"objectId": "obj"}})

        res_fn = cdp.send("Runtime.callFunctionOn", {"objectId": "obj"}, session_id="s1")
        self.assertEqual(res_fn, {})

        res_box = cdp.send("DOM.getBoxModel", {"backendNodeId": 101}, session_id="s1")
        self.assertIn("model", res_box)

        target = _make_target_node(pressed="false")
        self.assertEqual(target["id"], "target")
        self.assertEqual(target["backend_id"], 101)
        self.assertEqual(target["role"], "button")
        self.assertEqual(target["name"], "Send")
        self.assertEqual(target["depth"], 1)
        self.assertEqual(target["ancestors"], ("main",))
        self.assertEqual(target["ordinal"], 0)
        self.assertEqual(target["cardinality"], 1)

    def test_wp1_03_press_enter_action(self):
        # WP1-03: press op and Input.dispatchKeyEvent handling for Enter key
        cdp = FakeCdp()
        initial_nodes = _make_nodes(pressed="false")
        changed_nodes = _make_nodes(pressed="true")
        reader = ScriptedReadNodes([changed_nodes])

        action = {
            "op": "press",
            "key": "Enter",
            "target": "target",
            "expect": {"target_states": {"pressed": "true"}},
        }

        receipt = run_ax_action(
            cdp,
            "sid-press",
            initial_nodes,
            action,
            reader,
            wait_ms=100,
            sleep=_sleep_noop,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["post_sha"], ax.snapshot_sha(changed_nodes, True))

        key_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
        self.assertTrue(len(key_calls) >= 2)

        for call in key_calls:
            self.assertEqual(call["session_id"], "sid-press")
            params = call["params"]
            self.assertEqual(params.get("key"), "Enter")
            self.assertEqual(params.get("code"), "Enter")
            self.assertEqual(params.get("windowsVirtualKeyCode"), 13)

        # No mouse or Input.insertText calls
        mouse_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchMouseEvent"]
        insert_calls = [c for c in cdp.calls if c["method"] == "Input.insertText"]
        self.assertEqual(mouse_calls, [])
        self.assertEqual(insert_calls, [])

        types = [c["params"].get("type") for c in key_calls]
        self.assertNotIn("char", types)

        # Down variant logic: exactly one down (keyDown or rawKeyDown) followed by keyUp
        down_calls = [c for c in key_calls if c["params"].get("type") in ("keyDown", "rawKeyDown")]
        up_calls = [c for c in key_calls if c["params"].get("type") == "keyUp"]
        self.assertEqual(len(down_calls), 1)
        self.assertEqual(len(up_calls), 1)

        down_type = down_calls[0]["params"].get("type")
        if down_type == "keyDown":
            self.assertIn(down_calls[0]["params"].get("text"), ("\r", None))
        elif down_type == "rawKeyDown":
            self.assertIn(down_calls[0]["params"].get("text", ""), ("", None))

        up_text = up_calls[0]["params"].get("text", "")
        self.assertEqual(up_text, "")

    def test_wp1_03_press_enter_unchanged_state_fails_verification(self):
        # WP1-03: press op fails verification when target state remains unchanged
        cdp = FakeCdp()
        initial_nodes = _make_nodes(pressed="false")
        unchanged_nodes = _make_nodes(pressed="false")
        reader = ScriptedReadNodes([unchanged_nodes])

        action = {
            "op": "press",
            "key": "Enter",
            "target": "target",
            "expect": {"target_states": {"pressed": "true"}},
        }

        receipt = run_ax_action(
            cdp,
            "sid-press-fail",
            initial_nodes,
            action,
            reader,
            retries=1,
            wait_ms=50,
            sleep=_sleep_noop,
        )

        self.assertEqual(receipt["ok"], 0)
        self.assertEqual(receipt["verified"], 0)


if __name__ == "__main__":
    unittest.main()
