"""WP1-03 literal insertion unicode test pins for wender/act/cdp_executor.py."""
import unittest

from wender.act.cdp_executor import run_ax_action, ActionError


class FakeCdp:
    """Recording FakeCdp transport for testing CDP actions."""

    def __init__(self, resolve_null=False):
        self.calls = []
        self.resolve_null = resolve_null

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "session_id": session_id})
        if method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "DOM.resolveNode":
            if self.resolve_null:
                return {"object": None}
            return {"object": {"objectId": "obj_target_101"}}
        elif method == "Runtime.callFunctionOn":
            return {"result": {"value": True}}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        elif method in ("Input.insertText", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent"):
            return {}
        else:
            return {}


def make_node(**overrides):
    """Construct local pruned-style nodes with specified base defaults."""
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


class TestJq1A7InsertUnicode(unittest.TestCase):

    def test_nominal_invalid_inputs_raise_action_error(self):
        """Nominal control: invalid inputs or missing target raise ActionError without CDP calls."""
        cdp = FakeCdp()
        nodes = [make_node(role="textbox", name="Prompt")]
        with self.assertRaises(ActionError):
            run_ax_action(cdp, "sess1", nodes, {"op": "type", "target": "missing"}, lambda: nodes, sleep=lambda s: None)
        self.assertEqual(cdp.calls, [])

    def test_wp1_03_insert_text_unicode_payload_and_focus(self):
        # Defect: WP1-03 op insert_text is not yet implemented in cdp_executor
        cdp = FakeCdp()
        target_node = make_node(role="textbox", name="Prompt", attrs=(("busy", "false"),))
        nodes = [target_node]

        read_count = 0

        def read_nodes():
            nonlocal read_count
            read_count += 1
            if read_count >= 1:
                return [make_node(role="textbox", name="Prompt", attrs=(("busy", "true"),))]
            return [make_node(role="textbox", name="Prompt", attrs=(("busy", "false"),))]

        text_to_insert = "preguntá 你好 🌍\n"
        action = {
            "op": "insert_text",
            "target": "target",
            "text": text_to_insert,
            "expect": {"target_states": {"busy": "true"}},
        }

        receipt = run_ax_action(cdp, "sess1", nodes, action, read_nodes, sleep=lambda s: None)

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)

        # Focus verification before insertion
        focus_or_resolve = [c for c in cdp.calls if c["method"] in ("DOM.resolveNode", "Runtime.callFunctionOn")]
        self.assertTrue(len(focus_or_resolve) > 0)

        # Exactly one Input.insertText payload containing unchanged full text
        insert_calls = [c for c in cdp.calls if c["method"] == "Input.insertText"]
        self.assertEqual(len(insert_calls), 1)
        payload_text = insert_calls[0]["params"].get("text")
        self.assertEqual(payload_text, text_to_insert)

        # No Input.dispatchKeyEvent calls
        key_calls = [c for c in cdp.calls if c["method"] == "Input.dispatchKeyEvent"]
        self.assertEqual(len(key_calls), 0)

        # No value-assignment JS pretending to be insertion
        runtime_calls = [c for c in cdp.calls if c["method"] == "Runtime.callFunctionOn"]
        for call in runtime_calls:
            func_decl = call["params"].get("functionDeclaration", "")
            self.assertNotIn("this.value =", func_decl)

    def test_wp1_03_insert_text_unverified_when_busy_remains_false(self):
        # Defect: WP1-03 op insert_text is not yet implemented in cdp_executor
        cdp = FakeCdp()
        target_node = make_node(role="textbox", name="Prompt", attrs=(("busy", "false"),))
        nodes = [target_node]

        def read_nodes_busy_false():
            return [make_node(role="textbox", name="Prompt", attrs=(("busy", "false"),))]

        text_to_insert = "preguntá 你好 🌍\n"
        action = {
            "op": "insert_text",
            "target": "target",
            "text": text_to_insert,
            "expect": {"target_states": {"busy": "true"}},
        }

        receipt = run_ax_action(cdp, "sess1", nodes, action, read_nodes_busy_false, sleep=lambda s: None)

        self.assertEqual(receipt["verified"], 0)


if __name__ == "__main__":
    unittest.main()
