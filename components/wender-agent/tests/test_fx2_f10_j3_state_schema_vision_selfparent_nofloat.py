import json
import math
import unittest
from unittest.mock import patch

from wender.state import schema


def make_valid_chain_nodes():
    """Construct 3 pruned nodes forming a valid parent-before-child chain."""
    return [
        {
            "id": "0000000000000001",
            "role": "RootWebArea",
            "name": "Main Window",
            "depth": 0,
            "ordinal": 0,
            "attrs": (("disabled", "false"),),
            "ancestors": (),
        },
        {
            "id": "0000000000000002",
            "role": "generic",
            "name": "Container",
            "depth": 1,
            "ordinal": 0,
            "attrs": (),
            "ancestors": ("0000000000000001",),
        },
        {
            "id": "0000000000000003",
            "role": "button",
            "name": "Submit",
            "depth": 2,
            "ordinal": 0,
            "attrs": (),
            "ancestors": ("0000000000000001", "0000000000000002"),
        },
    ]


def make_valid_state(kind="cdp", nodes=None):
    if nodes is None:
        nodes = make_valid_chain_nodes()
    return schema.build_state(
        nodes,
        kind=kind,
        url="file:///test_chain",
        title="Chain Test",
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "test_prune", "version": 1},
        raw_count=len(nodes),
        boxes={
            nodes[0]["id"]: [0, 0, 1024, 768],
            nodes[1]["id"]: [10, 10, 500, 400],
            nodes[2]["id"]: [20, 20, 80, 32],
        },
        viewport={"w": 1024, "h": 768},
        actionable_roles=frozenset({"button"}),
    )


