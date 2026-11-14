"""tests/test_jq1_a1_adv_gm_real_module_zero_wrong.py - Exercise eval.v2x.gm_bands_hv through adv_gm.score."""

import unittest
from eval.v2x.adv_gm import cases, score


class TestAdvGmRealModuleZeroWrong(unittest.TestCase):

    def _verify_score_result(self, res, include_1080p):
        gen_cases = cases(seed=0x5EED, include_1080p=include_1080p)

        self.assertEqual(res["status"], "OK")
        self.assertIsNone(res["error"])

        expected_total_pairs = len(gen_cases)
        self.assertGreater(expected_total_pairs, 0)
        self.assertEqual(res["total"]["pairs"], expected_total_pairs)
        self.assertEqual(res["total"]["wrong_vectors"], 0)
        self.assertEqual(res["total"]["static_relabels"], 0)

        self.assertEqual(
            res["total"]["pairs"],
            res["total"]["exact"] + res["total"]["silent"] + res["total"]["wrong_vectors"]
        )

        kinds = res["kinds"]
        self.assertGreater(len(kinds), 0)

        for case_item in gen_cases:
            kind = case_item["kind"]
            self.assertIn(kind, kinds)

        for kind, kstat in kinds.items():
            expected_kind_pairs = sum(1 for c in gen_cases if c["kind"] == kind)
            self.assertGreater(expected_kind_pairs, 0)
            self.assertEqual(kstat["pairs"], expected_kind_pairs)
            self.assertEqual(kstat["wrong_vectors"], 0)
            self.assertEqual(kstat["static_relabels"], 0)
            self.assertEqual(
                kstat["pairs"],
                kstat["exact"] + kstat["silent"] + kstat["wrong_vectors"]
            )

    def test_adv_gm_real_module_no_1080p(self):
        res = score("eval.v2x.gm_bands_hv", seed=0x5EED, include_1080p=False)
        self._verify_score_result(res, include_1080p=False)

    def test_adv_gm_real_module_with_1080p(self):
        res = score("eval.v2x.gm_bands_hv", seed=0x5EED, include_1080p=True)
        self._verify_score_result(res, include_1080p=True)


if __name__ == "__main__":
    unittest.main()
