"""Unit tests for _joint_sad cutoff optimization decision exactness in gm_bands_hv."""

import unittest
import numpy as np
from eval.v2x import adv_gm, gm_bands_hv


class TestJointSadCutoffExactness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pairs = cls._build_pairs()

    @staticmethod
    def _build_pairs():
        all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)
        two_motions = [c for c in all_cases if c["kind"] == "two_motions"]
        periodic_text = [c for c in all_cases if c["kind"] == "periodic_text"]

        pairs = []
        for c in two_motions:
            pairs.append((c["case_id"], c["frames"][0], c["frames"][1]))
        for c in periodic_text:
            pairs.append((c["case_id"], c["frames"][0], c["frames"][1]))

        rng = np.random.default_rng(1111)

        # Custom 1: vertical shift 13 + signed noise [-3, 3] clipped into uint8
        f0_v = rng.integers(0, 256, size=(240, 320), dtype=np.uint8)
        f1_v = np.zeros_like(f0_v)
        f1_v[13:, :] = f0_v[:-13, :]
        f1_v[:13, :] = rng.integers(0, 256, size=(13, 320), dtype=np.uint8)
        noise_v = rng.integers(-3, 4, size=(240, 320), dtype=np.int16)
        f1_v_noisy = np.clip(f1_v.astype(np.int16) + noise_v, 0, 255).astype(np.uint8)
        pairs.append(("custom_v_shift_13", f0_v, f1_v_noisy))

        # Custom 2: horizontal shift 17 + signed noise [-3, 3] clipped into uint8
        f0_h = rng.integers(0, 256, size=(240, 320), dtype=np.uint8)
        f1_h = np.zeros_like(f0_h)
        f1_h[:, 17:] = f0_h[:, :-17]
        f1_h[:, :17] = rng.integers(0, 256, size=(240, 17), dtype=np.uint8)
        noise_h = rng.integers(-3, 4, size=(240, 320), dtype=np.int16)
        f1_h_noisy = np.clip(f1_h.astype(np.int16) + noise_h, 0, 255).astype(np.uint8)
        pairs.append(("custom_h_shift_17", f0_h, f1_h_noisy))

        # Custom 3: 64-square block dragged (8,8) on flat (240, 320) canvas
        f0_b = np.full((240, 320), 100, dtype=np.uint8)
        f1_b = np.full((240, 320), 100, dtype=np.uint8)
        win_tex = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
        y0, x0 = 32, 32
        f0_b[y0:y0 + 64, x0:x0 + 64] = win_tex
        f1_b[y0 + 8:y0 + 8 + 64, x0 + 8:x0 + 8 + 64] = win_tex
        pairs.append(("custom_block_drag_8_8", f0_b, f1_b))

        return pairs

    def test_ten_pairs_constructed(self):
        self.assertEqual(len(self.pairs), 10)

    def test_joint_sad_cutoff_preserves_decisions_and_reduces_ops(self):
        max_d = 150
        saved_original_joint_sad = gm_bands_hv._joint_sad

        for case_id, prev_luma, cur_luma in self.pairs:
            with self.subTest(case_id=case_id):
                # 1. Normal evaluation (cutoff optimization active)
                normal_res = gm_bands_hv.estimate_global_motion(prev_luma, cur_luma, max_d)
                normal_ops = gm_bands_hv.OPS

                # 2. Full evaluation (force cutoff=None via saved original callable)
                received_cutoffs = []

                def patched_joint_sad(cur, prev, cutoff):
                    forced_cutoff = None
                    received_cutoffs.append(forced_cutoff)
                    return saved_original_joint_sad(cur, prev, cutoff=forced_cutoff)

                old_joint_sad = gm_bands_hv._joint_sad
                try:
                    gm_bands_hv._joint_sad = patched_joint_sad
                    full_res = gm_bands_hv.estimate_global_motion(prev_luma, cur_luma, max_d)
                    full_ops = gm_bands_hv.OPS
                finally:
                    gm_bands_hv._joint_sad = old_joint_sad

                # 3. Assertions
                # Assert that full evaluation actually receives cutoff=None
                self.assertTrue(
                    all(c is None for c in received_cutoffs),
                    f"{case_id}: Expected all cutoff arguments received during full evaluation to be None, got {received_cutoffs}"
                )

                # Require identical complete tuples or None
                self.assertEqual(
                    normal_res,
                    full_res,
                    f"{case_id}: Normal result {normal_res} != full result {full_res}"
                )

                # Require OPS with cutoff no greater than full evaluation OPS
                self.assertLessEqual(
                    normal_ops,
                    full_ops,
                    f"{case_id}: Normal OPS ({normal_ops}) > full OPS ({full_ops})"
                )


if __name__ == "__main__":
    unittest.main()
