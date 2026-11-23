"""Fixed-seed property tests for integer geometry helpers of mesh_gate.

Tests _cross, _det3, _find, _union, _tri_overlap_2d, _uv_area2, _uv_overlap, _bool01.
Uses stdlib and integer arithmetic with random.Random seeds 0 to 9.
"""
import random
import unittest

from blender_visual_qa.assets.mesh_gate import (
    _bool01,
    _cross,
    _det3,
    _find,
    _tri_overlap_2d,
    _union,
    _uv_area2,
    _uv_overlap,
)


def _strict_in_grid_int(x, y, scale, tri):
    """Reference point-in-triangle test on grid with scale factor. Returns True if strictly inside."""
    o, a, b = tri[0], tri[1], tri[2]
    c0 = (a[0] - o[0]) * (y - o[1] * scale) - (a[1] - o[1]) * (x - o[0] * scale)
    c1 = (b[0] - a[0]) * (y - a[1] * scale) - (b[1] - a[1]) * (x - a[0] * scale)
    c2 = (o[0] - b[0]) * (y - b[1] * scale) - (o[1] - b[1]) * (x - b[0] * scale)
    return (c0 > 0 and c1 > 0 and c2 > 0) or (c0 < 0 and c1 < 0 and c2 < 0)


def _ref_tri_overlap_grid_int(ta, tb, scale=60):
    """Reference brute-force rational point-in-triangle overlap on a small integer grid."""
    min_x = max(min(v[0] for v in ta), min(v[0] for v in tb))
    max_x = min(max(v[0] for v in ta), max(v[0] for v in tb))
    min_y = max(min(v[1] for v in ta), min(v[1] for v in tb))
    max_y = min(max(v[1] for v in ta), max(v[1] for v in tb))

    if min_x >= max_x or min_y >= max_y:
        return False

    for x in range(min_x * scale + 1, max_x * scale):
        for y in range(min_y * scale + 1, max_y * scale):
            if _strict_in_grid_int(x, y, scale, ta) and _strict_in_grid_int(x, y, scale, tb):
                return True
    return False


