# @module {"id":"wa.tests.test_fx2_f4_j1_program_map_injectivity_and_canonical_edges","layer":1,"owner":"REMEMBER","deps":["wa.remember.program_map","wa.state.schema"]}
"""Tests for program map injectivity, canonical edge deduplication, sorting, and ownership."""
import unittest

from wender.remember.program_map import ProgramMap, state_key
from wender.state.schema import dumps


class TestProgramMapInjectivityAndCanonicalEdges(unittest.TestCase):

    def test_passing_control_rejected_missing_endpoints_preserves_serialization(self):
        """Healthy control: Rejected missing endpoints leave program map state and serialization unchanged."""
        pm = ProgramMap("offline_app_control")
        n1 = pm.observe("pantalla_main", "ax_main_01")
        json_before = pm.to_json()

        with self.assertRaises(KeyError) as ctx_pre:
            pm.add_edge("missing_pre_key", {"op": "click"}, n1)
        self.assertIn("missing_pre_key", str(ctx_pre.exception))

        with self.assertRaises(KeyError) as ctx_post:
            pm.add_edge(n1, {"op": "click"}, "missing_post_key")
        self.assertIn("missing_post_key", str(ctx_post.exception))

        self.assertEqual(len(pm.edges), 0)
        self.assertEqual(pm.to_json(), json_before)

    def test_passing_control_schema_dumps_array_order_and_member_sorting(self):
        """Healthy control: JSON serialization enforces object key sorting while preserving array order."""
        act1 = {"z_key": 1, "a_key": [10, 20]}
        act2 = {"a_key": [20, 10], "z_key": 1}
        act1_sorted_keys = {"a_key": [10, 20], "z_key": 1}

        self.assertEqual(dumps(act1), '{"a_key":[10,20],"z_key":1}')
        self.assertEqual(dumps(act1), dumps(act1_sorted_keys))
        self.assertNotEqual(dumps(act1), dumps(act2))

    def test_passing_control_duplicate_edge_deduplication(self):
        """Healthy control: Identical byte edge payloads do not create duplicate edges in list."""
        pm = ProgramMap("offline_app_control")
        n1 = pm.observe("pantalla_main", "ax_main_01")
        n2 = pm.observe("pantalla_detail", "ax_detail_01")

        action = {"path": "ax", "cmd": "tap", "target": "btn_settings"}
        changed = {"status": "ok"}

        e1 = pm.add_edge(n1, action, n2, changed)
        e2 = pm.add_edge(n1, action, n2, changed)

        self.assertEqual(len(pm.edges), 1)

    def test_typed_state_identity_delimiters_control_unicode_swapped(self):
        """Defect D-A8-01: State identity collapses delimiters, control chars, Unicode, and swapped components."""
        typed_delimiters_unicode_pairs = [
            (100, "ax_画面\t\x00_01"),
            ("100", "ax_画面\t\x00_01"),
            ("pantalla_画面:sec1\t", "sec2\x00_ax"),
            ("pantalla_画面", "sec1\t:sec2\x00_ax"),
            (False, "pantalla_画面\n_02"),
            (0, "pantalla_画面\n_02"),
        ]

        keys = [state_key(fp, ax) for fp, ax in typed_delimiters_unicode_pairs]
        self.assertEqual(
            len(keys),
            len(set(keys)),
            "state_key produced duplicate keys for distinct typed/delimited/swapped pairs",
        )

        pm = ProgramMap("app_injectivity")
        for fp, ax in typed_delimiters_unicode_pairs:
            pm.observe(fp, ax)

        self.assertEqual(
            len(pm.nodes),
            len(typed_delimiters_unicode_pairs),
            "ProgramMap lost distinct nodes due to key collision",
        )
        for fp, ax in typed_delimiters_unicode_pairs:
            k = state_key(fp, ax)
            self.assertIn(k, pm.nodes)
            self.assertEqual(pm.nodes[k]["screen_fp"], fp)
            self.assertEqual(pm.nodes[k]["ax_stable_sha"], ax)

    def test_nested_boolean_versus_integer_edge_payload_deduplication(self):
        """Defect D-A8-02: Python dict equality considers True == 1, collapsing distinct boolean/integer edge payloads."""
        pm = ProgramMap("app_typed_payloads")
        n1 = pm.observe("screen_alpha", "ax_alpha")
        n2 = pm.observe("screen_beta", "ax_beta")

        action_bool = {"params": {"flag": True, "levels": [1, 2]}}
        action_int = {"params": {"flag": 1, "levels": [1, 2]}}
        changed_bool = {"meta": {"active": True}}

        pm.add_edge(n1, action_bool, n2, changed_bool)
        pm.add_edge(n1, action_int, n2, changed_bool)

        self.assertEqual(len(pm.edges), 2, "ProgramMap collapsed edges with boolean True vs integer 1 action payloads")

    def test_canonical_graph_serialization_across_insertion_permutations(self):
        """Defect D-A8-03: Edge tie-breaking omits changed summary, causing serialization to depend on insertion order."""
        action = {"path": "ax", "cmd": "press", "target": "submit_btn"}

        pm1 = ProgramMap("app_permutations")
        n1_1 = pm1.observe("screen_main", "ax_main")
        n2_1 = pm1.observe("screen_sub", "ax_sub")

        chg_a = {"details": {"count": 10, "label": "first"}}
        chg_b = {"details": {"count": 20, "label": "second"}}
        chg_c = {"details": {"count": 30, "label": "third"}}

        pm1.add_edge(n1_1, action, n2_1, chg_a)
        pm1.add_edge(n1_1, action, n2_1, chg_b)
        pm1.add_edge(n1_1, action, n2_1, chg_c)

        pm2 = ProgramMap("app_permutations")
        n1_2 = pm2.observe("screen_main", "ax_main")
        n2_2 = pm2.observe("screen_sub", "ax_sub")

        # Permuted insertion order
        pm2.add_edge(n1_2, action, n2_2, chg_c)
        pm2.add_edge(n1_2, action, n2_2, chg_a)
        pm2.add_edge(n1_2, action, n2_2, chg_b)

        self.assertEqual(len(pm1.edges), 3)
        self.assertEqual(len(pm2.edges), 3)
        self.assertEqual(pm1.to_json(), pm2.to_json(), "to_json output differs for permuted edge insertions")

    def test_nested_ownership_and_mutation_isolation_after_access(self):
        """Defect D-A8-04: Edge payloads lack defensive copy on insertion, duplicate access, and successors retrieval."""
        pm = ProgramMap("app_ownership")
        n1 = pm.observe("screen_a", "ax_a")
        n2 = pm.observe("screen_b", "ax_b")

        action = {"ctx": {"tags": ["read", "write"]}}
        changed = {"state": {"tokens": [100, 200]}}

        e1 = pm.add_edge(n1, action, n2, changed)
        json_before = pm.to_json()

        # 1. Mutate caller's original dictionaries
        action["ctx"]["tags"].append("admin")
        changed["state"]["tokens"].append(300)

        # 2. Mutate returned successor's element dictionary
        succ = pm.successors(n1)
        succ[0]["action"]["ctx"]["tags"].clear()

        # 3. Mutate returned duplicate edge
        e_dup = pm.add_edge(n1, {"ctx": {"tags": ["read", "write"]}}, n2, {"state": {"tokens": [100, 200]}})
        e_dup["changed"]["state"]["tokens"] = [0]

        # Serialization must remain unchanged
        self.assertEqual(pm.to_json(), json_before)


if __name__ == "__main__":
    unittest.main()
