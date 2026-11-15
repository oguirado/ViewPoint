"""Adversarial unittest tests for D-A8-03 and D-A8-04 in ProgramMap.add_edge and to_json."""
import unittest

from wender.remember.program_map import ProgramMap
from wender.state.schema import dumps


class TestEdgeCanonicalDedupAndSort(unittest.TestCase):

    def test_changed_type_identity_and_successor_total_order(self):
        first = ProgramMap("p")
        a, b = first.observe("a", None), first.observe("b", None)
        for changed in ({"nested": [True]}, {"nested": [1]}, None):
            first.add_edge(a, {"op": "click"}, b, changed)
        first.add_edge(a, {"op": "click"}, b, {"nested": [True]})
        self.assertEqual(len(first.edges), 3)
        self.assertEqual([e["changed"] for e in first.successors(a)],
                         [None, {"nested": [1]}, {"nested": [True]}])

    def test_invalid_nested_metadata_cannot_mutate_map(self):
        graph = ProgramMap("p")
        a, b = graph.observe("a", None), graph.observe("b", None)
        before = graph.to_json()
        cycle = []
        cycle.append(cycle)
        for invalid in ({1.5: "key"}, {"x": [{False: "key"}]},
                        {"x": 1.5}, {"x": b"binary"}, {"PiXeLs": "forbidden"}, cycle):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(ValueError):
                    graph.add_edge(a, {"op": "click"}, b, invalid)
                self.assertEqual(graph.to_json(), before)

    def test_passing_controls_dumps_distinguishes_bool_int(self):
        """Passing control: schema.dumps distinguishes boolean True from integer 1 in actions."""
        action_bool = {"click": "button", "flag": True}
        action_int = {"click": "button", "flag": 1}
        dump_bool = dumps(action_bool)
        dump_int = dumps(action_int)
        self.assertNotEqual(dump_bool, dump_int)
        self.assertEqual(dump_bool, '{"click":"button","flag":true}')
        self.assertEqual(dump_int, '{"click":"button","flag":1}')

    def test_passing_controls_edge_multiset_order_independence(self):
        """Passing control: expected canonical edge multiset is independent of insertion order when edges are sorted."""
        map1 = ProgramMap("test")
        n1 = map1.observe("screen1", "ax1")
        n2 = map1.observe("screen2", "ax2")

        action_a = {"click": "a"}
        action_b = {"click": "b"}

        map1.add_edge(n1, action_a, n2, changed={"c": 1})
        map1.add_edge(n1, action_b, n2, changed={"c": 2})

        map2 = ProgramMap("test")
        n1_2 = map2.observe("screen1", "ax1")
        n2_2 = map2.observe("screen2", "ax2")

        map2.add_edge(n1_2, action_b, n2_2, changed={"c": 2})
        map2.add_edge(n1_2, action_a, n2_2, changed={"c": 1})

        self.assertEqual(len(map1.edges), 2)
        self.assertEqual(len(map2.edges), 2)
        self.assertEqual(map1.to_json(), map2.to_json())

    def test_d_a8_03_boolean_integer_deduplication(self):
        # Known defect D-A8-03: Python dict equality considers True == 1, causing add_edge deduplication to collapse canonically distinct edges.
        map1 = ProgramMap("p")
        n1 = map1.observe("s1", "ax1")
        n2 = map1.observe("s2", "ax2")

        action_bool = {"click": "btn", "flag": True}
        action_int = {"click": "btn", "flag": 1}

        map1.add_edge(n1, action_bool, n2, changed={"c": 1})
        map1.add_edge(n1, action_int, n2, changed={"c": 1})

        map2 = ProgramMap("p")
        n1_2 = map2.observe("s1", "ax1")
        n2_2 = map2.observe("s2", "ax2")

        map2.add_edge(n1_2, action_int, n2_2, changed={"c": 1})
        map2.add_edge(n1_2, action_bool, n2_2, changed={"c": 1})

        # Correct behavior retains both canonically distinct edges in both maps and produces equal to_json output.
        self.assertEqual(len(map1.edges), 2)
        self.assertEqual(len(map2.edges), 2)
        self.assertEqual(map1.to_json(), map2.to_json())

    def test_d_a8_04_changed_tie_breaking(self):
        # Known defect D-A8-04: to_json sorting key (e["pre"], dumps(e["action"]), e["post"]) omits changed summary, causing tie-breaking to depend on insertion order.
        action = {"click": "btn"}

        map1 = ProgramMap("p")
        n1 = map1.observe("s1", "ax1")
        n2 = map1.observe("s2", "ax2")

        # changed summaries plus 1 minus 0 changed 0 and plus 2 minus 0 changed 0
        changed1 = {"plus": 1, "minus": 0, "changed": 0}
        changed2 = {"plus": 2, "minus": 0, "changed": 0}

        map1.add_edge(n1, action, n2, changed=changed1)
        map1.add_edge(n1, action, n2, changed=changed2)

        map2 = ProgramMap("p")
        n1_2 = map2.observe("s1", "ax1")
        n2_2 = map2.observe("s2", "ax2")

        map2.add_edge(n1_2, action, n2_2, changed=changed2)
        map2.add_edge(n1_2, action, n2_2, changed=changed1)

        # Correct behavior retains both edges and produces equal serialization regardless of insertion order.
        self.assertEqual(len(map1.edges), 2)
        self.assertEqual(len(map2.edges), 2)
        self.assertEqual(map1.to_json(), map2.to_json())


if __name__ == "__main__":
    unittest.main()
