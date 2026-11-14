import os
import random
import unittest

from wender.state import schema

try:
    from wender.state import diff
except ImportError as err:
    if os.environ.get("WA1_PIN_ALLOW_SKIP") == "1":
        raise unittest.SkipTest("wa1-spark-state-diff module wender.state.diff not available: %s" % err)
    raise


def _make_state(nodes, *, ax_sha="0" * 64, ax_stable_sha="0" * 64):
    out_nodes = []
    actionable_ids = []
    for i, n in enumerate(nodes):
        node_id = n.get("id", "%016x" % (i + 1))
        is_actionable = n.get("actionable", False)
        out_nodes.append({
            "id": node_id,
            "role": n.get("role", "button"),
            "name": n.get("name", "Node %d" % i),
            "depth": n.get("depth", 0),
            "parent": n.get("parent", None),
            "ordinal": n.get("ordinal", i),
            "states": n.get("states", {}),
            "actionable": is_actionable,
            "box": n.get("box", [0, 0, 10, 10]),
        })
        if is_actionable:
            actionable_ids.append(node_id)

    st = {
        "schema": schema.SCHEMA_ID,
        "source": {"kind": "cdp", "url": "http://example.com", "title": "Test"},
        "prune": {"name": "test", "version": 1},
        "ax_sha256": ax_sha,
        "ax_stable_sha256": ax_stable_sha,
        "viewport": {"w": 1024, "h": 768},
        "nodes": out_nodes,
        "actionable": actionable_ids,
        "counts": {
            "raw": len(out_nodes),
            "nodes": len(out_nodes),
            "actionable": len(actionable_ids),
        },
    }
    errs = schema.validate_state(st)
    if errs:
        raise schema.StateError("; ".join(errs))
    return st


