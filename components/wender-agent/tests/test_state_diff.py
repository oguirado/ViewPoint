"""STATE diff contract; synthetic trees only, without desktop or browser access."""
import copy
import hashlib
import unittest

from wender.state import schema
from wender.state.diff import diff_states, summarize


def node(number, *, role="button", name="Run", attrs=(), depth=0):
    return {"id": "%016x" % number, "role": role, "name": name,
            "attrs": list(attrs), "depth": depth, "ordinal": 0}


def state(nodes=(), boxes=None, stable="a" * 64):
    return schema.build_state(
        nodes, kind="cdp", url="", title="Test", ax_sha256="b" * 64,
        ax_stable_sha256=stable, prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(nodes), boxes=boxes, actionable_roles={"button", "checkbox"})


class TestStateDiff(unittest.TestCase):
    def test_identical_and_canonical_digest(self):
        before = state([node(1)])
        original = copy.deepcopy(before)
        result = diff_states(before, before)
        for key in ("added", "removed", "changed", "layout_only", "actionable_gained",
                    "actionable_lost"):
            self.assertEqual(result[key], [], key)
        self.assertEqual(result["states_flipped"], {})
        self.assertEqual(result["counts"], {"added": 0, "removed": 0, "changed": 0})
        self.assertTrue(result["same_chassis"])
        self.assertEqual(result["schema"], "wender.state_diff/v1")
        payload = {k: v for k, v in result.items() if k != "diff_sha256"}
        self.assertEqual(result["diff_sha256"],
                         hashlib.sha256(schema.dumps(payload).encode("utf-8")).hexdigest())
        self.assertEqual(result["diff_sha256"],
                         "01f3a815817dfe6482d43b4c70203b4d5f890867ad3cf98d8b5887c387fcafb7")
        self.assertEqual(schema.dumps(result), schema.dumps(diff_states(before, before)))
        self.assertEqual(before, original)

    def test_checked_flip_and_state_key_presence(self):
        before = state([node(1, role="checkbox", attrs=[("checked", "false"), ("busy", "true")])])
        after = state([node(1, role="checkbox", attrs=[("checked", "true"), ("selected", "true")])])
        result = diff_states(before, after)
        self.assertEqual(result["states_flipped"], {"0000000000000001": ["busy", "checked", "selected"]})
        self.assertEqual(result["changed"][0]["fields"], ["states"])
        self.assertEqual(result["changed"][0]["before"], {"states": before["nodes"][0]["states"]})
        self.assertEqual(result["changed"][0]["after"], {"states": after["nodes"][0]["states"]})
        self.assertTrue(result["same_chassis"])
        result["changed"][0]["after"]["states"]["checked"] = "false"
        self.assertEqual(after["nodes"][0]["states"]["checked"], "true")

    def test_document_order_add_remove_and_actionability(self):
        before = state([node(3), node(1), node(2)])
        after = state([node(5, role="dialog"), node(4, depth=1),
                       node(2, attrs=[("disabled", "true")])], stable="c" * 64)
        result = diff_states(before, after)
        self.assertEqual(result["added"], ["0000000000000005", "0000000000000004"])
        self.assertEqual(result["removed"], ["0000000000000003", "0000000000000001"])
        self.assertEqual(result["actionable_gained"], ["0000000000000004"])
        self.assertEqual(result["actionable_lost"], ["0000000000000003", "0000000000000001", "0000000000000002"])
        self.assertEqual(result["changed"][0]["fields"], ["actionable", "states"])
        self.assertFalse(result["same_chassis"])
        self.assertEqual(result["counts"], {"added": 2, "removed": 2, "changed": 1})

    def test_box_only_and_mixed_change(self):
        key = "0000000000000001"
        before = state([node(1)], {key: [0, 10, 20, 30]})
        after = state([node(1)], {key: [0, 0, 20, 30]})
        result = diff_states(before, after)
        self.assertEqual(result["layout_only"], [key])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["counts"]["changed"], 0)
        after["nodes"][0]["name"] = "Stop"
        result = diff_states(before, after)
        self.assertEqual(result["layout_only"], [])
        self.assertEqual(result["changed"][0]["fields"], ["box", "name"])
        self.assertEqual(result["changed"][0]["after"], {"box": [0, 0, 20, 30], "name": "Stop"})

    def test_parent_depth_and_changed_order(self):
        before = state([node(1), node(2), node(3)])
        after = state([node(3, name="First"), node(1, depth=1), node(2, depth=2)])
        result = diff_states(before, after)
        self.assertEqual([n["id"] for n in result["changed"]],
                         ["0000000000000003", "0000000000000001", "0000000000000002"])
        self.assertEqual(result["changed"][1]["fields"], ["depth", "parent"])
        # Returned mutable values must not alias either input.
        result["changed"][1]["after"]["depth"] = 100
        self.assertEqual(after["nodes"][1]["depth"], 1)

    def test_invalid_either_side(self):
        valid = state([node(1)])
        invalid = copy.deepcopy(valid)
        invalid["nodes"][0]["box"] = [0, 0, -1, 0]
        for before, after in ((invalid, valid), (valid, invalid), ({}, valid), (valid, None)):
            with self.subTest(before=before, after=after):
                with self.assertRaises(schema.StateError):
                    diff_states(before, after)

    def test_summary_large_ascii_and_bounded(self):
        before = state([node(i) for i in range(500)])
        after = state([node(i, name="\u00e9\nchanged") for i in range(500)])
        result = diff_states(before, after)
        text = summarize(result)
        self.assertEqual(text, "plus 0 minus 0 changed 500")
        self.assertLessEqual(len(text.encode("ascii")), 200)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
