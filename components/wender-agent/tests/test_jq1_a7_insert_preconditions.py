"""Tests for WP1-03 insert_text safety contract and preconditions in cdp_executor.

PROPOSAL / WP1-03: Safety contract for proposed insert_text op:
- A target node with editable role (e.g., 'textbox') and disabled='false' accepts string text.
- Targets with disabled='true', absent text, integer text, or non-editable role ('button')
  are refused with ActionError prior to any mutating CDP command.
- Note: An AX role alone does not prove browser focus or rich-editor selection;
  DOM element state and focus state must be validated by the executor/browser.
- Documented as PROPOSAL because no insert_text implementation currently exists in cdp_executor.
"""
import unittest
from wender.act.cdp_executor import run_ax_action, ActionError


class FakeCdp:
    """Recording CDP transport for unit tests."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append({
            "method": method,
            "params": params,
            "session_id": session_id,
        })
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        elif method == "Runtime.callFunctionOn":
            return {}
        elif method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 100, 10, 100, 40, 10, 40]}}
        elif method.startswith("Input."):
            return {}
        else:
            return {}


class TestInsertPreconditions(unittest.TestCase):
    """Pin proposed WP1-03 insert_text safety contract and preconditions."""

    def _make_node(self, **overrides):
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

    def test_insert_text_positive_editable_witness(self):
        """PROPOSAL / WP1-03: Editable textbox target with disabled false accepts string text.

        Currently expectedFailure because insert_text is not yet implemented in cdp_executor.
        """
        cdp = FakeCdp()
        node = self._make_node(
            role="textbox",
            name="Prompt",
            attrs=(("disabled", "false"),),
        )
        nodes = [node]
        action = {
            "op": "insert_text",
            "target": "target",
            "text": "Hello world",
            "expect": {"target_name": "Prompt"},
        }

        receipt = run_ax_action(
            cdp,
            "sess1",
            nodes,
            action,
            lambda: nodes,
            sleep=lambda s: None,
        )

        self.assertTrue(receipt["ok"])
        self.assertGreater(len(cdp.calls), 0)

    def test_insert_text_refuses_disabled_target(self):
        """PROPOSAL / WP1-03: Disabled target (attrs disabled='true') is refused before CDP commands."""
        cdp = FakeCdp()
        node = self._make_node(
            role="textbox",
            name="Prompt",
            attrs=(("disabled", "true"),),
        )
        nodes = [node]
        action = {
            "op": "insert_text",
            "target": "target",
            "text": "Hello world",
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                sleep=lambda s: None,
            )

        self.assertEqual(cdp.calls, [])

    def test_insert_text_refuses_absent_text(self):
        """PROPOSAL / WP1-03: Action missing 'text' parameter is refused before CDP commands."""
        cdp = FakeCdp()
        node = self._make_node(
            role="textbox",
            name="Prompt",
            attrs=(("disabled", "false"),),
        )
        nodes = [node]
        action = {
            "op": "insert_text",
            "target": "target",
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                sleep=lambda s: None,
            )

        self.assertEqual(cdp.calls, [])

    def test_insert_text_refuses_integer_text(self):
        """PROPOSAL / WP1-03: Action with integer 'text' parameter is refused before CDP commands."""
        cdp = FakeCdp()
        node = self._make_node(
            role="textbox",
            name="Prompt",
            attrs=(("disabled", "false"),),
        )
        nodes = [node]
        action = {
            "op": "insert_text",
            "target": "target",
            "text": 123,
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                sleep=lambda s: None,
            )

        self.assertEqual(cdp.calls, [])

    def test_insert_text_refuses_noneditable_button(self):
        """PROPOSAL / WP1-03: Noneditable button target is refused before CDP commands."""
        cdp = FakeCdp()
        node = self._make_node(
            role="button",
            name="Send",
            attrs=(),
        )
        nodes = [node]
        action = {
            "op": "insert_text",
            "target": "target",
            "text": "Hello world",
        }

        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                sleep=lambda s: None,
            )

        self.assertEqual(cdp.calls, [])


if __name__ == "__main__":
    unittest.main()
