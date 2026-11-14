"""tests/test_v2x_adv_gm.py - Unit tests for adversarial global motion scene generator and scorer."""

import unittest
import numpy as np
from eval.v2x.adv_gm import cases, score, compute_static_mask


class ToyOracleModule:
    """Toy oracle module that returns exact truth vector for test scenes."""

    @staticmethod
    def estimate_global_motion(prev_luma, cur_luma, max_d):
        # Determine displacement by matching against generated truth patterns
        H, W = cur_luma.shape
        diff = (cur_luma != prev_luma)
        if not diff.any():
            return (0, 0, 0)

        # Check if entire frame or moving band matches a shift (dx, dy)
        # Try expected dx, dy candidates from generator specs first
        candidates = [
            (4, 4), (8, 8), (20, 0), (0, -12), # flat_window_drag
            (0, -24),                          # chrome_clock
            (8, 0),                            # bare_caret
            (0, -600), (0, -830), (0, 600), (0, 816) # big_scroll_1080p
        ]

        # Check two_motions (two distinct object shifts) -> no global motion
        # Check flat_window_drag: moving window shifted by (dx, dy)
        for dx, dy in candidates:
            if abs(dx) > max_d or abs(dy) > max_d:
                continue
            cy0, cy1 = max(0, dy), min(H, H + dy)
            cx0, cx1 = max(0, dx), min(W, W + dx)
            if cy0 >= cy1 or cx0 >= cx1:
                continue
            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx

            c_sub = cur_luma[cy0:cy1, cx0:cx1]
            p_sub = prev_luma[py0:py1, px0:px1]

            # In flat_window_drag / bare_caret / chrome_clock, changed pixels in cur_luma inside overlap match prev_luma shifted
            chg = diff[cy0:cy1, cx0:cx1]
            if chg.any():
                if np.array_equal(c_sub[chg], p_sub[chg]):
                    # Distinguish single motion vs two_motions:
                    # If two_motions, two windows moved in different directions, so shifted check won't explain both or will leave unexplainable diffs
                    # Let's check total changed area explained
                    if (dx, dy) in [(4, 4), (8, 8), (20, 0), (0, -12)]:
                        # Check window size 64x64
                        if chg.sum() == 64 * 64:
                            return (dx, dy, 0)
                    elif (dx, dy) == (0, -24) and H >= 200:
                        # Header clock block changed, middle band scrolled by -24
                        return (0, -24, 0)
                    elif (dx, dy) == (8, 0):
                        # Caret shifted by 8
                        return (8, 0, 0)
                    elif dx == 0 and abs(dy) >= 600:
                        return (0, dy, 0)

        return None

    @staticmethod
    def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
        H, W = cur_luma.shape
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s

        cy0, cy1 = max(0, dy), min(H, H + dy)
        cx0, cx1 = max(0, dx), min(W, W + dx)

        diff_in_place = (cur_luma != prev_luma)

        diff = np.ones((H, W), dtype=bool)
        if cy0 < cy1 and cx0 < cx1:
            py0, py1 = cy0 - dy, cy1 - dy
            px0, px1 = cx0 - dx, cx1 - dx
            diff[cy0:cy1, cx0:cx1] = (cur_luma[cy0:cy1, cx0:cx1] != prev_luma[py0:py1, px0:px1])

        pad_h = gh * s - H
        pad_w = gw * s - W
        if pad_h > 0 or pad_w > 0:
            diff_p = np.pad(diff, ((0, pad_h), (0, pad_w)), constant_values=False)
            chg_p = np.pad(diff_in_place, ((0, pad_h), (0, pad_w)), constant_values=False)
        else:
            diff_p = diff
            chg_p = diff_in_place

        has_mismatch = diff_p.reshape(gh, s, gw, s).any(axis=(1, 3))
        has_change = chg_p.reshape(gh, s, gw, s).any(axis=(1, 3))
        return ~has_mismatch & has_change


