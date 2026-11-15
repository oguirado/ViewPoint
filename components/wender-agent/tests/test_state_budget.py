import unittest
from tools import state_budget
from wender.perceive import ax
from wender.state import schema


def make_state_with_nodes(count, actionable_indices=None):
    if actionable_indices is None:
        actionable_indices = set()
    raw_nodes = []
    # Build tree structure
    for i in range(count):
        # Even nodes or node 0 can be parents
        depth = 0 if i == 0 else (1 if i % 2 == 1 else 2)
        role = "button" if i in actionable_indices else ("group" if depth < 2 else "text")
        attrs = [("disabled", "false")] if role == "button" else []
        node = {
            "id": "%016x" % (i + 1),
            "role": role,
            "name": "Node %d" % i,
            "depth": depth,
            "attrs": attrs,
            "ordinal": i,
        }
        raw_nodes.append(node)

    # Use build_state with synthetic nodes
    # For build_state, nodes are passed as dicts with id, role, name, depth, ordinal, attrs
    st = schema.build_state(
        raw_nodes,
        kind="cdp",
        url="file:///test",
        title="Test Page",
        ax_sha256="0" * 64,
        ax_stable_sha256="0" * 64,
        prune={"name": ax.PRUNE_NAME, "version": 1},
        raw_count=count * 2,
        boxes={raw_nodes[0]["id"]: [0, 0, 100, 100]},
        viewport={"w": 1024, "h": 768},
        actionable_roles=frozenset(["button"]),
    )
    return st


class TestStateBudget(unittest.TestCase):
    def test_report_1_node(self):
        st = make_state_with_nodes(1, {0})
        rep = state_budget.report(st)
        self.assertIsInstance(rep, dict)
        for k, v in rep.items():
            self.assertIsInstance(v, int, "Key %s is not int: %r" % (k, v))
            self.assertNotIsInstance(v, bool, "Key %s is bool: %r" % (k, v))

    def test_report_50_nodes(self):
        st = make_state_with_nodes(50, {5, 10, 15})
        rep = state_budget.report(st)
        for k, v in rep.items():
            self.assertIsInstance(v, int, "Key %s is not int" % k)
            self.assertNotIsInstance(v, bool, "Key %s is bool" % k)
        self.assertEqual(rep["bytes_total"], rep["bytes_nodes"] + rep["bytes_top_level"])

    def test_report_2000_nodes(self):
        st = make_state_with_nodes(2000, {100, 500, 1500})
        rep = state_budget.report(st)
        for k, v in rep.items():
            self.assertIsInstance(v, int, "Key %s is not int" % k)
            self.assertNotIsInstance(v, bool, "Key %s is bool" % k)

    def test_trim_keeps_actionable_and_ancestors(self):
        # 50 nodes, node 45 is actionable (depth 2, parent is node 44 depth 1, whose parent is node 0 depth 0)
        st = make_state_with_nodes(50, {45})
        orig_bytes = len(schema.dumps(st).encode("utf-8"))
        target_bytes = orig_bytes - 1000
        trimmed = state_budget.trim(st, target_bytes)

        self.assertEqual(schema.validate_state(trimmed), [])
        trimmed_bytes = len(schema.dumps(trimmed).encode("utf-8"))
        self.assertLessEqual(trimmed_bytes, target_bytes)

        # Actionable node 45 and its emitted ancestors must be present
        trimmed_ids = {n["id"] for n in trimmed["nodes"]}
        act_node_id = "%016x" % 46  # 45 + 1
        self.assertIn(act_node_id, trimmed_ids)
        self.assertIn(act_node_id, trimmed["actionable"])

    def test_trim_deterministic(self):
        st = make_state_with_nodes(50, {10})
        orig_bytes = len(schema.dumps(st).encode("utf-8"))
        target = orig_bytes - 800
        t1 = state_budget.trim(st, target)
        t2 = state_budget.trim(st, target)
        self.assertEqual(schema.dumps(t1), schema.dumps(t2))

    def test_impossible_budget_raises(self):
        st = make_state_with_nodes(10, {0, 1, 2, 3, 4, 5, 6, 7, 8, 9})
        # All nodes are actionable, so none can be trimmed.
        with self.assertRaises(ValueError):
            state_budget.trim(st, 100)


if __name__ == "__main__":
    unittest.main()
