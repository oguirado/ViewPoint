"""W10D lane D: MOVED-miss cause classifier on synthetic sequences driven through the sealed engine."""
import unittest

import numpy as np

from blender_visual_qa.truth import diag_moved as dm
from blender_visual_qa.truth import labels

W = H = 128
BG = 40


def texture(kind, w, h):
    yy, xx = np.mgrid[0:h, 0:w]
    if kind == "flat":
        return np.full((h, w), 200, np.uint8)
    if kind == "checker12":  # 6x6 px cells, 12 px period (VQA_SMOKE TR/ML/BR)
        return np.where(((xx // 6) + (yy // 6)) % 2 == 0, 243, 63).astype(np.uint8)
    return np.random.default_rng(7).integers(0, 256, (h, w), dtype=np.uint8)


def sequence(kind, dx, n=4, x0=8, y0=24, sw=80, sh=80):
    """Textured square (attached texture) moving dx px/frame over a flat background, with Blender-style truth."""
    tex = texture(kind, sw, sh)
    rgbs, truths, boxes = [], [], []
    for f in range(n):
        x = x0 + dx * f
        lum = np.full((H, W), BG, np.uint8)
        ob = np.zeros((H, W), np.uint16)
        mv = np.zeros((H, W, 2), np.int16)
        z = np.full((H, W), labels.BG_Z, np.uint32)
        xa, xb = max(0, x), min(W, x + sw)
        lum[y0:y0 + sh, xa:xb] = tex[:, xa - x:xb - x]
        ob[y0:y0 + sh, xa:xb] = 1
        mv[y0:y0 + sh, xa:xb, 0] = 16 * dx
        z[y0:y0 + sh, xa:xb] = 1000
        rgbs.append(np.repeat(lum[..., None], 3, axis=2))
        truths.append({"ob": ob, "mv": mv, "z": z})
        boxes.append((x, y0, x + sw, y0 + sh))
    return rgbs, truths, boxes


def interior(t, boxes, s=16, margin=2):
    """Tile block (+margin) inside the square at t and its source block (shifted by -mv) inside it at t-1."""
    f, dx = t["frame"], t["mv"][0]
    x0, y0, x1, y1 = boxes[f]
    px0, _, px1, _ = boxes[f - 1]
    bx0, by0 = t["tx"] * s - margin, t["ty"] * s - margin
    bx1, by1 = (t["tx"] + 1) * s + margin, (t["ty"] + 1) * s + margin
    return x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1 and px0 <= bx0 - dx and bx1 - dx <= px1


def diagnose(kind, dx):
    rgbs, truths, boxes = sequence(kind, dx)
    g, cls, mv = dm.engine_run(rgbs)
    tiles = dm.diagnose_sequence(rgbs, truths, cls, mv, g)
    return tiles, [t for t in tiles if interior(t, boxes)]


class TestMovedDiag(unittest.TestCase):
    def test_flat_square_interior_is_aperture(self):
        tiles, inner = diagnose("flat", 3)
        self.assertGreaterEqual(len(inner), 6)
        self.assertEqual({t["cause"] for t in inner}, {"a"})
        self.assertFalse(any(t["hit"] for t in inner))  # any engine must say UNCHANGED
        m = dm.observable_mask(tiles, 64)
        self.assertFalse(any(m[t["tile"]] for t in inner))

    def test_checker12_dx3_is_ambiguity(self):
        _, inner = diagnose("checker12", 3)
        self.assertGreaterEqual(len(inner), 6)
        self.assertEqual({t["cause"] for t in inner}, {"b"})
        self.assertTrue(all(t["near_comp"] > 1 for t in inner))

    def test_noise_square_observable_and_recalled(self):
        _, inner = diagnose("noise", 3)
        self.assertGreaterEqual(len(inner), 6)
        causes = {t["cause"] for t in inner}
        self.assertFalse(causes & {"a", "b"})
        self.assertEqual(causes, {"e"})
        s = dm.summarize(inner)
        self.assertEqual(s["observable_n"], len(inner))
        # recall on the interior >= 0.95, or every miss is reported as a genuine engine miss (e)
        if s["recall_obs_ppm"] < 950000:
            self.assertEqual(s["causes"]["e"]["miss"], s["miss"])
        self.assertGreaterEqual(s["recall_obs_ppm"], 950000)

    def test_out_of_search_range_is_d(self):
        _, inner = diagnose("noise", 20)  # s = 16: |mv| > d
        self.assertTrue(inner)
        self.assertEqual({t["cause"] for t in inner}, {"d"})

    def test_summary_ppm_and_examples(self):
        tiles, _ = diagnose("flat", 3)
        s = dm.summarize(tiles)
        self.assertEqual(sum(c["n"] for c in s["causes"].values()), s["rigid_n"])
        self.assertLessEqual(len(s["causes"]["a"]["examples"]), dm.EXAMPLES)
        self.assertEqual(s["observable_n"], s["rigid_n"] - s["causes"]["a"]["n"] - s["causes"]["b"]["n"])

    def test_gate_seeds_refused(self):
        self.assertEqual(dm._cal_seeds("0-2"), [0, 1, 2])
        with self.assertRaises(dm.DiagError) as cm:
            dm._cal_seeds("9-10")
        self.assertEqual(cm.exception.rc, 5)
        self.assertEqual(dm.main(["--out", "/nonexistent/x.json", "--seeds", "10-12"]), 5)


if __name__ == "__main__":
    unittest.main()
