"""Unit tests for wender.act.cdp_executor rename recovery and backend-id fallback."""
import unittest
from unittest.mock import MagicMock

from wender.act.cdp_executor import _verify_expect, run_ax_action
from wender.perceive import ax


class FakeCdp:
    """Recording CDP double for cdp_executor tests."""

    def __init__(self, scripted=None):
        self.calls = []
        self.scripted = dict(scripted or {})

    def send(self, method, params=None, session_id=None):
        payload = dict(params or {})
        self.calls.append({"method": method, "params": payload, "session_id": session_id})
        if method in self.scripted:
            reply = self.scripted[method]
            if isinstance(reply, list):
                item = reply.pop(0) if reply else {}
            else:
                item = reply
            if isinstance(item, Exception):
                raise item
            return item
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
            return {}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 10, 50, 10, 50, 30, 10, 30]}}
        return {}


class ScriptedReadNodes:
    """Sequence callable returning pruned node lists."""

    def __init__(self, sequence):
        self.sequence = [list(s) for s in sequence]

    def __call__(self):
        if not self.sequence:
            raise IndexError("no more scripted snapshots")
        if len(self.sequence) == 1:
            return list(self.sequence[0])
        return list(self.sequence.pop(0))


class TestRenameRecovery(unittest.TestCase):
    def setUp(self):
        self.base_fields = {
            "backend_id": 101,
            "role": "button",
            "name": "Send",
            "depth": 1,
            "ancestors": ("main",),
            "attrs": (),
            "ordinal": 0,
            "cardinality": 1,
        }
        self.initial_node = dict(self.base_fields)
        self.initial_node["id"] = ax.stable_id(self.initial_node)

    def test_verify_expect_rename_recovery_and_backend_fallback(self):
        expect_spec = {
            "target_name": "Sent",
            "target_states": {"pressed": "true"},
        }

        # 1. Renamed node with name normalized by ax.normalize_name and recomputed stable_id
        renamed_name = ax.normalize_name("Sent")
        renamed_node = dict(self.initial_node, name=renamed_name, attrs=(("pressed", "true"),))
        renamed_node["id"] = ax.stable_id(renamed_node)

        # Verify rename recovery via recomputed stable_id
        res_rename = _verify_expect([renamed_node], self.initial_node, expect_spec)
        self.assertTrue(res_rename)

        # 2. Renamed node with altered ancestors changing its stable_id while retaining backend_id=101 and role=button
        altered_node = dict(renamed_node, ancestors=("main", "container"))
        altered_node["id"] = ax.stable_id(altered_node)

        # Verify backend-id recovery
        res_backend_fallback = _verify_expect([altered_node], self.initial_node, expect_spec)
        self.assertTrue(res_backend_fallback)

        # 3. Negative control: change backend_id
        neg_backend_node = dict(altered_node, backend_id=102)
        neg_backend_node["id"] = ax.stable_id(neg_backend_node)
        self.assertFalse(_verify_expect([neg_backend_node], self.initial_node, expect_spec))

        # 4. Negative control: change role
        neg_role_node = dict(altered_node, role="checkbox")
        neg_role_node["id"] = ax.stable_id(neg_role_node)
        self.assertFalse(_verify_expect([neg_role_node], self.initial_node, expect_spec))

        # 5. Negative control: keep backend_id and role but wrong name
        neg_name_node = dict(altered_node, name="Cancel")
        neg_name_node["id"] = ax.stable_id(neg_name_node)
        self.assertFalse(_verify_expect([neg_name_node], self.initial_node, expect_spec))

        # 6. Negative control: matching backend and role and name, but wrong state (pressed=false)
        neg_state_node = dict(altered_node, attrs=(("pressed", "false"),))
        neg_state_node["id"] = ax.stable_id(neg_state_node)
        self.assertFalse(_verify_expect([neg_state_node], self.initial_node, expect_spec))

    def test_run_ax_action_rename_recovery_integration(self):
        cdp = FakeCdp()
        sleep_mock = MagicMock()

        renamed_name = ax.normalize_name("Sent")
        post_node = dict(self.initial_node, name=renamed_name, attrs=(("pressed", "true"),))
        post_node["id"] = ax.stable_id(post_node)

        reader = ScriptedReadNodes([[post_node]])
        action = {
            "op": "click",
            "target": self.initial_node["id"],
            "expect": {
                "target_name": "Sent",
                "target_states": {"pressed": "true"},
            },
        }

        receipt = run_ax_action(
            cdp,
            "sid-test",
            [self.initial_node],
            action,
            reader,
            retries=1,
            sleep=sleep_mock,
        )

        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(receipt["verified"], 1)
        self.assertEqual(receipt["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
