"""tests/test_jq1_a4_oracle_digest_and_card_vectors.py - Claim-pinning and digest tests for oracle.expected."""

import hashlib
import json
import unittest
import numpy as np

from eval.v2x import gm_v2_oracle as oracle
from eval.v2x import gm_bands_hv as mod


class TestA4OracleDigestAndCardVectors(unittest.TestCase):

    def test_card_cases_claim_pinning(self):
        """Pin card cases outputs from conformance_cases(seed=0x5EED) against derived specs."""
        cases = {c["case_id"]: c for c in oracle.conformance_cases(seed=0x5EED)}

        # 1. identical_frames: (1, 0, 0, 0, 30, 40) and all-zero mask
        tup_ident, mask_ident = oracle.expected(mod, cases["identical_frames"])
        self.assertEqual(tup_ident, (1, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_ident, np.zeros((30, 40), dtype=np.uint8))

        # 2. large_dy_1088_1920: (1, 0, 300, 0, 68, 120) and mask rows 0..18 False, rows 19..67 True
        tup_ldy, mask_ldy = oracle.expected(mod, cases["large_dy_1088_1920"])
        self.assertEqual(tup_ldy, (1, 0, 300, 0, 68, 120))
        expected_mask_ldy = np.zeros((68, 120), dtype=np.uint8)
        expected_mask_ldy[19:68, :] = 1
        np.testing.assert_array_equal(mask_ldy, expected_mask_ldy)

        # 3. two_motions: (0, 0, 0, 0, 30, 40) and all-zero mask
        tup_two, mask_two = oracle.expected(mod, cases["two_motions"])
        self.assertEqual(tup_two, (0, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_two, np.zeros((30, 40), dtype=np.uint8))

        # 4. static_chrome_clock: (0, 0, 0, 0, 30, 40) and all-zero mask
        tup_clk, mask_clk = oracle.expected(mod, cases["static_chrome_clock"])
        self.assertEqual(tup_clk, (0, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_clk, np.zeros((30, 40), dtype=np.uint8))

        # 5. bare_caret: (0, 0, 0, 0, 30, 40) and all-zero mask
        tup_caret, mask_caret = oracle.expected(mod, cases["bare_caret"])
        self.assertEqual(tup_caret, (0, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_caret, np.zeros((30, 40), dtype=np.uint8))

        # 6. flat_window_drag: displacement 16, 24, but estimator returns None -> (0, 0, 0, 0, 30, 40) and all-zero mask
        tup_drag, mask_drag = oracle.expected(mod, cases["flat_window_drag"])
        self.assertEqual(tup_drag, (0, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_drag, np.zeros((30, 40), dtype=np.uint8))

        # 7. periodic_text: period-8 roll leaves bytes unchanged -> (1, 0, 0, 0, 30, 40) and all-zero mask
        tup_text, mask_text = oracle.expected(mod, cases["periodic_text"])
        self.assertEqual(tup_text, (1, 0, 0, 0, 30, 40))
        np.testing.assert_array_equal(mask_text, np.zeros((30, 40), dtype=np.uint8))

    def test_custom_uint8_fixtures(self):
        """Test expected outputs on custom 120x160 shifted fixtures with default_rng(403)."""
        h, w, s, max_d = 120, 160, 16, 32

        # Fixture 1: cur columns 0..155 equal prev columns 4..159 with fresh right columns
        rng1 = np.random.default_rng(403)
        prev1 = rng1.integers(0, 256, (h, w), dtype=np.uint8)
        cur1 = np.empty_like(prev1)
        cur1[:, :156] = prev1[:, 4:160]
        cur1[:, 156:] = rng1.integers(0, 256, (h, 4), dtype=np.uint8)

        case1 = {"h": h, "w": w, "s": s, "max_d": max_d, "prev": prev1, "cur": cur1}
        tup1, mask1 = oracle.expected(mod, case1)
        self.assertEqual(tup1, (1, -4, 0, 0, 8, 10))

        # Independently computed changed-tile mask for fixture 1:
        # Columns 0..155 (tiles 0..8) are in-bounds overlap and matched. Tile 9 covers cols 144..159,
        # where cols 156..159 are fresh/out-of-bounds for dx=-4, so tile 9 has mismatches.
        expected_mask1 = np.zeros((8, 10), dtype=np.uint8)
        expected_mask1[:, :9] = 1
        np.testing.assert_array_equal(mask1, expected_mask1)

        # Fixture 2: cur rows 0..115 equal prev rows 4..119 with fresh bottom rows
        rng2 = np.random.default_rng(403)
        prev2 = rng2.integers(0, 256, (h, w), dtype=np.uint8)
        cur2 = np.empty_like(prev2)
        cur2[:116, :] = prev2[4:120, :]
        cur2[116:, :] = rng2.integers(0, 256, (4, w), dtype=np.uint8)

        case2 = {"h": h, "w": w, "s": s, "max_d": max_d, "prev": prev2, "cur": cur2}
        tup2, mask2 = oracle.expected(mod, case2)
        self.assertEqual(tup2, (1, 0, -4, 0, 8, 10))

        # Independently computed changed-tile mask for fixture 2:
        # Rows 0..115 (tiles 0..6) are in-bounds overlap and matched. Tile row 7 covers rows 112..119,
        # where rows 116..119 are fresh/out-of-bounds for dy=-4, so tile row 7 has mismatches.
        expected_mask2 = np.zeros((8, 10), dtype=np.uint8)
        expected_mask2[:7, :] = 1
        np.testing.assert_array_equal(mask2, expected_mask2)

    def test_independent_digest_and_mutants(self):
        """Independently serialize 71 digest records and verify SHA-256 against oracle.digest and mutant stubs."""
        # Compute digest independently
        cases = oracle.conformance_cases(seed=0x5EED)
        self.assertEqual(len(cases), 71)

        records = []
        for case in cases:
            ref_tuple, ref_mask = oracle.expected(mod, case)
            has_gm, dx, dy, score, gh, gw = ref_tuple
            records.append({
                "case_id": str(case["case_id"]),
                "has_gm": int(has_gm),
                "dx": int(dx),
                "dy": int(dy),
                "score": int(score),
                "gh": int(gh),
                "gw": int(gw),
                "mask": np.ascontiguousarray(ref_mask, dtype=np.uint8).tolist(),
            })

        canonical_json = json.dumps(records, sort_keys=True, separators=(",", ":"))
        indep_digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        oracle_d = oracle.digest(mod)
        self.assertEqual(oracle_d, indep_digest)

        # Score mutant stub: changes score by 1 only for identical_frames
        class ScoreMutantStub:
            @staticmethod
            def estimate_global_motion(prev, cur, max_d):
                res = mod.estimate_global_motion(prev, cur, max_d)
                if res is not None and prev.shape == (480, 640) and np.array_equal(prev, cur):
                    return (res[0], res[1], res[2] + 1)
                return res

            @staticmethod
            def consensus_tiles(prev, cur, s, dx, dy):
                return mod.consensus_tiles(prev, cur, s, dx, dy)

        score_mutant_d = oracle.digest(ScoreMutantStub)
        self.assertNotEqual(score_mutant_d, oracle_d)

        # Mask mutant stub: flips a mask bit only for identical_frames
        class MaskMutantStub:
            @staticmethod
            def estimate_global_motion(prev, cur, max_d):
                return mod.estimate_global_motion(prev, cur, max_d)

            @staticmethod
            def consensus_tiles(prev, cur, s, dx, dy):
                mask = mod.consensus_tiles(prev, cur, s, dx, dy)
                if prev.shape == (480, 640) and np.array_equal(prev, cur):
                    mask = mask.copy()
                    mask[0, 0] = 1 if mask[0, 0] == 0 else 0
                return mask

        mask_mutant_d = oracle.digest(MaskMutantStub)
        self.assertNotEqual(mask_mutant_d, oracle_d)


if __name__ == "__main__":
    unittest.main()