class TestWA1DiffPins(unittest.TestCase):

    def test_invalid_state_rejected(self):
        """Kills mutant: missing validation check on before/after states that allows invalid state to pass."""
        valid = _make_state([])
        invalid = _make_state([])
        invalid["schema"] = "invalid_schema"
        with self.assertRaises(schema.StateError):
            diff.diff_states(invalid, valid)
        with self.assertRaises(schema.StateError):
            diff.diff_states(valid, invalid)

    def test_reparenting_with_same_id(self):
        """Kills mutant: reparenting (parent field change) is ignored or misclassified in changed fields."""
        n1_before = [{"id": "0000000000000001", "depth": 0, "parent": None},
                     {"id": "0000000000000002", "depth": 1, "parent": "0000000000000001"}]
        n1_after = [{"id": "0000000000000001", "depth": 0, "parent": None},
                    {"id": "0000000000000002", "depth": 0, "parent": None}]
        st1 = _make_state(n1_before)
        st2 = _make_state(n1_after)

        d = diff.diff_states(st1, st2)
        self.assertEqual(d["counts"]["changed"], 1)
        ch = d["changed"][0]
        self.assertEqual(ch["id"], "0000000000000002")
        self.assertEqual(ch["fields"], ["depth", "parent"])
        self.assertEqual(ch["before"], {"depth": 1, "parent": "0000000000000001"})
        self.assertEqual(ch["after"], {"depth": 0, "parent": None})

    def test_node_moving_order_but_not_content(self):
        """Kills mutant: order changes within nodes list falsely triggers changed entry or unsorted output order."""
        a = {"id": "0000000000000001", "name": "A"}
        b = {"id": "0000000000000002", "name": "B"}
        st1 = _make_state([a, b])
        st2 = _make_state([b, a])

        d = diff.diff_states(st1, st2)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["removed"], [])
        self.assertEqual(d["changed"], [])
        self.assertEqual(d["layout_only"], [])
        self.assertEqual(d["counts"], {"added": 0, "removed": 0, "changed": 0})

    def test_state_attr_removed_vs_set_to_false_string(self):
        """Kills mutant: attribute removal confused with attribute set to 'false' string in states_flipped."""
        st1 = _make_state([{"id": "0000000000000001", "states": {"expanded": "true", "checked": "false"}}])
        st2 = _make_state([{"id": "0000000000000001", "states": {"expanded": "false"}}])

        d = diff.diff_states(st1, st2)
        self.assertEqual(d["counts"]["changed"], 1)
        self.assertIn("0000000000000001", d["states_flipped"])
        self.assertEqual(d["states_flipped"]["0000000000000001"], ["checked", "expanded"])

        st3 = _make_state([{"id": "0000000000000001", "states": {"expanded": "false", "checked": "false"}}])
        d2 = diff.diff_states(st1, st3)
        self.assertEqual(d2["states_flipped"]["0000000000000001"], ["expanded"])

    def test_box_null_to_value_is_layout_only(self):
        """Kills mutant: box-only change leaking into changed list or box change combined with semantic change being marked layout_only."""
        st1 = _make_state([{"id": "0000000000000001", "box": None}])
        st2 = _make_state([{"id": "0000000000000001", "box": [10, 20, 100, 200]}])

        d = diff.diff_states(st1, st2)
        self.assertEqual(d["layout_only"], ["0000000000000001"])
        self.assertEqual(d["changed"], [])
        self.assertEqual(d["counts"]["changed"], 0)

        # Mixed box + semantic change
        st3 = _make_state([{"id": "0000000000000001", "name": "New Name", "box": [10, 20, 100, 200]}])
        d2 = diff.diff_states(st1, st3)
        self.assertEqual(d2["layout_only"], [])
        self.assertEqual(d2["counts"]["changed"], 1)
        self.assertEqual(d2["changed"][0]["fields"], ["box", "name"])

    def test_200_random_edit_scripts(self):
        """Kills mutant: off-by-one in added/removed counts or false diff when diffing state against itself."""
        rng = random.Random(7)
        for trial in range(200):
            num_nodes = rng.randint(1, 15)
            nodes_base = [{"id": "%016x" % (i + 1), "name": "node_%d" % i, "actionable": rng.choice([True, False])}
                          for i in range(num_nodes)]
            st_base = _make_state(nodes_base)

            # Self diff must be empty
            d_self = diff.diff_states(st_base, st_base)
            self.assertEqual(d_self["added"], [])
            self.assertEqual(d_self["removed"], [])
            self.assertEqual(d_self["changed"], [])
            self.assertEqual(d_self["layout_only"], [])
            self.assertEqual(d_self["counts"], {"added": 0, "removed": 0, "changed": 0})

            # Random edit script
            # Select subset for before and after
            indices_before = sorted(rng.sample(range(num_nodes), rng.randint(0, num_nodes)))
            indices_after = sorted(rng.sample(range(num_nodes), rng.randint(0, num_nodes)))

            nodes_before = [nodes_base[i] for i in indices_before]
            nodes_after = [nodes_base[i] for i in indices_after]

            st_before = _make_state(nodes_before)
            st_after = _make_state(nodes_after)

            d_edit = diff.diff_states(st_before, st_after)

            set_before = {n["id"] for n in nodes_before}
            set_after = {n["id"] for n in nodes_after}

            expected_added = [n["id"] for n in nodes_after if n["id"] not in set_before]
            expected_removed = [n["id"] for n in nodes_before if n["id"] not in set_after]

            self.assertEqual(d_edit["added"], expected_added)
            self.assertEqual(d_edit["removed"], expected_removed)
            self.assertEqual(d_edit["counts"]["added"], len(expected_added))
            self.assertEqual(d_edit["counts"]["removed"], len(expected_removed))

    def test_diff_sha256_differs_when_any_listed_field_differs(self):
        """Kills mutant: diff_sha256 computation omitting fields or returning a constant/mock hash."""
        st1 = _make_state([{"id": "0000000000000001", "name": "A"}])
        st2 = _make_state([{"id": "0000000000000001", "name": "B"}])
        st3 = _make_state([{"id": "0000000000000001", "name": "C"}])

        d1 = diff.diff_states(st1, st2)
        d2 = diff.diff_states(st1, st3)

        self.assertNotEqual(d1["diff_sha256"], d2["diff_sha256"])
        self.assertEqual(len(d1["diff_sha256"]), 64)

    def test_summarize_ascii_only(self):
        """Kills mutant: summarize returning non-ASCII characters, unformatted objects, or exceeding 200 bytes."""
        diff_obj = {
            "counts": {"added": 2, "removed": 1, "changed": 3}
        }
        summary = diff.summarize(diff_obj)
        self.assertIsInstance(summary, str)
        self.assertLessEqual(len(summary.encode("ascii")), 200)
        summary.encode("ascii")  # Raises UnicodeEncodeError if not pure ASCII
        self.assertEqual(summary, "plus 2 minus 1 changed 3")


if __name__ == "__main__":
    unittest.main()
