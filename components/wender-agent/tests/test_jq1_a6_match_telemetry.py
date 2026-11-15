"""Adversarial claim-pinning tests for wender.perceive.ax.match telemetry contracts."""
import unittest


class TestJq1A6MatchTelemetry(unittest.TestCase):
    def test_l2_name_telemetry_fallback_defect_s1_f8(self):
        """Pin defect S1-F8: L2_NAME records candidates == 0 in ladder_tried when falling back to at_ord despite selecting 1 candidate.

        Defect S1-F8: wender/perceive/ax.py:281-285 records len(others) == 0 in
        ladder_tried when falling back to at_ord, docs/spec/WA_S1_PERCEIVE_CONTRACTS.md:101,282.
        """
        from wender.perceive import ax

        raw_nodes = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Send"}},
        ]
        nodes = ax.prune(raw_nodes)

        # entry with matching role, name, ordinal, but mismatched exact and stable fingerprints
        entry = {
            "role": "button",
            "name": "Send",
            "ordinal": 0,
            "fp_exact": "0" * 16,
            "fp_stable": "0" * 16,
        }

        res = ax.match(nodes, entry)

        self.assertEqual(res["level"], "L2_NAME")
        self.assertEqual(res["candidates"], 1)

        l2_entry = next(item for item in res["ladder_tried"] if item.get("level") == "L2_NAME")
        # Under the corrected contract, candidates tried at L2_NAME when matching candidate(s)
        # exist must report candidates == 1, matching res["candidates"] == 1.
        self.assertEqual(l2_entry["candidates"], 1)

    def test_l4_unique_role_contract(self):
        """Healthy control: L4_UNIQUE_ROLE matches when name differs and role is unique."""
        from wender.perceive import ax

        raw_nodes = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Submit"}},
        ]
        nodes = ax.prune(raw_nodes)

        entry = {
            "role": "button",
            "name": "Send",
            "ordinal": 0,
            "fp_exact": "0" * 16,
            "fp_stable": "0" * 16,
        }

        res = ax.match(nodes, entry)

        self.assertEqual(res["level"], "L4_UNIQUE_ROLE")
        self.assertEqual(res["candidates"], 1)
        self.assertIsNotNone(res["node"])
        self.assertEqual(res["node"]["name"], "Submit")
        self.assertFalse(res["tie"])

        expected_ladder = [
            {"level": "L0_EXACT", "candidates": 0},
            {"level": "L1_STABLE", "candidates": 0},
            {"level": "L2_NAME", "candidates": 0},
            {"level": "L3_VARIANT", "candidates": 0},
            {"level": "L4_UNIQUE_ROLE", "candidates": 1},
        ]
        self.assertEqual(res["ladder_tried"], expected_ladder)

    def test_match_tie_flag_contract(self):
        """Healthy control: tie=True set when candidates have identical match scores."""
        from wender.perceive import ax

        raw_nodes = [
            {"nodeId": "1", "role": {"value": "RootWebArea"}, "name": {"value": "Doc"}, "childIds": ["2", "3"]},
            {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Send"}},
            {"nodeId": "3", "role": {"value": "button"}, "name": {"value": "Send"}},
        ]
        nodes = ax.prune(raw_nodes)

        # Entry searching for button "Send" at ordinal 5 (not present at ordinal 5, so L0/L1 fail)
        entry = {
            "role": "button",
            "name": "Send",
            "ordinal": 5,
            "fp_exact": "0" * 16,
            "fp_stable": "0" * 16,
        }

        res = ax.match(nodes, entry)

        self.assertEqual(res["level"], "L2_NAME")
        self.assertEqual(res["candidates"], 2)
        self.assertTrue(res["tie"])
        self.assertIsNotNone(res["node"])

    def test_l2_name_prefers_other_ordinals_and_reports_only_that_pool(self):
        from wender.perceive import ax
        nodes = ax.prune([
            {"nodeId": str(i), "role": {"value": "button"}, "name": {"value": "Send"}}
            for i in range(3)
        ])
        res = ax.match(nodes, {"role": "button", "name": "Send", "ordinal": 0,
                               "fp_exact": "0" * 16, "fp_stable": "0" * 16})
        self.assertEqual(res["level"], "L2_NAME")
        self.assertIs(res["node"], nodes[1])
        self.assertEqual(res["candidates"], 2)
        self.assertTrue(res["tie"])
        self.assertEqual(res["ladder_tried"], [
            {"level": "L0_EXACT", "candidates": 1},
            {"level": "L1_STABLE", "candidates": 1},
            {"level": "L2_NAME", "candidates": 2},
        ])
        self.assertNotIn("established", res)

    def test_missing_fingerprint_establishment_remains_diagnostic(self):
        from wender.perceive import ax
        nodes = ax.prune([{
            "nodeId": "1", "role": {"value": "button"}, "name": {"value": "Send"},
        }])
        for exact in ({}, {"fp_exact": None}):
            with self.subTest(exact=exact):
                entry = {"role": "button", "name": "Send", "ordinal": 0}
                entry.update(exact)
                res = ax.match(nodes, entry)
                self.assertEqual(res["level"], "L0_EXACT")
                self.assertTrue(res["established"])
                self.assertEqual(res["candidates"], 1)
                self.assertEqual(res["ladder_tried"], [
                    {"level": "L0_EXACT", "candidates": 1},
                ])


if __name__ == "__main__":
    unittest.main()
