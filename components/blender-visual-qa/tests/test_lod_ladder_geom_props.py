"""Fixed-seed property tests for LOD ladder geometry checks in blender_visual_qa/assets/lod_ladder.py."""
import math
import random
import unittest
from unittest import mock

from blender_visual_qa.assets import glb_core as g
from blender_visual_qa.assets import lod_ladder as L
from tests.test_ag2_lod import sphere


def _make_sphere_prim(name, seg, rings, r_um=100000, offset=(0, 0, 0)):
    p = sphere(name, seg, rings, r_um=r_um)
    if offset != (0, 0, 0):
        pos = [(x + offset[0], y + offset[1], z + offset[2]) for x, y, z in p.positions_um]
        p = g.Prim(p.name, p.node_path, p.world_q16, pos, p.indices, p.uvs_q16, p.normals_q16, p.material)
    return p


class TestLodLadderGeomProps(unittest.TestCase):

    def test_budget_property(self):
        """budget matches its docstring formula for seeds 0..7 and negative cases."""
        for seed in range(8):
            rng = random.Random(seed)
            f0 = rng.randint(0, 5000)
            permille = rng.randint(0, 2000)
            custom_floor = rng.choice([None, 50, 100, 200])
            res = L.budget(f0, permille, floor=custom_floor)
            flr = L.FLOOR if custom_floor is None else custom_floor
            expected = min(f0, max((f0 * permille + 500) // 1000, flr))
            self.assertEqual(res, expected)

        # Exact formula checks
        self.assertEqual(L.budget(1000, 500), 500)
        self.assertEqual(L.budget(1001, 500), 501)
        self.assertEqual(L.budget(200, 250), 120)
        self.assertEqual(L.budget(100, 250), 100)

        # Negative case: wrong prediction or mismatched input fails assertion
        self.assertNotEqual(L.budget(1000, 500), 499)
        self.assertNotEqual(L.budget(200, 250, floor=150), 120)

    def test_tri_soup_property(self):
        """tri_soup correctly concatenates world points and offsets triangle indices."""
        for seed in range(8):
            rng = random.Random(seed)
            p1 = _make_sphere_prim("p1", 6, 4, r_um=1000, offset=(rng.randint(-500, 500), 0, 0))
            p2 = _make_sphere_prim("p2", 8, 4, r_um=1500, offset=(rng.randint(-500, 500), 0, 0))
            pts, tris = L.tri_soup([p1, p2])
            self.assertEqual(len(pts), len(p1.positions_um) + len(p2.positions_um))
            self.assertEqual(len(tris), len(p1.indices) // 3 + len(p2.indices) // 3)
            # Verify indexing of second prim
            offset = len(p1.positions_um)
            p2_first_tri = (offset + p2.indices[0], offset + p2.indices[1], offset + p2.indices[2])
            self.assertEqual(tris[len(p1.indices) // 3], p2_first_tri)

        # Negative case: empty prim list returns empty lists
        pts_empty, tris_empty = L.tri_soup([])
        self.assertEqual(pts_empty, [])
        self.assertEqual(tris_empty, [])

    def test_geom_sha_property(self):
        """geom_sha is invariant under triangle order and vertex rotation."""
        for seed in range(8):
            rng = random.Random(seed)
            p1 = _make_sphere_prim("mesh", 8, 6, r_um=5000)
            sha_orig = L.geom_sha([p1])

            # Invariance under triangle order shuffle
            pts, tris = L.tri_soup([p1])
            tris_shuffled = list(tris)
            rng.shuffle(tris_shuffled)
            idx_shuffled = [i for t in tris_shuffled for i in t]
            p_shuffled = g.Prim("mesh", "/mesh", g.IDENTITY_Q16, pts, idx_shuffled)
            self.assertEqual(L.geom_sha([p_shuffled]), sha_orig)

            # Invariance under vertex rotation within triangles (v0, v1, v2) -> (v1, v2, v0)
            tris_rotated = [(t[1], t[2], t[0]) for t in tris]
            idx_rotated = [i for t in tris_rotated for i in t]
            p_rotated = g.Prim("mesh", "/mesh", g.IDENTITY_Q16, pts, idx_rotated)
            self.assertEqual(L.geom_sha([p_rotated]), sha_orig)

            # Negative case: changing a vertex position alters sha
            pts_mutated = list(pts)
            pts_mutated[0] = (pts_mutated[0][0] + 100, pts_mutated[0][1], pts_mutated[0][2])
            p_mutated = g.Prim("mesh", "/mesh", g.IDENTITY_Q16, pts_mutated, p1.indices)
            self.assertNotEqual(L.geom_sha([p_mutated]), sha_orig)

    def test_pt_tri_dist_property(self):
        """pt_tri_dist equals brute-force minimum over dense barycentric samples and is zero for points on triangle."""
        for seed in range(8):
            rng = random.Random(seed)
            a = (rng.randint(-1000, 1000), rng.randint(-1000, 1000), rng.randint(-1000, 1000))
            b = (a[0] + rng.randint(100, 1000), a[1] + rng.randint(-500, 500), a[2] + rng.randint(-500, 500))
            c = (a[0] + rng.randint(-500, 500), a[1] + rng.randint(100, 1000), a[2] + rng.randint(-500, 500))
            p = (rng.randint(-2000, 2000), rng.randint(-2000, 2000), rng.randint(-2000, 2000))

            dist_calc = L.pt_tri_dist(p, a, b, c)

            # Brute force sample with grid step size 30
            steps = 30
            min_bf = float("inf")
            for i in range(steps + 1):
                u = i / steps
                for j in range(steps + 1 - i):
                    v = j / steps
                    qx = a[0] + u * (b[0] - a[0]) + v * (c[0] - a[0])
                    qy = a[1] + u * (b[1] - a[1]) + v * (c[1] - a[1])
                    qz = a[2] + u * (b[2] - a[2]) + v * (c[2] - a[2])
                    d = math.sqrt((p[0] - qx) ** 2 + (p[1] - qy) ** 2 + (p[2] - qz) ** 2)
                    if d < min_bf:
                        min_bf = d

            # Distance should be within sampling resolution tolerance
            self.assertAlmostEqual(dist_calc, min_bf, delta=max(50.0, 0.05 * min_bf))

            # Point on triangle has distance 0.0
            u_on, v_on = 0.3, 0.4
            p_on = (a[0] + u_on * (b[0] - a[0]) + v_on * (c[0] - a[0]),
                    a[1] + u_on * (b[1] - a[1]) + v_on * (c[1] - a[1]),
                    a[2] + u_on * (b[2] - a[2]) + v_on * (c[2] - a[2]))
            self.assertAlmostEqual(L.pt_tri_dist(p_on, a, b, c), 0.0, places=5)

            # Negative case: point off triangle returns positive non-zero distance
            p_off = (a[0] + 5000, a[1] + 5000, a[2] + 5000)
            self.assertGreater(L.pt_tri_dist(p_off, a, b, c), 100.0)

    def test_tri_grid_property(self):
        """TriGrid.dist equals brute-force minimum over all triangles in the grid."""
        for seed in range(8):
            rng = random.Random(seed)
            prim = _make_sphere_prim("mesh", 6, 4, r_um=2000, offset=(rng.randint(-500, 500), 0, 0))
            pts, tris = L.tri_soup([prim])
            p = (rng.randint(-3000, 3000), rng.randint(-3000, 3000), rng.randint(-3000, 3000))
            diag = L.bbox_diag_um(pts)
            cell = max(diag / L.GRID_CELLS, 1e-9)

            grid = L.TriGrid(pts, tris, cell)
            grid_dist = grid.dist(p, diag)

            bf_dist = min(L.pt_tri_dist(p, pts[i], pts[j], pts[k]) for i, j, k in tris)
            expected = bf_dist if bf_dist < diag else diag
            self.assertAlmostEqual(grid_dist, expected, places=5)

            # Negative case: empty grid / no hit within rmax returns rmax
            empty_grid = L.TriGrid([], [], 10.0)
            self.assertEqual(empty_grid.dist(p, 100.0), 100.0)

    def test_one_way_property(self):
        """one_way distance properties and negative cases."""
        for seed in range(8):
            rng = random.Random(seed)
            p1 = _make_sphere_prim("m1", 6, 4, r_um=1000)
            p2 = _make_sphere_prim("m2", 6, 4, r_um=2000)
            pts1, tris1 = L.tri_soup([p1])
            pts2, tris2 = L.tri_soup([p2])
            diag = L.bbox_diag_um(pts1 + pts2)

            d12 = L.one_way(pts1, tris1, pts2, tris2, diag, samples=10)
            self.assertGreaterEqual(d12, 0.0)
            self.assertLessEqual(d12, diag)

            # Negative case: empty destination triangles returns diag
            self.assertEqual(L.one_way(pts1, tris1, [], [], diag, samples=10), diag)

    def test_hausdorff_um_property(self):
        """hausdorff_um of a mesh against itself is (0, 0) and is symmetric under swapping arguments."""
        for seed in range(8):
            rng = random.Random(seed)
            p1 = _make_sphere_prim("m1", 6, 4, r_um=1000, offset=(rng.randint(-200, 200), 0, 0))
            p2 = _make_sphere_prim("m2", 8, 5, r_um=1500, offset=(rng.randint(-200, 200), 0, 0))
            diag = L.bbox_diag_um(L.tri_soup([p1, p2])[0])

            # Self hausdorff distance is (0, 0)
            self.assertEqual(L.hausdorff_um([p1], [p1], diag, samples=10), (0, 0))

            # Argument swapping symmetry: hausdorff_um(A, B) == (hb(B, A), hf(B, A))
            h_ab = L.hausdorff_um([p1], [p2], diag, samples=10)
            h_ba = L.hausdorff_um([p2], [p1], diag, samples=10)
            self.assertEqual(h_ab, (h_ba[1], h_ba[0]))

            # Negative case: non-overlapping disjoint meshes have non-zero hausdorff distance
            p3 = _make_sphere_prim("m3", 6, 4, r_um=500, offset=(10000, 10000, 10000))
            diag3 = L.bbox_diag_um(L.tri_soup([p1, p3])[0])
            h_disjoint = L.hausdorff_um([p1], [p3], diag3, samples=10)
            self.assertGreater(h_disjoint[0], 0)
            self.assertGreater(h_disjoint[1], 0)

    def test_bbox_diag_um_property(self):
        """bbox_diag_um is exact for seeds 0..7 and handles empty input."""
        for seed in range(8):
            rng = random.Random(seed)
            pts = [(rng.randint(-5000, 5000), rng.randint(-5000, 5000), rng.randint(-5000, 5000)) for _ in range(20)]
            lo = [min(p[k] for p in pts) for k in range(3)]
            hi = [max(p[k] for p in pts) for k in range(3)]
            expected = math.isqrt(sum((hi[k] - lo[k]) ** 2 for k in range(3)))
            self.assertEqual(L.bbox_diag_um(pts), expected)

        # Negative case: empty points list returns 0
        self.assertEqual(L.bbox_diag_um([]), 0)

    def test_ntris_property(self):
        """ntris calculation is exact and handles empty list."""
        for seed in range(8):
            rng = random.Random(seed)
            p1 = _make_sphere_prim("p1", rng.randint(4, 10), rng.randint(3, 8))
            p2 = _make_sphere_prim("p2", rng.randint(4, 10), rng.randint(3, 8))
            expected = len(p1.indices) // 3 + len(p2.indices) // 3
            self.assertEqual(L.ntris([p1, p2]), expected)

        # Negative case: empty prim list returns 0
        self.assertEqual(L.ntris([]), 0)

    def test_verify_ladder_fails_when_tri_count_exceeds_budget(self):
        """verify_ladder passes valid ladders and fails when a level triangle count exceeds its budget."""
        with mock.patch.object(L, "SAMPLES", 10):
            for seed in range(8):
                rng = random.Random(seed)
                # Create a source mesh with e.g. 528 tris
                lod0 = _make_sphere_prim("obj_LOD0", 24, 12, r_um=5000)
                # Budget caps for 528: cap0=528, cap1=264, cap2=132
                lod1_valid = _make_sphere_prim("obj_LOD1", 16, 8, r_um=5000)  # 224 tris <= 264
                lod2_valid = _make_sphere_prim("obj_LOD2", 12, 6, r_um=5000)  # 120 tris <= 132

                scene_valid = g.Scene([lod0, lod1_valid, lod2_valid])
                checks, per = L.verify_ladder(scene_valid)
                failed = [c["id"] for c in checks if not c["ok"]]
                self.assertEqual(failed, [])

                # Invalid case: LOD1 triangle count exceeds cap (360 tris > cap 264)
                lod1_invalid = _make_sphere_prim("obj_LOD1", 20, 10, r_um=5000)  # 360 tris > 264
                scene_invalid = g.Scene([lod0, lod1_invalid, lod2_valid])
                checks_inv, per_inv = L.verify_ladder(scene_invalid)
                failed_inv = [c["id"] for c in checks_inv if not c["ok"]]
                self.assertIn("obj:lod1_rule", failed_inv)


if __name__ == "__main__":
    unittest.main()