class TestFx2F10J3StateSchemaVisionSelfparentNofloat(unittest.TestCase):

    # --- Valid Parent-Before-Child Chains across Source Kinds ---

    def test_valid_parent_chain_across_existing_kinds(self):
        """Valid parent-before-child chain produces empty violations and deterministic canonical JSON."""
        for kind in ("cdp", "atspi", "godot"):
            s = make_valid_state(kind=kind)
            errs = schema.validate_state(s)
            self.assertEqual(errs, [], f"Expected no errors for valid state with kind '{kind}'")
            node_ids = [n["id"] for n in s["nodes"]]
            self.assertEqual(s["nodes"][0]["parent"], None)
            self.assertEqual(s["nodes"][1]["parent"], node_ids[0])
            self.assertEqual(s["nodes"][2]["parent"], node_ids[1])

            raw_json = schema.dumps(s)
            self.assertIsInstance(raw_json, str)
            self.assertEqual(raw_json, schema.dumps(s))

    def test_vision_kind_validates(self):
        """Pending vision kind state validation (A5-D1)."""
        s = make_valid_state(kind="vision")
        errs = schema.validate_state(s)
        self.assertEqual(errs, [])

    # --- Node Parent Reference Mutators & Violations ---

    def test_later_node_self_parent_rejected(self):
        """Mutate later node (node 1) to self-parent. Must be rejected by validate_state."""
        s = make_valid_state(kind="cdp")
        s["nodes"][1]["parent"] = s["nodes"][1]["id"]
        errs = schema.validate_state(s)
        self.assertTrue(errs, "Expected error for later-node self-parent")
        self.assertTrue(any("nodes[1].parent must precede the node" in e for e in errs))

    def test_first_node_self_parent_rejected(self):
        """Mutate root node (node 0) to self-parent. Must be rejected by validate_state."""
        s = make_valid_state(kind="cdp")
        s["nodes"][0]["parent"] = s["nodes"][0]["id"]
        errs = schema.validate_state(s)
        self.assertTrue(errs, "Expected error for root-node self-parent")
        self.assertTrue(any("nodes[0].parent must precede the node" in e for e in errs))

    def test_forward_parent_cycle_rejected(self):
        """Mutate node 0 parent to point to node 1 ID (forward reference cycle). Validated as error."""
        s = make_valid_state(kind="cdp")
        s["nodes"][0]["parent"] = s["nodes"][1]["id"]
        errs = schema.validate_state(s)
        self.assertTrue(errs, "Expected error for forward-parent reference")
        self.assertTrue(any("nodes[0].parent must precede the node" in e for e in errs))

    def test_duplicate_node_id_rejected(self):
        """Mutate node 1 ID to duplicate node 0 ID. Validated as error."""
        s = make_valid_state(kind="cdp")
        s["nodes"][1]["id"] = s["nodes"][0]["id"]
        errs = schema.validate_state(s)
        self.assertTrue(errs, "Expected error for duplicate node ID")
        self.assertTrue(any("nodes[1].id duplicated" in e for e in errs))

    # --- Float Value and Float Key Serialization Controls ---

    def test_float_value_nested_dicts_lists_tuples_rejected(self):
        """Float values in nested dicts, lists, and tuples raise StateError with exact path."""
        payload_dict = {"level1": {"level2": 3.14}}
        with self.assertRaises(schema.StateError) as cm:
            schema.dumps(payload_dict)
        self.assertIn("$.level1.level2", str(cm.exception))

        payload_list = {"items": [10, [20, 1.5]]}
        with self.assertRaises(schema.StateError) as cm:
            schema.dumps(payload_list)
        self.assertIn("$.items[1][1]", str(cm.exception))

        payload_tuple = {"tuple_data": (1, (2, 9.99))}
        with self.assertRaises(schema.StateError) as cm:
            schema.dumps(payload_tuple)
        self.assertIn("$.tuple_data[1][1]", str(cm.exception))

    def test_nonfinite_float_values_rejected(self):
        """Nonfinite float values (inf, -inf, nan) raise StateError."""
        for val in (float("inf"), float("-inf"), float("nan"), math.inf, -math.inf, math.nan):
            with self.assertRaises(schema.StateError):
                schema.dumps({"val": val})

    def test_top_level_float_key_rejected(self):
        """Float dict key at top level raises StateError before json.dumps delegation."""
        with self.assertRaises(schema.StateError):
            schema.dumps({1.5: "value"})

    def test_nested_float_key_rejected(self):
        """Nested float dict key in dict/list/tuple raises StateError."""
        payload_nested = {"outer": {0.25: "nested_value"}}
        with self.assertRaises(schema.StateError):
            schema.dumps(payload_nested)

        payload_list = [{"a": 1}, {3.14: "key_in_list"}]
        with self.assertRaises(schema.StateError):
            schema.dumps(payload_list)

        payload_tuple = ({"a": 1}, {0.5: "key_in_tuple"})
        with self.assertRaises(schema.StateError):
            schema.dumps(payload_tuple)

    def test_mixed_float_and_string_keys_rejected(self):
        """Dict containing mixed string and float keys raises StateError."""
        payload = {"str_key": 100, 2.718: "float_key"}
        with self.assertRaises(schema.StateError):
            schema.dumps(payload)

    # --- Instrumentation of json.dumps Delegation ---

    @patch("wender.state.schema.json.dumps")
    def test_dumps_instrumented_json_dumps_not_called_on_float_value(self, mock_json_dumps):
        """schema.dumps raises StateError on float value without calling json.dumps."""
        with self.assertRaises(schema.StateError):
            schema.dumps({"key": 4.2})
        mock_json_dumps.assert_not_called()

    @patch("wender.state.schema.json.dumps")
    def test_dumps_instrumented_json_dumps_not_called_on_float_key(self, mock_json_dumps):
        """schema.dumps raises StateError on float dict key without calling json.dumps."""
        with self.assertRaises(schema.StateError):
            schema.dumps({4.2: "value"})
        mock_json_dumps.assert_not_called()

    # --- Healthy Controls: Integer, Boolean, Unknown Kind, and Key/Node Order ---

    def test_integer_boolean_and_unknown_kind_controls(self):
        """Integer/boolean differentiation, unknown source kinds, and key/node order controls."""
        self.assertTrue(schema._is_int(0))
        self.assertTrue(schema._is_int(42))
        self.assertTrue(schema._is_int(-10))
        self.assertFalse(schema._is_int(True))
        self.assertFalse(schema._is_int(False))

        s = make_valid_state(kind="cdp")
        s["source"]["kind"] = "unity"
        errs = schema.validate_state(s)
        self.assertTrue(any("source.kind" in e for e in errs))

        s_unrelated = make_valid_state(kind="cdp")
        s_unrelated["source"]["kind"] = "unrelated_unknown"
        errs_unrelated = schema.validate_state(s_unrelated)
        self.assertTrue(any("source.kind" in e for e in errs_unrelated))

        # Test canonical serialization key sorting order preservation
        unordered = {"z": 1, "a": 2, "m": 3}
        dumped = schema.dumps(unordered)
        self.assertEqual(dumped, '{"a":2,"m":3,"z":1}')


if __name__ == "__main__":
    unittest.main()
