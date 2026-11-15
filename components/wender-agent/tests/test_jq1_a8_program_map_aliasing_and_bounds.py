"""Endpoint boundary, aliasing, and ownership tests for ProgramMap (D-A8-05)."""
import unittest

from wender.remember.program_map import ProgramMap


class TestProgramMapAliasingAndBounds(unittest.TestCase):
    def test_nested_duplicate_and_snapshot_ownership(self):
        graph = ProgramMap("p")
        a, b = graph.observe("a", None), graph.observe("b", None)
        graph.add_edge(a, {"params": [1]}, b, {"details": {"ok": True}})
        before = graph.to_json()
        duplicate = graph.add_edge(a, {"params": [1]}, b, {"details": {"ok": True}})
        duplicate["action"]["params"].append(2)
        duplicate["changed"]["details"]["ok"] = False
        snapshot = graph.snapshot()
        snapshot["nodes"][a]["seen"] = 999
        snapshot["edges"][0]["changed"]["details"]["ok"] = False
        self.assertEqual(graph.to_json(), before)

    def test_endpoint_bounds_and_unknown_key_successors(self):
        pm = ProgramMap("test_app")
        # KeyError naming missing_pre when both pre and post endpoints are absent
        missing_pre = "fp_missing_pre:ax_missing_pre"
        missing_post = "fp_missing_post:ax_missing_post"
        with self.assertRaises(KeyError) as ctx:
            pm.add_edge(missing_pre, {"op": "click"}, missing_post)
        self.assertIn(missing_pre, str(ctx.exception))

        # successors of an unknown key returns empty list without changing nodes
        nodes_before = dict(pm.nodes)
        succ = pm.successors("unknown_key")
        self.assertEqual(succ, [])
        self.assertEqual(pm.nodes, nodes_before)

    def test_duplicate_return_contract(self):
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        action = {"path": "ax", "op": "press", "target": "btn"}
        changed = {"tiles": 3}

        # New and duplicate returns are independently owned copies.
        e1 = pm.add_edge(a, action, b, changed)
        self.assertEqual(e1, pm.edges[0])
        self.assertIsNot(e1, pm.edges[0])

        # Duplicate return is equal but a separate object
        e2 = pm.add_edge(a, action, b, changed)
        self.assertEqual(e2, e1)
        self.assertIsNot(e2, e1)
        self.assertIsNot(e2, pm.edges[0])
        self.assertEqual(len(pm.edges), 1)

    def test_mutating_outer_successors_list_is_harmless(self):
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        pm.add_edge(a, {"op": "click"}, b, {"state": "ok"})
        json_before = pm.to_json()

        # Mutate outer list returned by successors
        succ = pm.successors(a)
        succ.append({"fake": "edge"})
        succ.clear()

        # ProgramMap remains unchanged
        self.assertEqual(len(pm.edges), 1)
        self.assertEqual(pm.to_json(), json_before)

    def test_ownership_mutating_caller_action_preserves_serialization(self):
        # D-A8-05: ProgramMap holds caller action dictionary by reference without defensive copy
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        action = {"path": "ax", "op": "press", "params": {"target": "x"}}
        pm.add_edge(a, action, b)
        json_before = pm.to_json()

        # Mutate nested caller action
        action["params"]["target"] = "y"

        # Correct ownership behavior preserves the recorded serialization
        self.assertEqual(pm.to_json(), json_before)

    def test_ownership_mutating_caller_changed_preserves_serialization(self):
        # D-A8-05: ProgramMap holds changed payload dictionary by reference without defensive copy
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        changed = {"details": {"tiles": [1, 2]}}
        pm.add_edge(a, {"op": "click"}, b, changed)
        json_before = pm.to_json()

        # Mutate nested changed payload
        changed["details"]["tiles"].append(3)

        # Correct ownership behavior preserves the recorded serialization
        self.assertEqual(pm.to_json(), json_before)

    def test_ownership_mutating_returned_edge_preserves_serialization(self):
        # D-A8-05: ProgramMap.add_edge returns stored edge object allowing direct mutation
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        e1 = pm.add_edge(a, {"op": "click"}, b, {"status": "ok"})
        json_before = pm.to_json()

        # Mutate returned original edge
        e1["action"]["op"] = "double_click"

        # Correct ownership behavior preserves the recorded serialization
        self.assertEqual(pm.to_json(), json_before)

    def test_ownership_mutating_successors_element_preserves_serialization(self):
        # D-A8-05: ProgramMap.successors returns list of stored edge dictionaries sharing references
        pm = ProgramMap("test_app")
        a = pm.observe("fp1", "s1")
        b = pm.observe("fp2", "s2")
        pm.add_edge(a, {"op": "click"}, b, {"status": "ok"})
        json_before = pm.to_json()

        # Mutate dictionary element returned by successors
        succ = pm.successors(a)
        succ[0]["action"]["op"] = "drag"

        # Correct ownership behavior preserves the recorded serialization
        self.assertEqual(pm.to_json(), json_before)


if __name__ == "__main__":
    unittest.main()