class ToyConstantModule:
    """Toy module that always returns vector (1,0)."""

    @staticmethod
    def estimate_global_motion(prev_luma, cur_luma, max_d):
        return (1, 0, 100)

    @staticmethod
    def consensus_tiles(prev_luma, cur_luma, s, dx, dy):
        H, W = cur_luma.shape
        gh = (H + s - 1) // s
        gw = (W + s - 1) // s
        return np.ones((gh, gw), dtype=bool)


class ToyRaisingModule:
    """Toy module that raises an exception."""

    @staticmethod
    def estimate_global_motion(prev_luma, cur_luma, max_d):
        raise RuntimeError("Module error during estimation")


class TestV2xAdvGm(unittest.TestCase):

    def test_determinism(self):
        c1 = cases(seed=0x5EED, include_1080p=False)
        c2 = cases(seed=0x5EED, include_1080p=False)
        self.assertEqual(len(c1), len(c2))
        for item1, item2 in zip(c1, c2):
            self.assertEqual(item1["case_id"], item2["case_id"])
            self.assertEqual(item1["kind"], item2["kind"])
            self.assertEqual(item1["truth_vectors"], item2["truth_vectors"])
            for f1, f2 in zip(item1["frames"], item2["frames"]):
                np.testing.assert_array_equal(f1, f2)
            for m1, m2 in zip(item1["static_masks"], item2["static_masks"]):
                np.testing.assert_array_equal(m1, m2)

    def test_frame_shapes_and_dtypes(self):
        c_list = cases(seed=0x5EED, include_1080p=False)
        for c in c_list:
            h, w, s = c["h"], c["w"], c["s"]
            gh = (h + s - 1) // s
            gw = (w + s - 1) // s
            for f in c["frames"]:
                self.assertEqual(f.shape, (h, w))
                self.assertEqual(f.dtype, np.uint8)
            for m in c["static_masks"]:
                self.assertEqual(m.shape, (gh, gw))
                self.assertEqual(m.dtype, bool)

    def test_truth_vectors_independent_pixel_check(self):
        c_list = cases(seed=0x5EED, include_1080p=True)
        for c in c_list:
            kind = c["kind"]
            frames = c["frames"]
            truth_vectors = c["truth_vectors"]
            f0, f1 = frames[0], frames[1]
            truth = truth_vectors[0]

            if kind == "flat_window_drag":
                dx, dy = truth
                diff = (f1 != f0)
                self.assertTrue(diff.any())
            elif kind == "two_motions":
                self.assertIsNone(truth)
            elif kind == "big_scroll_1080p":
                dx, dy = truth
                self.assertEqual(dx, 0)
                if dy > 0:
                    np.testing.assert_array_equal(f1[dy:], f0[:-dy])
                else:
                    np.testing.assert_array_equal(f1[: c["h"] + dy], f0[-dy:])
            elif kind == "chrome_clock":
                dx, dy = truth
                self.assertEqual((dx, dy), (0, -24))
                top_static = np.array_equal(f0[:10], f1[:10])
                self.assertTrue(top_static)
            elif kind == "periodic_text":
                self.assertIsNone(truth)
            elif kind == "bare_caret":
                dx, dy = truth
                self.assertEqual((dx, dy), (8, 0))

    def test_score_oracle(self):
        res = score(ToyOracleModule, seed=0x5EED, include_1080p=False)
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["total"]["wrong_vectors"], 0)
        self.assertEqual(res["total"]["static_relabels"], 0)

    def test_score_constant_wrong_vectors(self):
        res = score(ToyConstantModule, seed=0x5EED, include_1080p=False)
        self.assertEqual(res["status"], "OK")
        tot_pairs = res["total"]["pairs"]
        self.assertEqual(res["total"]["wrong_vectors"], tot_pairs)

    def test_score_raising_module(self):
        res = score(ToyRaisingModule, seed=0x5EED, include_1080p=False)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Module error during estimation", res["error"])


if __name__ == "__main__":
    unittest.main()
