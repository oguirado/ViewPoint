"""Pin D-A1-F4-CLOCK on each chrome_clock case from adv_gm.cases."""
import unittest
import numpy as np

import eval.v2x.adv_gm as adv_gm
import eval.v2x.gm_bands_hv as gm


def _get_chrome_clock_cases():
    all_cases = adv_gm.cases(seed=0x5EED, include_1080p=False)
    return [c for c in all_cases if c.get("kind") == "chrome_clock"]


def _make_test_case_class():
    cc_cases = _get_chrome_clock_cases()

    class TestChromeClockScroll(unittest.TestCase):
        pass

    for c in cc_cases:
        cid = c["case_id"]

        def make_estimate_test(case_dict):
            def test_method(self):
                """D-A1-F4-CLOCK: Header clock changing in place causes gm_bands_hv to fail on scrolling body."""
                f0, f1 = case_dict["frames"]
                h, w = case_dict["h"], case_dict["w"]
                max_d = max(w, h) - 1
                res = gm.estimate_global_motion(f0, f1, max_d)
                self.assertIsNotNone(res, f"Case {case_dict['case_id']}: estimate_global_motion returned None")
                dx, dy, score = res
                self.assertEqual((dx, dy), (0, -24), f"Case {case_dict['case_id']}: expected (0, -24), got ({dx}, {dy})")
                self.assertIsInstance(score, int, f"Case {case_dict['case_id']}: score is not a Python int")

            return test_method

        def make_consensus_test(case_dict):
            def test_method(self):
                """Safety assertion: consensus_tiles must match changed-in-place pixel rule and no byte-identical tile relabelled."""
                f0, f1 = case_dict["frames"]
                h, w = case_dict["h"], case_dict["w"]
                s = case_dict.get("s", 16)
                truth_dx, truth_dy = 0, -24

                c_mask = gm.consensus_tiles(f0, f1, s, truth_dx, truth_dy)
                self.assertIsInstance(c_mask, np.ndarray)

                # Compute expected consensus tiles independently based on changed-in-place & valid source match
                gh = (h + s - 1) // s
                gw = (w + s - 1) // s
                pad_h = gh * s - h
                pad_w = gw * s - w

                # Changed in place tile check
                changed = (f0 != f1)
                if pad_h > 0 or pad_w > 0:
                    changed = np.pad(changed, ((0, pad_h), (0, pad_w)), constant_values=False)
                changed_tiles = changed.reshape(gh, s, gw, s).any(axis=(1, 3))

                # Valid source translated pixel equality check
                diff = np.ones((h, w), dtype=bool)
                cy0, cy1 = max(0, truth_dy), min(h, h + truth_dy)
                cx0, cx1 = max(0, truth_dx), min(w, w + truth_dx)
                if cy0 < cy1 and cx0 < cx1:
                    py0, py1 = cy0 - truth_dy, cy1 - truth_dy
                    px0, px1 = cx0 - truth_dx, cx1 - truth_dx
                    diff[cy0:cy1, cx0:cx1] = (f1[cy0:cy1, cx0:cx1] != f0[py0:py1, px0:px1])

                if pad_h > 0 or pad_w > 0:
                    diff_padded = np.pad(diff, ((0, pad_h), (0, pad_w)), constant_values=False)
                else:
                    diff_padded = diff
                has_mismatch = diff_padded.reshape(gh, s, gw, s).swapaxes(1, 2).any(axis=(2, 3))

                expected_consensus = ~has_mismatch & changed_tiles
                self.assertTrue(
                    np.array_equal(c_mask, expected_consensus),
                    f"Case {case_dict['case_id']}: consensus_tiles did not match expected valid-source pixel rule"
                )

                # No byte-identical tile may be relabelled
                static_mask = case_dict["static_masks"][0]
                relabels = int(np.logical_and(c_mask, static_mask).sum())
                self.assertEqual(
                    relabels, 0,
                    f"Case {case_dict['case_id']}: {relabels} byte-identical tiles were relabelled"
                )

            return test_method

        setattr(TestChromeClockScroll, f"test_estimate_global_motion_{cid}", make_estimate_test(c))
        setattr(TestChromeClockScroll, f"test_consensus_tiles_safety_{cid}", make_consensus_test(c))

    return TestChromeClockScroll


TestChromeClockScroll = _make_test_case_class()

if __name__ == "__main__":
    unittest.main()
