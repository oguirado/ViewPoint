"""@module tests.test_jq1_a7_press_guards - Unit tests for WP1-03 key validation and press operation guards."""
import unittest
from unittest.mock import patch

from wender.act import cdp_executor
from wender.act.cdp_executor import ActionError, run_ax_action

# Propose a bounded semantic-key allowlist for press action validation.
PROPOSED_PRESS_KEY_ALLOWLIST = frozenset({
    "Enter",
    "Escape",
    "Tab",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Space",
    "Home",
    "End",
    "PageUp",
    "PageDown",
})


def make_target_node(**overrides):
    """Construct local pruned-style node with requested default fields."""
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


class RecordingFakeCdp:
    """Recording FakeCdp double with send accepting method, params, and session_id."""

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
        if method in ("Runtime.callFunctionOn", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent"):
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        return {}


class ScriptedReadNodes:
    """Callable returning scripted node lists in sequence."""

    def __init__(self, snapshots):
        self.snapshots = [list(s) for s in snapshots]

    def __call__(self):
        if not self.snapshots:
            raise IndexError("no more scripted snapshots")
        if len(self.snapshots) == 1:
            return list(self.snapshots[0])
        return list(self.snapshots.pop(0))


def noop_sleep(seconds):
    pass


class TestJq1A7PressGuards(unittest.TestCase):
    """Unit tests pinning WP1-03 key validation and press guards."""

    def setUp(self):
        self.target = make_target_node()
        self.nodes = [self.target]

    def test_nominal_control_click_succeeds(self):
        """Nominal control verifying FakeCdp and executor function on supported op outside expectedFailure."""
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        receipt = run_ax_action(
            cdp,
            "sid-1",
            self.nodes,
            {
                "op": "click",
                "target": "target",
                "expect": {"target_name": "Send"},
            },
            reader,
            sleep=noop_sleep,
        )
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

    def test_nominal_control_invalid_op_raises_action_error(self):
        """Nominal control verifying unknown op raises ActionError before transport call."""
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sid-1",
                self.nodes,
                {"op": "frobnicate", "target": "target"},
                reader,
                sleep=noop_sleep,
            )
        self.assertEqual(cdp.calls, [])

    def test_wp1_03_press_key_validation_guards(self):
        # Defect WP1-03: key validation for press action missing when press is enabled.
        # Note: Negative cases alone do not establish key validation while press itself is
        # unsupported because unsupported-op rejection raises ActionError vacuously.
        # Patching OPS to include 'press' exposes whether key validation guards input before transport calls.
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        with patch.object(cdp_executor, "OPS", cdp_executor.OPS + ("press",)):
            # 1. Missing key
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-1",
                    self.nodes,
                    {"op": "press", "target": "target"},
                    reader,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

            # 2. Non-string key
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-1",
                    self.nodes,
                    {"op": "press", "target": "target", "key": 123},
                    reader,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

            # 3. Empty key string
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-1",
                    self.nodes,
                    {"op": "press", "target": "target", "key": ""},
                    reader,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

            # 4. Unknown key 'Frobnicator'
            with self.assertRaises(ActionError):
                run_ax_action(
                    cdp,
                    "sid-1",
                    self.nodes,
                    {"op": "press", "target": "target", "key": "Frobnicator"},
                    reader,
                    sleep=noop_sleep,
                )
            self.assertEqual(cdp.calls, [])

    def test_wp1_03_press_escape_positive(self):
        # Defect WP1-03: press op for Escape unsupported in cdp_executor
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        receipt = run_ax_action(
            cdp,
            "sid-esc",
            self.nodes,
            {
                "op": "press",
                "target": "target",
                "key": "Escape",
                "expect": {"target_name": "Send"},
            },
            reader,
            sleep=noop_sleep,
        )
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
        downs = [e for e in key_events if e["params"].get("type") in ("keyDown", "rawKeyDown")]
        ups = [e for e in key_events if e["params"].get("type") == "keyUp"]
        self.assertEqual(len(downs), 1)
        self.assertEqual(len(ups), 1)
        self.assertEqual(downs[0]["params"].get("key"), "Escape")
        self.assertEqual(downs[0]["params"].get("code"), "Escape")
        self.assertEqual(ups[0]["params"].get("key"), "Escape")
        self.assertEqual(ups[0]["params"].get("code"), "Escape")
        # Non-text key must not insert text
        self.assertFalse(downs[0]["params"].get("text"))
        self.assertFalse(ups[0]["params"].get("text"))

    def test_wp1_03_press_tab_positive(self):
        # Defect WP1-03: press op for Tab unsupported in cdp_executor
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        receipt = run_ax_action(
            cdp,
            "sid-tab",
            self.nodes,
            {
                "op": "press",
                "target": "target",
                "key": "Tab",
                "expect": {"target_name": "Send"},
            },
            reader,
            sleep=noop_sleep,
        )
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
        downs = [e for e in key_events if e["params"].get("type") in ("keyDown", "rawKeyDown")]
        ups = [e for e in key_events if e["params"].get("type") == "keyUp"]
        self.assertEqual(len(downs), 1)
        self.assertEqual(len(ups), 1)
        self.assertEqual(downs[0]["params"].get("key"), "Tab")
        self.assertEqual(downs[0]["params"].get("code"), "Tab")
        self.assertEqual(ups[0]["params"].get("key"), "Tab")
        self.assertEqual(ups[0]["params"].get("code"), "Tab")
        # Non-text key must not insert text
        self.assertFalse(downs[0]["params"].get("text"))
        self.assertFalse(ups[0]["params"].get("text"))

    def test_wp1_03_press_arrow_down_positive(self):
        # Defect WP1-03: press op for ArrowDown unsupported in cdp_executor
        cdp = RecordingFakeCdp()
        reader = ScriptedReadNodes([self.nodes])
        receipt = run_ax_action(
            cdp,
            "sid-arrow",
            self.nodes,
            {
                "op": "press",
                "target": "target",
                "key": "ArrowDown",
                "expect": {"target_name": "Send"},
            },
            reader,
            sleep=noop_sleep,
        )
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        key_events = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
        downs = [e for e in key_events if e["params"].get("type") in ("keyDown", "rawKeyDown")]
        ups = [e for e in key_events if e["params"].get("type") == "keyUp"]
        self.assertEqual(len(downs), 1)
        self.assertEqual(len(ups), 1)
        self.assertEqual(downs[0]["params"].get("key"), "ArrowDown")
        self.assertEqual(downs[0]["params"].get("code"), "ArrowDown")
        self.assertEqual(ups[0]["params"].get("key"), "ArrowDown")
        self.assertEqual(ups[0]["params"].get("code"), "ArrowDown")
        # Non-text key must not insert text
        self.assertFalse(downs[0]["params"].get("text"))
        self.assertFalse(ups[0]["params"].get("text"))

    def test_proposed_press_key_allowlist_bounded_and_contains_standard_keys(self):
        """Verify proposed semantic key allowlist contains required keys and rejects Frobnicator."""
        self.assertIn("Escape", PROPOSED_PRESS_KEY_ALLOWLIST)
        self.assertIn("Tab", PROPOSED_PRESS_KEY_ALLOWLIST)
        self.assertIn("ArrowDown", PROPOSED_PRESS_KEY_ALLOWLIST)
        self.assertNotIn("Frobnicator", PROPOSED_PRESS_KEY_ALLOWLIST)
        self.assertLessEqual(len(PROPOSED_PRESS_KEY_ALLOWLIST), 30)


if __name__ == "__main__":
    unittest.main()
