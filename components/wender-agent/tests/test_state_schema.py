import json
import unittest
from pathlib import Path

from wender.perceive import ax
from wender.state import schema
from tests.test_ax import fixture


def make():
    nodes = ax.prune(fixture())
    return schema.build_state(
        nodes, kind="cdp", url="file:///x", title="Doc", ax_sha256=ax.snapshot_sha(nodes),
        ax_stable_sha256=ax.snapshot_sha(nodes, False), prune={"name": ax.PRUNE_NAME, "version": 1},
        raw_count=8, boxes={nodes[2]["id"]: [1, 2, 3, 4]}, viewport={"w": 800, "h": 600},
        actionable_roles=ax.ACTIONABLE_ROLES)


class TestState(unittest.TestCase):
    def test_build_valid_and_parents(self):
        s = make()
        self.assertEqual(schema.validate_state(s), [])
        ids = [n["id"] for n in s["nodes"]]
        self.assertEqual([n["parent"] for n in s["nodes"]], [None, ids[0], ids[1], ids[0]])
        self.assertEqual(s["actionable"], [ids[2]])
        self.assertEqual(s["nodes"][2]["box"], [1, 2, 3, 4])

    def test_dumps_deterministic_and_rejects_float(self):
        self.assertEqual(schema.dumps(make()), schema.dumps(make()))
        with self.assertRaises(schema.StateError):
            schema.dumps({"a": 1.5})

    def test_validator_catches_violations(self):
        s = make()
        s["nodes"][1]["box"] = [0, 0, -1, 2]
        s["counts"]["nodes"] = 99
        errs = schema.validate_state(s)
        self.assertTrue(any("box" in e for e in errs))
        self.assertTrue(any("counts" in e for e in errs))
        bad = make()
        bad["extra"] = 1
        self.assertTrue(schema.validate_state(bad))

    def test_json_schema_matches_code_keys(self):
        js = json.loads((Path(schema.__file__).parent / "wender.state.v1.schema.json").read_text())
        self.assertEqual(sorted(js["required"]), sorted(schema.TOP_KEYS))
        self.assertEqual(sorted(js["properties"]["nodes"]["items"]["required"]), sorted(schema.NODE_KEYS))
        self.assertEqual(js["$id"], schema.SCHEMA_ID)
        self.assertEqual(js["properties"]["source"]["properties"]["kind"]["enum"], list(schema.SOURCE_KINDS))

    def test_source_kinds(self):
        # godot: in-engine UI-tree exporters (Holdings BOOK_MAP R1/B4); unknown kinds still fail.
        for kind in schema.SOURCE_KINDS:
            s = make()
            s["source"]["kind"] = kind
            self.assertEqual(schema.validate_state(s), [], kind)
        s = make()
        s["source"]["kind"] = "unity"
        self.assertTrue(any("source.kind" in e for e in schema.validate_state(s)))


if __name__ == "__main__":
    unittest.main()
