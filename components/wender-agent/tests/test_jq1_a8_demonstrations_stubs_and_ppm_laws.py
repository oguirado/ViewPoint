"""Unit tests for wender.learn.demonstrations stub contracts and reproduced_share_ppm laws."""

import itertools
import unittest

from wender.learn import demonstrations


class TestDemonstrationsStubsAndPpmLaws(unittest.TestCase):
    def test_stubs_raise_not_implemented_error(self):
        """Verify the four demonstration stubs raise exact NotImplementedError messages."""
        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.segment("in_memory_video_data")
        self.assertEqual(str(cm.exception), "v0 stub: vision-engine segmentation")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.label_intent(["frame1", "frame2"])
        self.assertEqual(str(cm.exception), "v0 stub: multimodal intent labelling")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.map_to_actions("intent_label")
        self.assertEqual(str(cm.exception), "v0 stub: reasoner -> structured actions")

        with self.assertRaises(NotImplementedError) as cm:
            demonstrations.replay_and_verify([{"path": "ax"}], {"expected": "state"})
        self.assertEqual(str(cm.exception), "v0 stub: headless replay + verification")

    def test_reproduced_share_ppm_strict_boolean_four_elements(self):
        """Combine true/true outcome, integer 1/1, string true/true, false/true and require 250000."""
        segments = [
            {"reproduced": True, "verified": True},
            {"reproduced": 1, "verified": 1},
            {"reproduced": "true", "verified": "true"},
            {"reproduced": False, "verified": True},
        ]
        result = demonstrations.reproduced_share_ppm(segments)
        self.assertEqual(result, 250000)
        self.assertIs(type(result), int)

    def test_reproduced_share_ppm_thirds(self):
        """Verify one of three gives 333333, two of three 666666, three of three 1000000, all type int."""
        one_of_three = [
            {"reproduced": True, "verified": True},
            {"reproduced": False, "verified": True},
            {"reproduced": True, "verified": False},
        ]
        res1 = demonstrations.reproduced_share_ppm(one_of_three)
        self.assertEqual(res1, 333333)
        self.assertIs(type(res1), int)

        two_of_three = [
            {"reproduced": True, "verified": True},
            {"reproduced": True, "verified": True},
            {"reproduced": False, "verified": True},
        ]
        res2 = demonstrations.reproduced_share_ppm(two_of_three)
        self.assertEqual(res2, 666666)
        self.assertIs(type(res2), int)

        three_of_three = [
            {"reproduced": True, "verified": True},
            {"reproduced": True, "verified": True},
            {"reproduced": True, "verified": True},
        ]
        res3 = demonstrations.reproduced_share_ppm(three_of_three)
        self.assertEqual(res3, 1000000)
        self.assertIs(type(res3), int)

    def test_deterministic_permutations_preserve_result(self):
        """Verify deterministic permutations of segment cohorts produce identical ppm results."""
        four_items = [
            {"reproduced": True, "verified": True},
            {"reproduced": 1, "verified": 1},
            {"reproduced": "true", "verified": "true"},
            {"reproduced": False, "verified": True},
        ]
        for perm in itertools.permutations(four_items):
            self.assertEqual(demonstrations.reproduced_share_ppm(list(perm)), 250000)

        two_of_three = [
            {"reproduced": True, "verified": True},
            {"reproduced": True, "verified": True},
            {"reproduced": False, "verified": True},
        ]
        for perm in itertools.permutations(two_of_three):
            self.assertEqual(demonstrations.reproduced_share_ppm(list(perm)), 666666)

    def test_missing_verified_and_nested_evidence_contribute_zero(self):
        """Missing verified key and skill-shaped nested evidence without top-level flags contribute zero."""
        missing_verified = {"reproduced": True}
        nested_evidence = {
            "evidence": {
                "reproduced": True,
                "verified": True,
                "verified_by": "scene_diff",
            }
        }

        self.assertEqual(demonstrations.reproduced_share_ppm([missing_verified]), 0)
        self.assertEqual(demonstrations.reproduced_share_ppm([nested_evidence]), 0)

        mixed_cohort = [
            {"reproduced": True, "verified": True},
            missing_verified,
            nested_evidence,
        ]
        res = demonstrations.reproduced_share_ppm(mixed_cohort)
        self.assertEqual(res, 333333)
        self.assertIs(type(res), int)


if __name__ == "__main__":
    unittest.main()
