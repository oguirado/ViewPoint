import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blender_visual_qa.truth import labels as L  # noqa: E402

W, H = 96, 64   # grid s16, 6x4 = 24 tiles


def frame(box=None, ob=1, z_mm=2000, mv=(0, 0), extra=None):
    """Synthetic truth frame: bg ob 0 (z=BG), a box (x0,y0,x1,y1) with ob id, uniform mv in px."""
    f = {"ob": np.zeros((H, W), np.uint16), "z": np.full((H, W), L.BG_Z, np.uint32),
         "mv": np.zeros((H, W, 2), np.int16)}
    if box:
        x0, y0, x1, y1 = box
        f["ob"][y0:y1, x0:x1] = ob
        f["z"][y0:y1, x0:x1] = z_mm
        f["mv"][y0:y1, x0:x1] = (mv[0] * 16, mv[1] * 16)
    return f


def tile(g, lab, tx, ty):
    i = int(np.nonzero((g.tx == tx) & (g.ty == ty))[0][0])
    return {k: lab[k][i] for k in lab}


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.g = L.vgrid.Grid(W, H)

    def test_static_all(self):
        a = frame((20, 20, 60, 50))
        lab = L.label_frame(a, frame((20, 20, 60, 50)), self.g)
        self.assertTrue((lab["tl"] == L.TL_ID["STATIC"]).all())

    def test_rigid_sign_and_value(self):
        # box moves +3 px x, -2 px y; mv t-1->t in image coords = (+3,-2)
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(3, -2))
        lab = L.label_frame(a, b, self.g)
        t = tile(self.g, lab, 2, 1)   # interior tile (32..48, 16..32)
        self.assertEqual(t["tl"], L.TL_ID["RIGID"])
        self.assertEqual((int(t["mv"][0]), int(t["mv"][1])), (3, -2))
        self.assertEqual(t["ob_major"], 1)

    def test_mv_sign_negated_is_not_rigid_truth(self):
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(-3, 2))   # wrong sign: source falls outside the box
        t = tile(self.g, L.label_frame(a, b, self.g), 2, 1)
        self.assertNotEqual((int(t["mv"][0]), int(t["mv"][1])), (3, -2))

    def test_occlusion_disoccluded_strip(self):
        a = frame((0, 0, 40, 64), ob=1)
        b = frame((0, 0, 40, 64), ob=1)
        b["ob"][:, 40:56] = 2         # new object appears, mv 0
        b["z"][:, 40:56] = 1500
        t = tile(self.g, L.label_frame(a, b, self.g), 2, 1)   # x 32..48 half new
        self.assertEqual(t["tl"], L.TL_ID["OCCLUDE"])
        self.assertGreater(int(t["occ_permille"]), 10)
        t2 = tile(self.g, L.label_frame(a, b, self.g), 5, 3)
        self.assertEqual(t2["tl"], L.TL_ID["STATIC"])

    def test_subpix_edge_only(self):
        a = frame((16, 16, 64, 48))
        b = frame((16, 16, 64, 48))
        b["mv"][..., 0] = 4   # 0.25 px everywhere: below MOV threshold -> no rigid
        lab = L.label_frame(a, b, self.g)
        self.assertTrue((lab["tl"] == L.TL_ID["STATIC"]).all())
        # one-pixel boundary shift: edge-only change -> SUBPIX on the boundary tile
        c = frame((17, 16, 64, 48))
        lab = L.label_frame(a, c, self.g)
        t = tile(self.g, lab, 1, 1)
        self.assertEqual(t["tl"], L.TL_ID["SUBPIX"])
        self.assertTrue(int(t["flags"]) & L.FLAG_EDGE_ONLY)

    def test_deform_non_uniform(self):
        a = frame((16, 16, 64, 48))
        b = frame((16, 16, 64, 48))
        b["mv"][16:48, 16:64, 0] = np.tile(np.arange(48) * 16, (32, 1)).astype(np.int16) % 80
        t = tile(self.g, L.label_frame(a, b, self.g), 2, 1)
        self.assertIn(t["tl"], (L.TL_ID["DEFORM"], L.TL_ID["OCCLUDE"]))

    def test_anim_shade_with_and_without_resid(self):
        a = frame((16, 16, 64, 48))
        lab = L.label_frame(a, a, self.g, anim_ob={1})
        self.assertEqual(tile(self.g, lab, 2, 1)["tl"], L.TL_ID["SHADE_BAND"])   # no resid -> excluded
        res = np.full(self.g.N, 300, np.int64)
        lab = L.label_frame(a, a, self.g, anim_ob={1}, resid=res, t_enter=64, t_exit=48)
        self.assertEqual(tile(self.g, lab, 2, 1)["tl"], L.TL_ID["SHADE_STRONG"])
        lab = L.label_frame(a, a, self.g, anim_ob={1}, resid=np.full(self.g.N, 4, np.int64))
        self.assertEqual(tile(self.g, lab, 2, 1)["tl"], L.TL_ID["SHADE_WEAK"])
        self.assertEqual(tile(self.g, lab, 5, 3)["tl"], L.TL_ID["STATIC"])

    def test_invisible_with_clean_resid(self):
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(3, -2))
        lab = L.label_frame(a, b, self.g, resid=np.zeros(self.g.N, np.int64))
        self.assertEqual(tile(self.g, lab, 2, 1)["tl"], L.TL_ID["INVISIBLE"])

    def test_temporal_override(self):
        a = frame((16, 16, 64, 48))
        j = {"temporal": [{"name": "box", "kind": "flicker", "frames": [1, 3]}]}
        labs = L.label_sequence([a, a, a], self.g, j, {"box": 1}, 0)
        self.assertEqual(tile(self.g, labs[1], 2, 1)["tl"], L.TL_ID["FLICKER_T"])
        self.assertEqual(tile(self.g, labs[1], 5, 3)["tl"], L.TL_ID["STATIC"])
        self.assertEqual(tile(self.g, labs[0], 2, 1)["tl"], L.TL_ID["STATIC"])

    def test_morton_order(self):
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(3, -2))
        lab = L.label_frame(a, b, self.g)
        i = int(np.nonzero((self.g.tx == 2) & (self.g.ty == 1))[0][0])
        self.assertEqual(int(self.g.mortons[i]), L.vlayout.morton(2, 1))
        self.assertEqual(lab["tl"][i], L.TL_ID["RIGID"])

    def test_determinism(self):
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(3, -2))
        p1 = L.pack_truth_tiles(self.g, L.label_sequence([a, b], self.g, None, None, 0))
        p2 = L.pack_truth_tiles(self.g, L.label_sequence([a, b], self.g, None, None, 0))
        self.assertEqual(p1, p2)


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.g = L.vgrid.Grid(W, H)
        a = frame((16, 16, 64, 48))
        b = frame((19, 14, 67, 46), mv=(3, -2))
        self.labs = L.label_sequence([a, b], self.g, None, None, 5)

    def test_roundtrip(self):
        blob = L.pack_truth_tiles(self.g, self.labs, 5, b"\x11" * 16)
        self.assertEqual(blob[:8], b"BVQTRUT1")
        self.assertEqual(len(blob), 64 + 2 * self.g.N * 8)
        h, arrs = L.parse_truth_tiles(blob, self.g)
        self.assertEqual((h["n_frames"], h["f0"], h["gw"], h["gh"]), (2, 5, 6, 4))
        self.assertTrue((arrs[1]["tl"] == self.labs[1]["tl"]).all())
        self.assertTrue((arrs[1]["dx"] == self.labs[1]["mv"][:, 0]).all())

    def test_grid_mismatch_rc4(self):
        blob = L.pack_truth_tiles(self.g, self.labs)
        with self.assertRaises(L.VqaError) as c:
            L.parse_truth_tiles(blob, L.vgrid.Grid(128, 64))
        self.assertEqual((c.exception.code, c.exception.rc), ("E_STALE", 4))

    def test_label_out_of_range_rc2(self):
        blob = bytearray(L.pack_truth_tiles(self.g, self.labs))
        blob[64] = 99
        with self.assertRaises(L.VqaError) as c:
            L.parse_truth_tiles(bytes(blob), self.g)
        self.assertEqual((c.exception.code, c.exception.rc), ("E_SCHEMA", 2))

    def test_bad_magic_and_size(self):
        blob = L.pack_truth_tiles(self.g, self.labs)
        with self.assertRaises(L.VqaError):
            L.parse_truth_tiles(b"XXXXXXXX" + blob[8:])
        with self.assertRaises(L.VqaError):
            L.parse_truth_tiles(blob[:-1])

    def test_schema_checks(self):
        f = frame((1, 1, 5, 5))
        L.check_frame(f)
        f["ob"] = f["ob"].astype(np.int32)
        with self.assertRaises(L.VqaError) as c:
            L.check_frame(f)
        self.assertEqual(c.exception.rc, 2)

    def test_quant_check(self):
        ob = np.array([[0, 1, 3]], np.uint16)
        L.check_ob_in_manifest(ob, [1, 3])
        with self.assertRaises(L.VqaError) as c:
            L.check_ob_in_manifest(ob, [1, 2])
        self.assertEqual((c.exception.code, c.exception.rc), ("E_TRUTH_QUANT", 2))

    def test_regions(self):
        rows = L.regions_truth(1, self.labs[1], self.g, {1: "box"})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ob"], r["name"], r["mv"]), (1, "box", [3, -2]))
        self.assertTrue(r["id"].startswith("r1:"))
        self.assertGreaterEqual(r["n_tiles"], 1)

    def test_cli_end_to_end(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            tr = os.path.join(d, "truth")
            os.makedirs(tr)
            frs = [frame((16, 16, 64, 48)), frame((19, 14, 67, 46), mv=(3, -2))]
            with open(os.path.join(tr, "frames.jsonl"), "w") as fh:
                for i, f in enumerate(frs, 1):
                    for k in ("ob", "mv", "z"):
                        np.save(os.path.join(tr, "f%06d.%s.npy" % (i, k)), f[k])
                    fh.write(json.dumps({"render_index": i, "width": W, "height": H,
                                         "grid": self.g.ident, "motion_blur": False}) + "\n")
            with open(os.path.join(d, "scene.manifest.json"), "w") as mf:
                json.dump({"objects": [{"name": "box", "pass_index": 1}]}, mf)
            res = L.run(d, {}, os.path.join(d, "labels"))
            self.assertEqual((res["rc"], res["frames"]), (0, 2))
            with open(os.path.join(d, "labels", "tiles_truth.bin"), "rb") as bf:
                blob = bf.read()
            h, arrs = L.parse_truth_tiles(blob, self.g)
            self.assertEqual(int((arrs[1]["tl"] == L.TL_ID["RIGID"]).sum()), 2)
            # motion blur refusal
            with open(os.path.join(tr, "frames.jsonl"), "w") as fh:
                fh.write(json.dumps({"render_index": 1, "width": W, "height": H, "motion_blur": True}) + "\n")
            with self.assertRaises(L.VqaError) as c:
                L.run(d, {}, os.path.join(d, "labels2"))
            self.assertEqual(c.exception.code, "E_TRUTH_MB")
            self.assertFalse(os.path.exists(os.path.join(d, "labels2")))


if __name__ == "__main__":
    unittest.main()
