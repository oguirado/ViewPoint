"""Tests for wender.state.schema_metamorphic_fuzzer across schema and diff metamorphisms."""
from copy import deepcopy
import unittest

from wender.state import schema
from wender.state import schema_metamorphic_fuzzer as smf
from wender.state.diff import diff_states


class TestSchemaMetamorphicFuzzer(unittest.TestCase):
    """Verify 64-case metamorphic campaigns and 32-attempt shrinking for all 4 invariants."""

    def test_node_permutation_metamorphism_64_cases(self):
        res = smf.run_metamorphic_campaign(1337, "node_permutation", cases_per_invariant=64)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["valid_cases"], 64)
        self.assertIsNone(res["counterexample"])

        # Verify a concrete generated tree is genuinely reordered while keeping parent & actionable order
        sample = smf.generate_metamorphic_state(smf.derive_seed(1337, "node_permutation", 0), min_nodes=5, max_nodes=7)
        permuted = smf.permute_nodes_preserving_order(sample, seed=0)
        self.assertEqual(schema.validate_state(sample), [])
        self.assertEqual(schema.validate_state(permuted), [])
        self.assertNotEqual([n["id"] for n in sample["nodes"]], [n["id"] for n in permuted["nodes"]])
        self.assertEqual(sample["actionable"], permuted["actionable"])
        d = diff_states(sample, permuted)
        self.assertEqual(d["counts"], {"added": 0, "removed": 0, "changed": 0})
        self.assertEqual(d["layout_only"], [])

    def test_dpr_scaling_metamorphism_64_cases(self):
        res = smf.run_metamorphic_campaign(1337, "dpr_scaling", cases_per_invariant=64)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["valid_cases"], 64)

    def test_subtree_pruning_metamorphism_64_cases(self):
        res = smf.run_metamorphic_campaign(1337, "subtree_pruning", cases_per_invariant=64)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["valid_cases"], 64)

    def test_canonical_dumps_sha256_metamorphism_64_cases(self):
        res = smf.run_metamorphic_campaign(1337, "canonical_dumps_sha256", cases_per_invariant=64)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["valid_cases"], 64)

        # Floats are rejected by both schema.dumps and independent_canonical_json
        with self.assertRaises(schema.StateError):
            smf.independent_canonical_json({"bad": 1.25})

    def test_fault_injection_and_shrinking_across_all_invariants(self):
        # 1. Faulty node_permutation that mutates a node name when len(nodes) >= 4
        def faulty_perm_check(state):
            if len(state["nodes"]) >= 4:
                broken = smf.permute_nodes_preserving_order(state)
                broken["nodes"][-1]["name"] = broken["nodes"][-1]["name"] + "_mut"
                return smf.check_node_permutation_metamorphism(state, transform_fn=lambda _: broken)
            return smf.check_node_permutation_metamorphism(state)

        r_perm = smf.run_metamorphic_campaign(
            7001,
            "node_permutation",
            cases_per_invariant=64,
            max_shrink_attempts=32,
            check_fn=faulty_perm_check,
        )
        self.assertEqual(r_perm["status"], "counterexample")
        self.assertEqual(r_perm["invariant"], "node_permutation")
        self.assertEqual(r_perm["seed"], 7001)
        self.assertLessEqual(r_perm["shrink_attempts"], 32)
        self.assertEqual(schema.validate_state(r_perm["minimal_fixture"]), [])
        self.assertEqual(len(r_perm["minimal_fixture"]["nodes"]), 4)
        self.assertTrue(r_perm["counterexample"].reproducible)

        # 2. Faulty dpr_scaling that corrupts actionable flag when len(nodes) >= 3
        def faulty_dpr_check(state):
            def bad_scale(s):
                scaled = smf.scale_state_dpr(s, 2000000)
                if len(scaled["nodes"]) >= 3:
                    scaled["nodes"][0]["name"] = "corrupted_root"
                return scaled

            return smf.check_dpr_scaling_metamorphism(state, dpr_ppm=2000000, transform_fn=bad_scale)

        r_dpr = smf.run_metamorphic_campaign(
            7002,
            "dpr_scaling",
            cases_per_invariant=64,
            max_shrink_attempts=32,
            check_fn=faulty_dpr_check,
        )
        self.assertEqual(r_dpr["status"], "counterexample")
        self.assertEqual(r_dpr["invariant"], "dpr_scaling")
        self.assertLessEqual(r_dpr["shrink_attempts"], 32)
        self.assertEqual(schema.validate_state(r_dpr["minimal_fixture"]), [])
        self.assertEqual(len(r_dpr["minimal_fixture"]["nodes"]), 3)
        self.assertTrue(r_dpr["counterexample"].reproducible)

        # 3. Faulty subtree_pruning that omits pruned_ids when len(nodes) >= 3
        def faulty_prune_check(state):
            def bad_prune(s):
                after, pruned_ids = smf.prune_state_subtree(s)
                if len(s["nodes"]) >= 3:
                    return after, []
                return after, pruned_ids

            return smf.check_subtree_pruning_metamorphism(state, prune_fn=bad_prune)

        r_prune = smf.run_metamorphic_campaign(
            7003,
            "subtree_pruning",
            cases_per_invariant=64,
            max_shrink_attempts=32,
            check_fn=faulty_prune_check,
        )
        self.assertEqual(r_prune["status"], "counterexample")
        self.assertEqual(r_prune["invariant"], "subtree_pruning")
        self.assertLessEqual(r_prune["shrink_attempts"], 32)
        self.assertEqual(schema.validate_state(r_prune["minimal_fixture"]), [])
        self.assertEqual(len(r_prune["minimal_fixture"]["nodes"]), 3)
        self.assertTrue(r_prune["counterexample"].reproducible)

        # 4. Faulty canonical_dumps_sha256 that adds trailing whitespace
        def faulty_dumps_check(state):
            return smf.check_canonical_dumps_sha256_metamorphism(
                state,
                dump_fn=lambda obj: schema.dumps(obj) + (" " if len(state["nodes"]) >= 3 else ""),
            )

        r_dumps = smf.run_metamorphic_campaign(
            7004,
            "canonical_dumps_sha256",
            cases_per_invariant=64,
            max_shrink_attempts=32,
            check_fn=faulty_dumps_check,
        )
        self.assertEqual(r_dumps["status"], "counterexample")
        self.assertEqual(r_dumps["invariant"], "canonical_dumps_sha256")
        self.assertLessEqual(r_dumps["shrink_attempts"], 32)
        self.assertEqual(schema.validate_state(r_dumps["minimal_fixture"]), [])
        self.assertEqual(len(r_dumps["minimal_fixture"]["nodes"]), 3)
        self.assertTrue(r_dumps["counterexample"].reproducible)


if __name__ == "__main__":
    unittest.main()
