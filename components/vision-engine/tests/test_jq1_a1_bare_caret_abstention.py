"""tests/test_jq1_a1_bare_caret_abstention.py - Invariant test for bare-caret global motion abstention.

Pins the CARD_GM_V2 bare-caret abstention rule despite the scorer truth mismatch in adv_gm.
In adv_gm, bare_caret cases record the local caret translation vector (8, 0) as their truth
vector. Consequently, adv_gm.score() reports abstention (returning None or (0,0)) as a truth
mismatch. However, the CARD_GM_V2 domain invariant dictates that a localized caret moving over a
uniform luma background must NOT be detected as global scene motion. estimate_global_motion
must abstain by returning None or a zero vector (0, 0, score) with nonnegative Python int score.

This test guards against future relaxation of the narrow-overlap or alias gates in global motion
estimation that could mistakenly turn local caret movement into global motion (8, 0).
"""

import unittest
import numpy as np
from eval.v2x import adv_gm
from eval.v2x.gm_bands_hv import estimate_global_motion


class TestBareCaretAbstention(unittest.TestCase):
    """Test suite asserting bare-caret abstention under CARD_GM_V2."""

    def test_bare_caret_cases_abstain_at_max_d_64(self):
        """Verify that all bare_caret adversarial cases return None or (0, 0, score), never (8, 0)."""
        all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)
        bare_caret_cases = [c for c in all_cases if c.get("kind") == "bare_caret"]

        # Ensure we selected the expected set of bare_caret cases
        self.assertGreaterEqual(len(bare_caret_cases), 3, "Expected at least 3 bare_caret cases from adv_gm")

        for case in bare_caret_cases:
            case_id = case["case_id"]
            frames = case["frames"]
            self.assertEqual(len(frames), 2, f"Case {case_id} expected to contain 2 frames")

            f0, f1 = frames[0], frames[1]

            # Evaluate global motion estimation at max_d = 64
            res = estimate_global_motion(f0, f1, max_d=64)

            # Do not use scorer exact counts to decide this card invariant because adv_gm
            # records the caret translation (8, 0) as its truth vector.
            # estimate_global_motion MUST return None or a zero vector (0, 0, score).
            if res is not None:
                self.assertIsInstance(res, tuple, f"Case {case_id}: result must be a tuple or None")
                self.assertEqual(len(res), 3, f"Case {case_id}: result tuple must have 3 elements (dx, dy, score)")

                dx, dy, score = res
                self.assertIsInstance(dx, int, f"Case {case_id}: dx must be a Python int")
                self.assertIsInstance(dy, int, f"Case {case_id}: dy must be a Python int")
                self.assertIsInstance(score, int, f"Case {case_id}: score must be a Python int")

                # Must return a zero vector (0, 0), never the local caret translation vector (8, 0)
                self.assertEqual((dx, dy), (0, 0), f"Case {case_id}: estimated global motion must be zero vector (0, 0), got ({dx}, {dy})")
                self.assertGreaterEqual(score, 0, f"Case {case_id}: score must be nonnegative int, got {score}")
                self.assertNotEqual((dx, dy), (8, 0), f"Case {case_id}: estimate_global_motion must never return (8, 0)")
            else:
                # Returning None is a valid abstention response
                self.assertIsNone(res, f"Case {case_id}: returned None as valid abstention")


if __name__ == "__main__":
    unittest.main()
