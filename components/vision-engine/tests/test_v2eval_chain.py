"""Unit tests for combined post-pass chain evaluation harness."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from eval.gui_synth.scenes import build
from eval.v2eval.chain import run_chain
from eval.v2eval.postpass import apply_claims
from visioncore.desc import TILE_DTYPE
from visioncore.grid import Grid


class TestV2EvalChain(unittest.TestCase):
    def test_precedence_rules_hand_built_claim_masks(self):
        baseline = {
            "cls": np.zeros(4, dtype=np.int64),
            "mv_dx": np.zeros(4, dtype=np.int64),
            "mv_dy": np.zeros(4, dtype=np.int64),
        }
        gm_claim = {
            "mask": np.array([True, True, False, False]),
            "cls": 3,
            "mv_dx": np.array([1, 1, 0, 0], dtype=np.int64),
            "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
        }
        fl_claim = {
            "mask": np.array([False, True, True, False]),
            "cls": 4,
            "mv_dx": np.array([0, 0, 0, 0], dtype=np.int64),
            "mv_dy": np.array([0, 0, 0, 0], dtype=np.int64),
        }

        # gm_then_fl: FL cannot overwrite tile 1 (already claimed by GM)
        fl_eff_gm = {
            "mask": fl_claim["mask"] & (~gm_claim["mask"]),
            "cls": 4,
            "mv_dx": fl_claim["mv_dx"],
            "mv_dy": fl_claim["mv_dy"],
        }
        out_gm_fl = apply_claims(baseline, [gm_claim, fl_eff_gm])
        np.testing.assert_array_equal(out_gm_fl["cls"], [3, 3, 4, 0])

        # fl_then_gm: GM cannot overwrite tile 1 (already claimed by FL)
        gm_eff_fl = {
            "mask": gm_claim["mask"] & (~fl_claim["mask"]),
            "cls": 3,
            "mv_dx": gm_claim["mv_dx"],
            "mv_dy": gm_claim["mv_dy"],
        }
        out_fl_gm = apply_claims(baseline, [fl_claim, gm_eff_fl])
        np.testing.assert_array_equal(out_fl_gm["cls"], [3, 4, 4, 0])

    def test_dev_only_guard_rejects_non_dev_scene(self):
        non_dev_scene = build("static_clean", 0, w=32, h=32, n=3, split="TEST")
        with self.assertRaises(ValueError):
            run_chain([non_dev_scene], "gm=tests.test_v2eval.toy_variants", "fl=tests.test_v2eval.toy_variants")

    def test_determinism_and_flicker_false_positives(self):
        scene = build("static_clean", 0, w=32, h=32, n=3, split="DEV")
        res1 = run_chain([scene], "gm=tests.test_v2eval.toy_variants", "fl=tests.test_v2eval.toy_variants")
        res2 = run_chain([scene], "gm=tests.test_v2eval.toy_variants", "fl=tests.test_v2eval.toy_variants")

        json1 = json.dumps(res1, sort_keys=True)
        json2 = json.dumps(res2, sort_keys=True)
        self.assertEqual(json1, json2)

        self.assertEqual(res1["schema"], "vqa.v2eval-chain/v1")
        self.assertEqual(res1["split"], "DEV")
        self.assertEqual(res1["measurement_tag"], "VM")

        metrics_fl = res1["variants"]["fl=tests.test_v2eval.toy_variants"]["metrics"]
        self.assertIsNotNone(metrics_fl)
        self.assertIn("flicker_false_positives", metrics_fl)
        self.assertIn("flicker_fp", metrics_fl)

    def test_failed_reporting(self):
        scene = build("scroll_h", 0, w=32, h=32, n=3, split="DEV")
        res = run_chain([scene], "gm=tests.test_v2eval.bad_variants", "fl=tests.test_v2eval.toy_variants")
        self.assertEqual(res["variants"]["gm=tests.test_v2eval.bad_variants"]["status"], "FAILED")
        self.assertEqual(res["variants"]["gm_then_fl"]["status"], "FAILED")
        self.assertIsNone(res["variants"]["gm_then_fl"]["metrics"])


if __name__ == "__main__":
    unittest.main()
