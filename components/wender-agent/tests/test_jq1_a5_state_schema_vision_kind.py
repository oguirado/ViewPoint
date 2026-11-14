import json
import unittest
from pathlib import Path

from wender.perceive import ax
from wender.state import schema


def make_submit_button_state(kind="cdp"):
    node = {
        "role": "button",
        "name": "Submit",
        "attrs": (),
        "depth": 0,
        "ordinal": 0,
        "ancestors": (),
    }
    node["id"] = ax.stable_id(node)
    nodes = [node]
    return schema.build_state(
        nodes,
        kind=kind,
        url="file:///submit_test",
        title="Submit Page",
        ax_sha256=ax.snapshot_sha(nodes),
        ax_stable_sha256=ax.snapshot_sha(nodes, False),
        prune={"name": ax.PRUNE_NAME, "version": 1},
        raw_count=1,
        boxes={node["id"]: [20, 20, 80, 32]},
        viewport={"w": 1024, "h": 768},
        actionable_roles=ax.ACTIONABLE_ROLES,
    )


class TestA5StateSchemaVisionKind(unittest.TestCase):
    def test_ordinary_cdp_and_atspi_accepted(self):
        for kind in ("cdp", "atspi"):
            s = make_submit_button_state(kind=kind)
            self.assertEqual(schema.validate_state(s), [])
            self.assertEqual(s["source"]["kind"], kind)
            self.assertEqual(s["nodes"][0]["name"], "Submit")
            self.assertEqual(s["nodes"][0]["role"], "button")
            self.assertEqual(s["nodes"][0]["depth"], 0)
            self.assertEqual(s["nodes"][0]["ordinal"], 0)
            self.assertEqual(s["nodes"][0]["box"], [20, 20, 80, 32])

    def test_canonical_dumps_float_free(self):
        s = make_submit_button_state("cdp")
        raw_json = schema.dumps(s)
        self.assertIsInstance(raw_json, str)
        self.assertEqual(raw_json, schema.dumps(s))
        with self.assertRaises(schema.StateError):
            schema.dumps({"float_val": 1.5})

    def test_unrelated_unknown_kind_rejected(self):
        s = make_submit_button_state("cdp")
        s["source"]["kind"] = "unrelated_unknown_kind"
        errs = schema.validate_state(s)
        self.assertTrue(any("source.kind" in e for e in errs))

    # A5-D1: Pixel perception is admitted as a distinct source provenance.
    def test_a5_d1_build_state_vision_kind_validates(self):
        # A5-D1: Assert build_state with kind vision validates without errors
        s = make_submit_button_state(kind="vision")
        self.assertEqual(schema.validate_state(s), [])

    def test_a5_d1_python_source_kinds_admits_vision(self):
        # A5-D1: Assert Python SOURCE_KINDS admits vision
        self.assertIn("vision", schema.SOURCE_KINDS)

    def test_a5_d1_json_schema_source_kind_enum_admits_vision(self):
        # A5-D1: Assert repository JSON schema source-kind enum admits vision
        schema_path = Path(schema.__file__).parent / "wender.state.v1.schema.json"
        js = json.loads(schema_path.read_text("utf-8"))
        enum_kinds = js["properties"]["source"]["properties"]["kind"]["enum"]
        self.assertIn("vision", enum_kinds)
        self.assertEqual(enum_kinds, list(schema.SOURCE_KINDS))


if __name__ == "__main__":
    unittest.main()
