"""Adversarial claim-pinning tests for wender.state.schema.validate_state self-parent rejection.

Defect S1-F9: wender/state/schema.py:121-122 adds n["id"] to ids before checking n["parent"] not in ids,
allowing a self-parent node (n["parent"] == n["id"]) to pass validation without triggering the
"nodes[0].parent must precede the node" violation.
"""
import json
import unittest


class TestJq1A6SelfParent(unittest.TestCase):
    def test_healthy_control_valid_state_passes(self):
        """Healthy control: a valid 1-node state produced by schema.build_state validates with 0 errors."""
        from wender.state import schema
        raw_nodes = [{
            "id": "0123456789abcdef",
            "role": "RootWebArea",
            "name": "Test Page",
            "depth": 0,
            "ordinal": 0,
            "attrs": [],
        }]
        state = schema.build_state(
            raw_nodes,
            kind="cdp",
            url="http://example.com",
            title="Test Page",
            ax_sha256="a" * 64,
            ax_stable_sha256="a" * 64,
            prune={"name": "test", "version": 1},
            raw_count=1,
        )
        errs = schema.validate_state(state)
        self.assertEqual(errs, [])

    def test_self_parent_node_rejected(self):
        """Defect S1-F9 (wender/state/schema.py:121-122, docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:283):
        validate_state must reject a node whose parent equals its own id with
        'nodes[0].parent must precede the node'.
        """
        from wender.state import schema
        raw_nodes = [{
            "id": "0123456789abcdef",
            "role": "RootWebArea",
            "name": "Test Page",
            "depth": 0,
            "ordinal": 0,
            "attrs": [],
        }]
        state = schema.build_state(
            raw_nodes,
            kind="cdp",
            url="http://example.com",
            title="Test Page",
            ax_sha256="a" * 64,
            ax_stable_sha256="a" * 64,
            prune={"name": "test", "version": 1},
            raw_count=1,
        )
        state["nodes"][0]["parent"] = state["nodes"][0]["id"]
        errs = schema.validate_state(state)
        self.assertTrue(errs, "expected non-empty error list for self-parent node")
        self.assertIn("nodes[0].parent must precede the node", errs)

    @staticmethod
    def _chain_state():
        from wender.state import schema
        nodes = [{
            "id": "%016x" % (index + 1), "role": "group", "name": "",
            "depth": index, "ordinal": 0, "attrs": [],
        } for index in range(3)]
        return schema.build_state(
            nodes, kind="vision", url=None, title=None,
            ax_sha256="a" * 64, ax_stable_sha256="b" * 64,
            prune={"name": "test", "version": 1}, raw_count=3,
        )

    def test_preceding_parent_chain_is_valid_and_canonical(self):
        from wender.state import schema
        state = self._chain_state()
        ids = [node["id"] for node in state["nodes"]]
        self.assertEqual([node["parent"] for node in state["nodes"]], [None, ids[0], ids[1]])
        self.assertEqual(schema.validate_state(state), [])
        self.assertEqual(json.loads(schema.dumps(state)), state)

    def test_forward_parent_and_multinode_cycles_are_rejected(self):
        from wender.state import schema
        for parent_index in (1, 2):
            with self.subTest(parent_index=parent_index):
                state = self._chain_state()
                state["nodes"][0]["parent"] = state["nodes"][parent_index]["id"]
                self.assertIn("nodes[0].parent must precede the node", schema.validate_state(state))

    def test_duplicate_id_and_self_parent_preserve_both_diagnostics(self):
        from wender.state import schema
        state = self._chain_state()
        state["nodes"][1]["id"] = state["nodes"][0]["id"]
        errors = schema.validate_state(state)
        self.assertIn("nodes[1].id duplicated", errors)
        self.assertIn("nodes[1].parent must precede the node", errors)


if __name__ == "__main__":
    unittest.main()
