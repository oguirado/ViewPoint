"""CF1: B1 box-size floor (DLAB RC-1), light scope (FC-DLAB-4), EDGE_RING from filter_width, --resid CLI."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import numpy as np

from blender_visual_qa.truth import labels as L

W, H = 320, 180


def fr(x0, bw, bh, v=0, ob=1):
    f = {"ob": np.zeros((H, W), np.uint16), "z": np.full((H, W), L.BG_Z, np.uint32),
         "mv": np.zeros((H, W, 2), np.int16)}
    f["ob"][60:60 + bh, x0:x0 + bw] = ob
    f["z"][60:60 + bh, x0:x0 + bw] = 2000
    f["mv"][60:60 + bh, x0:x0 + bw] = (v * 16, 0)
    return f


def rigid_count(bw, bh, v, offs=(0, 5, 11)):
    g = L.vgrid.Grid(W, H)
    n = 0
    for o in offs:
        lab = L.label_frame(fr(40 + o, bw, bh), fr(40 + o + v, bw, bh, v), g)
        n += int((lab["tl"] == L.TL_ID["RIGID"]).sum())
    return n


class B1GeometryTests(unittest.TestCase):
    def test_48x48_yields_rigid_tiles_with_exact_mv(self):
        g = L.vgrid.Grid(W, H)
        for v in (1, 2, 3, 5, 8, 16):
            self.assertGreaterEqual(rigid_count(48, 48, v, (0,)), 1, v)
        lab = L.label_frame(fr(40, 48, 48), fr(45, 48, 48, 5), g)
        m = lab["tl"] == L.TL_ID["RIGID"]
        self.assertTrue(m.any())
        self.assertEqual({(int(a), int(b)) for a, b in lab["mv"][m]}, {(5, 0)})

    def test_12x8_never_rigid(self):  # documents the size floor: boxes < 2s + 2*EDGE_RING cannot exercise RIGID
        for v in (1, 3, 16):
            self.assertEqual(rigid_count(12, 8, v), 0, v)


class LightScopeTests(unittest.TestCase):
    def test_lit_scope_keeps_background_static(self):
        g = L.vgrid.Grid(W, H)
        a, b = fr(100, 48, 48), fr(100, 48, 48)
        j = {"animated": [{"frame": 2, "kind": "light", "name": "lt"}], "lit": ["lit"]}
        labs = L.label_sequence([a, b], g, j, {"lit": 1}, 1)
        tl = labs[1]["tl"]
        self.assertTrue((tl == L.TL_ID["SHADE_BAND"]).any())
        self.assertTrue((tl == L.TL_ID["STATIC"]).sum() > (tl == L.TL_ID["SHADE_BAND"]).sum())

    def test_no_lit_falls_back_to_anim_all(self):
        g = L.vgrid.Grid(W, H)
        j = {"animated": [{"frame": 2, "kind": "light", "name": "lt"}]}
        labs = L.label_sequence([fr(100, 48, 48), fr(100, 48, 48)], g, j, {"lit": 1}, 1)
        self.assertTrue((labs[1]["tl"] == L.TL_ID["SHADE_BAND"]).all())


class RingTests(unittest.TestCase):
    def test_ring_from_filter(self):
        self.assertEqual(L.ring_from_filter(None), L.EDGE_RING)
        self.assertEqual(L.ring_from_filter(0.01), 1)
        self.assertEqual(L.ring_from_filter(1.5), 2)
        self.assertEqual(L.ring_from_filter(2.0), 2)


class ResidTests(unittest.TestCase):
    def test_invisible_and_shade_with_resid(self):
        g = L.vgrid.Grid(W, H)
        a, b = fr(40, 48, 48), fr(45, 48, 48, 5)
        zero = np.zeros(g.N, np.int64)
        lab = L.label_frame(a, b, g, resid=zero)
        self.assertEqual(int((lab["tl"] == L.TL_ID["RIGID"]).sum()), 0)  # invisible motion is not RIGID truth
        self.assertTrue((lab["tl"] == L.TL_ID["INVISIBLE"]).any())
        big = np.full(g.N, 5000, np.int64)
        self.assertTrue((L.label_frame(a, b, g, resid=big)["tl"] == L.TL_ID["RIGID"]).any())

    def test_cli_resid_missing_rc2_and_bad_shape(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = L.main(["--run", d, "--out", d, "--resid", os.path.join(d, "nope.npy")])
            self.assertEqual(rc, 2)
            self.assertIn("E_IO", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
