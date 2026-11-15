"""S4 regions tests (SPEC_CORE §5): islands, groups, order independence, adjacency, links; positive, negative, mutants."""
import random
import unittest

import numpy as np

from visioncore import canon, layout, regions as R
from visioncore.change import TILE_DT
from visioncore.errors import VqaError

W, H = 1280, 720  # s16, 80x45


def mk(cells, W=W, H=H):
    """cells: dict (tx,ty)->dict(cls,..) -> tile array over those cells only (Morton ascending)."""
    items = sorted(((layout.morton(x, y), v) for (x, y), v in cells.items()))
    t = np.zeros(len(items), TILE_DT)
    for i, (m, v) in enumerate(items):
        t[i]["morton"] = m
        t[i]["cls"] = v.get("cls", 2)
        t[i]["flags"] = v.get("flags", 0)
        t[i]["resid"] = v.get("resid", 10)
        t[i]["mv_dx"] = v.get("dx", 0)
        t[i]["mv_dy"] = v.get("dy", 0)
        t[i]["edge"] = v.get("edge", 0)
    return t


def box(x0, y0, x1, y1, **v):
    return {(x, y): dict(v) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


class TestIslands(unittest.TestCase):
    def test_two_islands(self):
        c = box(0, 0, 1, 1)
        c.update(box(10, 10, 10, 12))
        rs = R.regions_of(mk(c), W, H, 5)
        self.assertEqual([r["n_tiles"] for r in rs], [4, 3])
        self.assertEqual(rs[0]["id"], "r5:0000")
        self.assertEqual(rs[0]["bbox_t"], [0, 0, 1, 1])
        self.assertEqual(rs[0]["bbox_px"], [0, 0, 32, 32])
        self.assertEqual(rs[0]["schema"], "vqa.core-region/v1")
        self.assertEqual(rs[0]["grid"], "grid:1280x720:s16")

    def test_diagonal_is_connected(self):
        rs = R.regions_of(mk({(0, 0): {}, (1, 1): {}}), W, H, 0)
        self.assertEqual(len(rs), 1)

    def test_gap_is_not_connected(self):
        rs = R.regions_of(mk({(0, 0): {}, (2, 0): {}}), W, H, 0)
        self.assertEqual(len(rs), 2)

    def test_clean_tiles_ignored(self):
        rs = R.regions_of(mk({(0, 0): {"cls": 0}, (1, 0): {"cls": 1}}), W, H, 0)
        self.assertEqual(rs, [])

    def test_groups_split_touching_tiles(self):
        c = {(0, 0): {"cls": 2}, (1, 0): {"cls": 5}, (2, 0): {"cls": 3, "dx": 4, "dy": 0},
             (3, 0): {"cls": 3, "dx": 5, "dy": 0}}
        rs = R.regions_of(mk(c), W, H, 1)
        self.assertEqual(sorted(r["group"] for r in rs), ["CHG", "FRZ", "MOV", "MOV"])
        mv = sorted(tuple(r["mv"]) for r in rs if r["group"] == "MOV")
        self.assertEqual(mv, [(4, 0), (5, 0)])

    def test_chg_group_merges_flicker_drift(self):
        c = {(0, 0): {"cls": 2}, (1, 0): {"cls": 4}, (2, 0): {"cls": 6}}
        rs = R.regions_of(mk(c), W, H, 1)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["cls_hist"], [0, 0, 1, 0, 1, 0, 1])
        self.assertEqual(rs[0]["cls"], "CHANGED")  # argmax ties -> lower enum

    def test_stats_spans_block(self):
        c = {(0, 0): {"resid": 7, "edge": 100}, (1, 0): {"resid": 9, "edge": 50}, (4, 0): {"resid": 1}}
        rs = R.regions_of(mk(c), W, H, 2)
        r = rs[0]
        self.assertEqual((r["r_max"], r["r_sum"], r["n_tiles"]), (9, 16, 2))
        self.assertEqual(r["edge_ppm"], 1000000 * 150 // (255 * 2))
        self.assertEqual(r["spans"], [["0000", "0001"]])
        self.assertEqual(r["block"], [1, "0000", "0003"])

    def test_edge_tile_bbox_clipped(self):
        gw, gh = layout.grid_params(W, H)[1:]
        rs = R.regions_of(mk({(gw - 1, gh - 1): {}}), W, H, 0)
        x, y, w, h = rs[0]["bbox_px"]
        self.assertEqual((x + w, y + h), (W, H))

    def test_json_canonical_ints(self):
        rs = R.regions_of(mk(box(0, 0, 3, 3, flags=R.FLAG_UI_OCC)), W, H, 3)
        canon.dumps(rs[0])  # integers only, no floats
        self.assertEqual(rs[0]["occ"], ["ui"])


class TestKind(unittest.TestCase):
    def test_kinds(self):
        s = 16
        self.assertEqual(R._kind_of(R.FLAG_EDGE, 40, s), 0)
        self.assertEqual(R._kind_of(R.FLAG_TEXTURE, 200, s), 1)
        self.assertEqual(R._kind_of(0, 0, s), 2)
        self.assertEqual(R._kind_of(0, 10, s), 3)

    def test_edge_vs_smooth_split(self):
        edge = mk(box(0, 0, 2, 2, flags=R.FLAG_EDGE, edge=40))
        smooth = mk(box(0, 0, 2, 2, flags=R.FLAG_SHADE_ONLY))
        self.assertEqual(R.regions_of(edge, W, H, 0)[0]["kind"], "EDGE")
        self.assertFalse(R.regions_of(edge, W, H, 0)[0]["shade_only"])
        r = R.regions_of(smooth, W, H, 0)[0]
        self.assertEqual(r["kind"], "SMOOTH")
        self.assertTrue(r["shade_only"])

    def test_shade_only_needs_90_percent(self):
        c = box(0, 0, 9, 0, flags=R.FLAG_SHADE_ONLY)
        c[(0, 0)] = {"flags": 0}
        c[(1, 0)] = {"flags": 0}  # 8/10 shaded
        self.assertFalse(R.regions_of(mk(c), W, H, 0)[0]["shade_only"])


class TestOrderIndependence(unittest.TestCase):
    def test_permuted_input_same_output(self):
        rnd = random.Random(7)
        c = {}
        for _ in range(300):
            c[(rnd.randrange(30), rnd.randrange(20))] = {"cls": rnd.choice([2, 4, 5, 6]), "resid": rnd.randrange(255)}
        t = mk(c)
        a = canon.dumps(R.regions_of(t, W, H, 9))
        for seed in range(5):
            idx = list(range(len(t)))
            random.Random(seed).shuffle(idx)
            self.assertEqual(canon.dumps(R.regions_of(t[idx], W, H, 9)), a)

    def test_label_is_min_morton(self):
        c = box(3, 3, 6, 6)
        r = R.regions_of(mk(c), W, H, 0)[0]
        self.assertEqual(r["id"], "r0:%04x" % min(layout.morton(x, y) for x, y in c))

    def test_mutant_first_seen_label_dies(self):
        # mutant: key = first visited tile (raster order) instead of min Morton; must differ on a column island
        c = box(1, 0, 2, 3)
        first_raster = layout.morton(1, 0)
        mn = min(layout.morton(x, y) for x, y in c)
        self.assertNotEqual(first_raster, 0)
        self.assertEqual(R.regions_of(mk(c), W, H, 0)[0]["id"], "r0:%04x" % mn)


class TestNegative(unittest.TestCase):
    def test_out_of_grid_morton(self):
        t = mk({(0, 0): {}})
        t[0]["morton"] = layout.morton(200, 200)
        with self.assertRaises(VqaError) as e:
            R.regions_of(t, W, H, 0)
        self.assertEqual(e.exception.code, "E_VC_ID_RANGE")

    def test_bad_frame(self):
        with self.assertRaises(VqaError):
            R.regions_of(mk({(0, 0): {}}), W, H, -1)

    def test_bad_size(self):
        with self.assertRaises(VqaError):
            R.regions_of(mk({(0, 0): {}}), 0, H, 0)

    def test_grid_mismatch_in_match(self):
        a = R.regions_of(mk({(0, 0): {}}), W, H, 0)
        b = R.regions_of(mk({(0, 0): {}}, 1920, 1080), 1920, 1080, 1)
        with self.assertRaises(VqaError) as e:
            R.match_regions(a, b)
        self.assertEqual(e.exception.code, "E_VC_GRID_MISMATCH")


class TestAdjacency(unittest.TestCase):
    def test_touching_groups(self):
        c = {(0, 0): {"cls": 2}, (1, 0): {"cls": 5}, (2, 5): {"cls": 5}}
        rs = R.regions_of(mk(c), W, H, 0)
        adj = R.adjacency(rs)
        self.assertEqual(len(adj), 1)
        self.assertEqual(adj[0][2], 1)
        self.assertLess(adj[0][0], adj[0][1])

    def test_diagonal_contact_and_counts(self):
        c = {(0, 0): {"cls": 2}, (1, 0): {"cls": 2}, (2, 1): {"cls": 5}, (1, 1): {"cls": 5}}
        rs = R.regions_of(mk(c), W, H, 0)
        adj = R.adjacency(rs)
        self.assertEqual(adj[0][2], 3)  # (0,0)-(1,1), (1,0)-(1,1), (1,0)-(2,1)

    def test_none_when_isolated(self):
        rs = R.regions_of(mk({(0, 0): {}, (9, 9): {}}), W, H, 0)
        self.assertEqual(R.adjacency(rs), [])


class TestMotionMoore(unittest.TestCase):
    def test_in_moore(self):
        rs = R.regions_of(mk({(0, 0): {"cls": 3, "dx": 16, "dy": -3}}), W, H, 0)
        self.assertTrue(R.mv_in_moore(rs, 16))
        self.assertFalse(R.mv_in_moore(rs, 8))


class TestMatch(unittest.TestCase):
    def reg(self, cells, f):
        return R.regions_of(mk(cells), W, H, f)

    def test_translation_with_global_mv(self):
        a = self.reg(box(10, 10, 13, 13), 4)
        b = self.reg(box(11, 10, 14, 13), 5)
        lk = R.match_regions(a, b, (16, 0))
        self.assertEqual(lk["links"], [[a[0]["id"], b[0]["id"], 16]])
        self.assertEqual(lk["frame"], 5)
        self.assertEqual(lk["schema"], "vqa.core-link/v1")
        # without compensation overlap is 12/16 = 75 % >= 50 %: still linked
        self.assertEqual(len(R.match_regions(a, b)["links"]), 1)

    def test_below_overlap_rejected(self):
        a = self.reg(box(10, 10, 13, 13), 4)
        b = self.reg(box(13, 10, 16, 13), 5)  # overlap 4/16 = 25 %
        lk = R.match_regions(a, b)
        self.assertEqual((lk["links"], lk["split"], lk["merge"]), ([], [], []))

    def test_split(self):
        a = self.reg(box(10, 10, 15, 10), 4)  # 6 tiles
        c = box(10, 10, 12, 10)
        c.update(box(14, 10, 15, 10))  # gap at 13
        b = self.reg(c, 5)
        lk = R.match_regions(a, b)
        self.assertEqual(len(lk["links"]), 1)
        self.assertEqual(len(lk["split"]), 1)
        self.assertEqual(lk["merge"], [])
        self.assertEqual(lk["links"][0][2], 3)

    def test_merge(self):
        c = box(10, 10, 12, 10)
        c.update(box(14, 10, 16, 10))
        a = self.reg(c, 4)
        b = self.reg(box(10, 10, 16, 10), 5)
        lk = R.match_regions(a, b)
        self.assertEqual(len(lk["links"]), 1)
        self.assertEqual(len(lk["merge"]), 1)
        self.assertEqual(lk["split"], [])

    def test_one_to_one_and_deterministic(self):
        a = self.reg(box(10, 10, 11, 11), 4)
        b = self.reg(box(10, 10, 11, 11), 5)
        l1 = R.match_regions(a, b)
        l2 = R.match_regions(list(reversed(a)), list(reversed(b)))
        self.assertEqual(canon.dumps(l1), canon.dumps(l2))

    def test_empty(self):
        self.assertEqual(R.match_regions([], [])["links"], [])

    def test_floor_shift_negative(self):
        # mv -9 px at s16: (-9+8)//16 = -1 (floor), truncation would give 0 (I4)
        a = self.reg(box(10, 10, 11, 10), 4)
        b = self.reg(box(9, 10, 10, 10), 5)
        lk = R.match_regions(a, b, (-9, 0))
        self.assertEqual(lk["links"][0][2], 2)



class TestMatchOwnMv(unittest.TestCase):  # FC-A5: a MOV region is matched at its own displacement
    def _seq(self, with_mv):
        a = R.regions_of(mk({(10, 10): {"cls": 3 if with_mv else 2, "dx": 16 if with_mv else 0, "dy": 0}}), W, H, 0)
        b = R.regions_of(mk({(11, 10): {"cls": 3 if with_mv else 2, "dx": 16 if with_mv else 0, "dy": 0}}), W, H, 1)
        return a, b

    def test_match_projectile(self):
        a, b = self._seq(True)
        self.assertEqual(a[0]["group"], "MOV")
        lk = R.match_regions(a, b)  # fixed camera: global_mv (0,0)
        self.assertEqual(len(lk["links"]), 1)
        self.assertEqual((lk["split"], lk["merge"]), ([], []))

    def test_projectile_chain_10_frames(self):
        regs = [R.regions_of(mk({(10 + f, 10): {"cls": 3, "dx": 16, "dy": 0}}), W, H, f) for f in range(10)]
        links = sum(len(R.match_regions(regs[i], regs[i + 1])["links"]) for i in range(9))
        self.assertEqual(links, 9)

    def test_non_mov_region_ignores_own_shift(self):  # negative: CHANGED region moving 1 tile has no overlap
        a, b = self._seq(False)
        self.assertEqual(a[0]["group"], "CHG")
        self.assertEqual(R.match_regions(a, b)["links"], [])


if __name__ == "__main__":
    unittest.main()
