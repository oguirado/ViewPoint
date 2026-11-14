# @module {"id":"wa.tests.test_fx2_f9_j1_godot_uitree_base_and_sidecar_validator","layer":1,"owner":"PERCEIVE","deps":["wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Contract tests for wender.perceive.godot node_id_for_control_path and validate_uitree APIs.

G-T3, G-SID and G-ALIAS enforcement contracts for Godot UI-tree base and sidecar records.
"""

import copy
import hashlib
import unittest

from wender.state import schema


# @anchor wa.tests.test_fx2_f9_j1_godot_uitree_base_and_sidecar_validator.contract

CADENCE_REGISTRY = {"100ms", "1s", "tick"}
SCENE_INVENTORY = {"scr_home"}


def _path_hash(control_path: str) -> str:
    try:
        from wender.perceive.godot import node_id_for_control_path
        return node_id_for_control_path(control_path)
    except (ImportError, AttributeError):
        return hashlib.sha256(control_path.encode("utf-8")).hexdigest()[:16]


def build_valid_fixture(empty_table: bool = False):
    root_cp = "Root"
    tbl_cp = "Root.Table"
    row_cp = "Root.Table.Row0"
    cell_cp = "Root.Table.Row0.Cell0"
    chart_cp = "Root.Chart"

    root_id = _path_hash(root_cp)
    tbl_id = _path_hash(tbl_cp)
    row_id = _path_hash(row_cp)
    cell_id = _path_hash(cell_cp)
    chart_id = _path_hash(chart_cp)

    nodes = [
        {
            "id": root_id,
            "role": "window",
            "name": "scr_home",
            "depth": 0,
            "ordinal": 0,
            "parent": None,
            "states": {},
            "actionable": False,
            "box": [0, 0, 1024, 768],
        },
        {
            "id": tbl_id,
            "role": "table",
            "name": "HoldingsTable",
            "depth": 1,
            "ordinal": 0,
            "parent": root_id,
            "states": {},
            "actionable": False,
            "box": [10, 10, 500, 300],
        },
    ]

    if not empty_table:
        nodes.extend([
            {
                "id": row_id,
                "role": "row",
                "name": "Row0",
                "depth": 2,
                "ordinal": 0,
                "parent": tbl_id,
                "states": {},
                "actionable": False,
                "box": [10, 10, 500, 30],
            },
            {
                "id": cell_id,
                "role": "cell",
                "name": "SymbolCell",
                "depth": 3,
                "ordinal": 0,
                "parent": row_id,
                "states": {"focusable": "true"},
                "actionable": True,
                "box": [10, 10, 100, 30],
            },
        ])

    nodes.append({
        "id": chart_id,
        "role": "chart",
        "name": "PortfolioChart",
        "depth": 1,
        "ordinal": 1,
        "parent": root_id,
        "states": {},
        "actionable": False,
        "box": [520, 10, 400, 300],
    })

    actionable = [n["id"] for n in nodes if n["actionable"]]

    base = {
        "schema": schema.SCHEMA_ID,
        "source": {"kind": "godot", "url": "res://scenes/scr_home.tscn", "title": "scr_home"},
        "prune": {"name": "godot.prune", "version": 1},
        "ax_sha256": hashlib.sha256(schema.dumps(nodes).encode("utf-8")).hexdigest(),
        "ax_stable_sha256": hashlib.sha256(schema.dumps(nodes).encode("utf-8")).hexdigest(),
        "viewport": {"w": 1024, "h": 768},
        "nodes": nodes,
        "actionable": actionable,
        "counts": {"nodes": len(nodes), "actionable": len(actionable), "raw": len(nodes)},
    }

    base_digest = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()

    sidecar_nodes = {
        root_id: {
            "control_path": root_cp,
            "screen_id": "scr_home",
        },
        tbl_id: {
            "control_path": tbl_cp,
            "screen_id": "scr_home",
            "table_id": "tbl_holdings",
            "visible_range": (
                {"first_row_id": None, "last_row_id": None, "count": 0}
                if empty_table
                else {"first_row_id": "row_0", "last_row_id": "row_0", "count": 1}
            ),
            "query": {
                "sort_key": "symbol",
                "sort_dir": "asc",
                "filter_spec": [{"col_key": "symbol", "op": "eq", "value": "AAPL"}],
                "page_index": 0,
                "page_size": 10,
            },
            "as_of_tick": 100,
            "refresh_cadence": "100ms",
        },
    }

    if not empty_table:
        sidecar_nodes[row_id] = {
            "control_path": row_cp,
            "screen_id": "scr_home",
            "table_id": "tbl_holdings",
            "row_id": "row_0",
        }
        sidecar_nodes[cell_id] = {
            "control_path": cell_cp,
            "screen_id": "scr_home",
            "table_id": "tbl_holdings",
            "row_id": "row_0",
            "col_key": "symbol",
        }

    sidecar_nodes[chart_id] = {
        "control_path": chart_cp,
        "screen_id": "scr_home",
        "as_of_tick": 100,
        "refresh_cadence": "1s",
    }

    sidecar = {
        "schema": "holdings.godot.uitree/v1",
        "base_sha256": base_digest,
        "nodes": sidecar_nodes,
    }

    return base, sidecar


class TestGodotUiTreeValidatorContract(unittest.TestCase):

    def test_valid_nonempty_table_control(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture(empty_table=False)
        self.assertEqual(schema.validate_state(base), [])
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertEqual(errs, [])

    def test_valid_empty_table_control(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture(empty_table=True)
        self.assertEqual(schema.validate_state(base), [])
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertEqual(errs, [])

    def test_mutant_object_box(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        base["nodes"][0]["box"] = [0, 0, -1, 100]
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_boolean_states(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        base["nodes"][3]["states"] = {"focusable": True}  # type: ignore
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_path_as_id(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        old_id = base["nodes"][0]["id"]
        new_id = "Root"
        base["nodes"][0]["id"] = new_id
        sidecar["nodes"][new_id] = sidecar["nodes"].pop(old_id)
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_extra_base_node_field(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        base["nodes"][0]["godot_class"] = "Control"
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_path_numeric_first(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        chart_id = _path_hash("Root.Chart")
        sidecar["nodes"][chart_id]["control_path"] = "Root.Panel.2Btn"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_path_empty_segment(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        chart_id = _path_hash("Root.Chart")
        sidecar["nodes"][chart_id]["control_path"] = "Root..Chart"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_path_invalid_char(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        chart_id = _path_hash("Root.Chart")
        sidecar["nodes"][chart_id]["control_path"] = "Root.Chart-1"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_duplicate_id(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        dup_node = copy.deepcopy(base["nodes"][0])
        base["nodes"].append(dup_node)
        base["counts"]["nodes"] = len(base["nodes"])
        sidecar["base_sha256"] = hashlib.sha256(schema.dumps(base).encode("utf-8")).hexdigest()
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_changed_digest(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        sidecar["base_sha256"] = "0" * 64
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_same_shaped_wrong_digest(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        wrong_digest = hashlib.sha256(b"wrong base content").hexdigest()
        sidecar["base_sha256"] = wrong_digest
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_unknown_top_level_sidecar_field(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        sidecar["unknown_top_field"] = "bad"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_unknown_nested_sidecar_field(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        root_id = _path_hash("Root")
        sidecar["nodes"][root_id]["unknown_nested_field"] = True
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_orphan_sidecar_key(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        orphan_id = _path_hash("Root.Orphan")
        sidecar["nodes"][orphan_id] = {
            "control_path": "Root.Orphan",
            "screen_id": "scr_home",
        }
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_missing_sidecar_key(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        root_id = _path_hash("Root")
        del sidecar["nodes"][root_id]
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_wrong_path_hash(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        root_id = _path_hash("Root")
        sidecar["nodes"][root_id]["control_path"] = "Root.MismatchedPath"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_missing_screen_id(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        root_id = _path_hash("Root")
        del sidecar["nodes"][root_id]["screen_id"]
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_visible_range_endpoints(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["visible_range"] = {
            "first_row_id": "row_0",
            "last_row_id": None,
            "count": 0,
        }
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_visible_range_count(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["visible_range"]["count"] = -1
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_wrong_sort_direction(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["query"]["sort_dir"] = "ascending"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_malformed_filter_clause(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["query"]["filter_spec"] = [{"col_key": "symbol", "op": "ne", "value": "AAPL"}]
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_zero_page_size(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["query"]["page_size"] = 0
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_boolean_page_index(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["query"]["page_index"] = True  # type: ignore
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_boolean_tick(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        sidecar["nodes"][tbl_id]["as_of_tick"] = True  # type: ignore
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_missing_cadence(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        tbl_id = _path_hash("Root.Table")
        del sidecar["nodes"][tbl_id]["refresh_cadence"]
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_misplaced_summary_fields(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        cell_id = _path_hash("Root.Table.Row0.Cell0")
        sidecar["nodes"][cell_id]["as_of_tick"] = 100
        sidecar["nodes"][cell_id]["refresh_cadence"] = "100ms"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)

    def test_mutant_inconsistent_linkage(self):
        from wender.perceive.godot import validate_uitree
        base, sidecar = build_valid_fixture()
        cell_id = _path_hash("Root.Table.Row0.Cell0")
        sidecar["nodes"][cell_id]["table_id"] = "tbl_mismatched"
        errs = validate_uitree(base, sidecar, cadence_registry=CADENCE_REGISTRY, scene_inventory=SCENE_INVENTORY)
        self.assertTrue(len(errs) > 0)


if __name__ == "__main__":
    unittest.main()