class TestMeshGateHelpersProps(unittest.TestCase):

    def test_cross_props(self):
        """Property tests for _cross across seeds 0..9."""
        # Exact values / negative cases
        self.assertEqual(_cross((1, 0, 0), (0, 1, 0)), (0, 0, 1))
        self.assertEqual(_cross((1, 2, 3), (4, 5, 6)), (-3, 6, -3))
        # Negative case: parallel vectors produce zero vector
        self.assertEqual(_cross((1, 2, 3), (2, 4, 6)), (0, 0, 0))
        self.assertEqual(_cross((0, 0, 0), (5, 9, 13)), (0, 0, 0))

        for seed in range(10):
            rng = random.Random(seed)
            for _ in range(20):
                a = (rng.randint(-100, 100), rng.randint(-100, 100), rng.randint(-100, 100))
                b = (rng.randint(-100, 100), rng.randint(-100, 100), rng.randint(-100, 100))

                # Matches cofactor formula
                expected = (
                    a[1] * b[2] - a[2] * b[1],
                    a[2] * b[0] - a[0] * b[2],
                    a[0] * b[1] - a[1] * b[0],
                )
                res = _cross(a, b)
                self.assertEqual(res, expected)

                # Anti-symmetry
                self.assertEqual(res, tuple(-x for x in _cross(b, a)))

                # Orthogonality
                self.assertEqual(a[0] * res[0] + a[1] * res[1] + a[2] * res[2], 0)
                self.assertEqual(b[0] * res[0] + b[1] * res[1] + b[2] * res[2], 0)

    def test_det3_props(self):
        """Property tests for _det3 across seeds 0..9."""
        # Exact values and negative cases
        m_id = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
        self.assertEqual(_det3(m_id), 1)

        m_swap = (0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
        self.assertEqual(_det3(m_swap), -1)

        # Negative case: linearly dependent rows give determinant 0
        m_deg = (1, 2, 3, 0, 1, 2, 3, 0, 4, 5, 6, 0, 0, 0, 0, 1)
        self.assertEqual(_det3(m_deg), 0)

        for seed in range(10):
            rng = random.Random(seed)
            for _ in range(20):
                m = tuple(rng.randint(-50, 50) for _ in range(16))
                r0, r1, r2 = (m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])

                # Match cofactor expansion formula
                expected = (
                    r0[0] * (r1[1] * r2[2] - r1[2] * r2[1])
                    - r0[1] * (r1[0] * r2[2] - r1[2] * r2[0])
                    + r0[2] * (r1[0] * r2[1] - r1[1] * r2[0])
                )
                self.assertEqual(_det3(m), expected)

    def test_find_and_union_props(self):
        """Property tests for _find and _union across seeds 0..9."""
        # Exact value / negative case for initial disconnected sets
        parent_init = {0: 0, 1: 1, 2: 2}
        self.assertEqual(_find(parent_init, 0), 0)
        self.assertEqual(_find(parent_init, 1), 1)
        self.assertNotEqual(_find(parent_init, 0), _find(parent_init, 1))

        # Union connects them
        _union(parent_init, 0, 1)
        self.assertEqual(_find(parent_init, 0), _find(parent_init, 1))
        self.assertNotEqual(_find(parent_init, 0), _find(parent_init, 2))

        for seed in range(10):
            rng = random.Random(seed)
            n = 25
            parent = {i: i for i in range(n)}
            components = [{i} for i in range(n)]

            for _ in range(30):
                u = rng.randint(0, n - 1)
                v = rng.randint(0, n - 1)
                _union(parent, u, v)

                # Update reference partition
                comp_u = next(c for c in components if u in c)
                comp_v = next(c for c in components if v in c)
                if comp_u is not comp_v:
                    comp_u.update(comp_v)
                    components.remove(comp_v)

            # Verify against reference partition
            for i in range(n):
                root_i = _find(parent, i)
                self.assertIn(root_i, parent)
                comp_i = next(c for c in components if i in c)
                for j in range(n):
                    root_j = _find(parent, j)
                    if j in comp_i:
                        self.assertEqual(root_i, root_j)
                    else:
                        self.assertNotEqual(root_i, root_j)

    def test_tri_overlap_2d_props(self):
        """Property tests for _tri_overlap_2d across seeds 0..9."""
        # False for triangles touching only at edge or vertex
        t_base = ((0, 0), (6, 0), (0, 6))

        # Edge-touching
        t_edge = ((0, 0), (6, 0), (3, -6))
        self.assertFalse(_tri_overlap_2d(t_base, t_edge))
        self.assertFalse(_tri_overlap_2d(t_edge, t_base))

        # Vertex-touching
        t_vert = ((0, 0), (-6, 0), (0, -6))
        self.assertFalse(_tri_overlap_2d(t_base, t_vert))
        self.assertFalse(_tri_overlap_2d(t_vert, t_base))

        # Shared edge segment
        t_seg = ((2, 0), (4, 0), (3, -5))
        self.assertFalse(_tri_overlap_2d(t_base, t_seg))

        # True for strictly smaller copy inside it
        t_inner = ((1, 1), (3, 1), (1, 3))
        self.assertTrue(_tri_overlap_2d(t_base, t_inner))
        self.assertTrue(_tri_overlap_2d(t_inner, t_base))

        # Negative case: disjoint triangles
        t_disjoint = ((10, 10), (15, 10), (10, 15))
        self.assertFalse(_tri_overlap_2d(t_base, t_disjoint))

        for seed in range(10):
            rng = random.Random(seed)
            for _ in range(15):
                def rand_tri():
                    while True:
                        t = (
                            (rng.randint(0, 6), rng.randint(0, 6)),
                            (rng.randint(0, 6), rng.randint(0, 6)),
                            (rng.randint(0, 6), rng.randint(0, 6)),
                        )
                        if _uv_area2(t) != 0:
                            return t

                ta, tb = rand_tri(), rand_tri()

                # Symmetry
                res_ab = _tri_overlap_2d(ta, tb)
                res_ba = _tri_overlap_2d(tb, ta)
                self.assertEqual(res_ab, res_ba)

                # Check against brute-force rational point-in-triangle grid reference
                ref_res = _ref_tri_overlap_grid_int(ta, tb, scale=60)
                self.assertEqual(res_ab, ref_res)

    def test_uv_area2_props(self):
        """Property tests for _uv_area2 across seeds 0..9."""
        # Exact values and negative cases
        t_ccw = ((0, 0), (4, 0), (0, 4))
        self.assertEqual(_uv_area2(t_ccw), 16)  # 2 * (1/2 * 4 * 4) = 16

        t_cw = ((0, 0), (0, 4), (4, 0))
        self.assertEqual(_uv_area2(t_cw), -16)

        # Negative case: collinear / degenerate face gives 0 area
        t_collinear = ((0, 0), (2, 2), (4, 4))
        self.assertEqual(_uv_area2(t_collinear), 0)

        for seed in range(10):
            rng = random.Random(seed)
            for _ in range(20):
                t = (
                    (rng.randint(-100, 100), rng.randint(-100, 100)),
                    (rng.randint(-100, 100), rng.randint(-100, 100)),
                    (rng.randint(-100, 100), rng.randint(-100, 100)),
                )

                # Equals twice signed area
                expected_twice_area = (t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) - (
                    t[1][1] - t[0][1]
                ) * (t[2][0] - t[0][0])
                area2 = _uv_area2(t)
                self.assertEqual(area2, expected_twice_area)

                # Flips sign under vertex swaps
                self.assertEqual(_uv_area2((t[1], t[0], t[2])), -area2)
                self.assertEqual(_uv_area2((t[0], t[2], t[1])), -area2)
                self.assertEqual(_uv_area2((t[2], t[1], t[0])), -area2)

    def test_uv_overlap_props(self):
        """Property tests for _uv_overlap across seeds 0..9."""
        # Negative case: disjoint set of triangles gives empty set
        disjoint_tris = [
            (0, ((0, 0), (1000, 0), (0, 1000))),
            (1, ((5000, 5000), (6000, 5000), (5000, 6000))),
            (2, ((10000, 10000), (11000, 10000), (10000, 11000))),
        ]
        self.assertEqual(_uv_overlap(disjoint_tris), set())

        # Overlapping triangles
        overlapping_tris = [
            (10, ((0, 0), (4000, 0), (0, 4000))),
            (20, ((1000, 1000), (3000, 1000), (1000, 3000))),
            (30, ((10000, 10000), (11000, 10000), (10000, 11000))),
        ]
        self.assertEqual(_uv_overlap(overlapping_tris), {10, 20})

        for seed in range(10):
            rng = random.Random(seed)
            tris = []
            for i in range(15):
                ox, oy = rng.randint(0, 10) * 4000, rng.randint(0, 10) * 4000
                w, h = rng.randint(1000, 4000), rng.randint(1000, 4000)
                tri = ((ox, oy), (ox + w, oy), (ox, oy + h))
                tris.append((i, tri))

            # Reference pairwise overlap check using _tri_overlap_2d
            ref_hit = set()
            for a_idx in range(len(tris)):
                for b_idx in range(a_idx + 1, len(tris)):
                    if _tri_overlap_2d(tris[a_idx][1], tris[b_idx][1]):
                        ref_hit.add(tris[a_idx][0])
                        ref_hit.add(tris[b_idx][0])

            res = _uv_overlap(tris)
            self.assertEqual(res, ref_hit)

    def test_bool01_props(self):
        """Property tests for _bool01 accepting boolean representations."""
        # Positive cases (evaluates to True)
        true_inputs = ["1", "true", "True"]
        for val in true_inputs:
            res = _bool01(val)
            self.assertIs(res, True)
            self.assertIsInstance(res, bool)

        # Negative cases (evaluates to False)
        false_inputs = ["0", "false", "False"]
        for val in false_inputs:
            res = _bool01(val)
            self.assertIs(res, False)
            self.assertIsInstance(res, bool)

        # Seeded verification over randomly selected valid boolean/0/1 string representations
        for seed in range(10):
            rng = random.Random(seed)
            choice = rng.choice(true_inputs + false_inputs)
            expected = choice in true_inputs
            self.assertEqual(_bool01(choice), expected)

    @unittest.expectedFailure
    def test_bool01_int_and_bool_inputs_bug(self):
        """_bool01 currently fails for typed int/bool falsy inputs 0 and False because it uses 's not in (\"0\", \"false\", \"False\")'."""
        self.assertIs(_bool01(0), False)
        self.assertIs(_bool01(False), False)


if __name__ == "__main__":
    unittest.main()
