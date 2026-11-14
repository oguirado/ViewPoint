"""Reproduction and gate tests for F-ACT-05 (missing-fingerprint establishment gate).

Tests the contract boundary where ax.match establishes L0_EXACT when fp_exact is missing/null,
verifying that irreversible actions require exact identity recording and raise ActionError.
"""
import unittest

from wender.act.cdp_executor import run_ax_action, ActionError
from wender.perceive import ax


class FakeCdp:
    """Recording fake CDP transport for testing cdp_executor without browser interaction."""

    def __init__(self):
        self.calls = []

    def send(self, method, params=None, session_id=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "session_id": session_id})
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        elif method == "Runtime.callFunctionOn":
            return {}
        elif method == "DOM.getBoxModel":
            return {"box": [10, 10, 40, 20]}
        elif method == "DOM.scrollIntoViewIfNeeded":
            return {}
        elif method == "Input.dispatchMouseEvent":
            return {}
        return {}


class TestFingerprintGate(unittest.TestCase):

    def _target_node(self):
        return {
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

    def test_irreversible_action_refused_when_fp_exact_is_none_override(self):
        """Defect F-ACT-05: Missing exact fingerprint establishment must not authorize irreversible action."""
        cdp = FakeCdp()
        nodes = [self._target_node()]
        desc_override = {"fp_exact": None}
        action = {
            "op": "click",
            "target": "target",
            "irreversible": True,
            "expect": {"target_name": "Send"},
            "describe": desc_override,
        }

        # Diagnostic assertion: ax.match reports established=True when fp_exact is None in desc
        derived_desc = dict(ax.describe(nodes[0]))
        derived_desc.update(desc_override)
        m = ax.match(nodes, derived_desc)
        self.assertTrue(m.get("established"))

        # Corrected contract requires ActionError before any CDP call
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                allow_coordinates=True,
                sleep=lambda s: None,
            )
        self.assertEqual(cdp.calls, [])

    def test_irreversible_action_refused_when_target_fp_exact_is_null(self):
        """Defect F-ACT-05: Target node with explicit null fp_exact must not authorize irreversible action."""
        cdp = FakeCdp()
        target = self._target_node()
        target["fp_exact"] = None
        nodes = [target]
        action = {
            "op": "click",
            "target": "target",
            "irreversible": True,
            "expect": {"target_name": "Send"},
        }

        # Diagnostic assertion: ax.match reports established=True when fp_exact in desc is None (copied from target)
        desc = dict(ax.describe(nodes[0]))
        if "fp_exact" in nodes[0]:
            desc["fp_exact"] = nodes[0]["fp_exact"]
        m = ax.match(nodes, desc)
        self.assertTrue(m.get("established"))

        # Corrected contract requires ActionError before any CDP call
        with self.assertRaises(ActionError):
            run_ax_action(
                cdp,
                "sess1",
                nodes,
                action,
                lambda: nodes,
                allow_coordinates=True,
                sleep=lambda s: None,
            )
        self.assertEqual(cdp.calls, [])

    def test_irreversible_action_succeeds_with_valid_exact_fingerprint(self):
        """Nominal control: valid exact fingerprint permits irreversible action execution."""
        cdp = FakeCdp()
        nodes = [self._target_node()]
        desc = ax.describe(nodes[0])
        action = {
            "op": "click",
            "target": "target",
            "irreversible": True,
            "expect": {"target_name": "Send"},
            "describe": desc,
        }

        # Diagnostic assertion: ax.match with valid fp_exact gives L0_EXACT without established flag
        m = ax.match(nodes, desc)
        self.assertEqual(m.get("level"), "L0_EXACT")
        self.assertNotIn("established", m)

        receipt = run_ax_action(
            cdp,
            "sess1",
            nodes,
            action,
            lambda: nodes,
            allow_coordinates=True,
            sleep=lambda s: None,
        )
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["verified"])
        self.assertTrue(len(cdp.calls) > 0)


if __name__ == "__main__":
    unittest.main()
