"""Unit tests for private mesh healing helpers in blender_visual_qa/assets/heal.py."""
import math
import unittest

from blender_visual_qa.assets import glb_core, heal


class TestHealHelpersUnit(unittest.TestCase):
    # ------------------------------------------------------------------ _find
    def test_find_positive(self):
        parent = [0, 0, 1, 2]  # 3 -> 2 -> 1 -> 0
        root = heal._find(parent, 3)
        self.assertEqual(root, 0)
        self.assertEqual(parent, [0, 0, 1, 1])

    def test_find_negative(self):
        parent = [0, 1, 2]
        with self.assertRaises(IndexError):
            heal._find(parent, 5)

    # ------------------------------------------------------------------ _is_sliver
    def test_is_sliver_positive(self):
        # Degenerate face with duplicate vertex index in triangle tuple
        pos_deg = [(0, 0, 0), (10, 0, 0), (20, 0, 0)]
        self.assertTrue(heal._is_sliver(pos_deg, (0, 0, 1), 10))

        # Collinear vertices (area 0, altitude 0)
        pos_col = [(0, 0, 0), (50, 0, 0), (100, 0, 0)]
        self.assertTrue(heal._is_sliver(pos_col, (0, 1, 2), 10))

        # Near-collinear sliver (altitude <= tol)
        # Edge 0-1 length 100 along x, point 2 at (50, 1, 0) -> altitude = 1 <= tol=10
        pos_sliver = [(0, 0, 0), (100, 0, 0), (50, 1, 0)]
        self.assertTrue(heal._is_sliver(pos_sliver, (0, 1, 2), 10))

    def test_is_sliver_negative(self):
        # Non-sliver triangle (altitude > tol)
        # Edge 0-1 length 100 along x, point 2 at (50, 50, 0) -> altitude = 50 > tol=10
        pos_normal = [(0, 0, 0), (100, 0, 0), (50, 50, 0)]
        self.assertFalse(heal._is_sliver(pos_normal, (0, 1, 2), 10))

    # ------------------------------------------------------------------ _components
    def test_components_positive(self):
        # Two disconnected components: component 1 (faces 0, 1), component 2 (face 2)
        tris = [(0, 1, 2), (2, 1, 3), (4, 5, 6)]
        comps = heal._components(tris)
        self.assertEqual(comps, [[0, 1], [2]])

    def test_components_negative(self):
        # Empty triangle list -> empty components list
        self.assertEqual(heal._components([]), [])

    # ------------------------------------------------------------------ _nonzero
    def test_nonzero_positive(self):
        # Non-zero area triangle
        pos = [(0, 0, 0), (100, 0, 0), (0, 100, 0)]
        self.assertTrue(heal._nonzero(pos, (0, 1, 2)))

    def test_nonzero_negative(self):
        # Zero-area collinear triangle
        pos_collinear = [(0, 0, 0), (50, 0, 0), (100, 0, 0)]
        self.assertFalse(heal._nonzero(pos_collinear, (0, 1, 2)))

    # ------------------------------------------------------------------ map_vertices
    def test_map_vertices_positive(self):
        orig = [(0, 0, 0), (100, 0, 0), (200, 0, 0)]
        new = [(2, 0, 0), (300, 0, 0)]
        used = {0, 1}
        # tol = 10; orig[0] (0,0,0) is within 10 of new[0] (2,0,0) -> mapped to index 0
        # orig[1] (100,0,0) is not within 10 of any used new vertex -> mapped to None
        # orig[2] (200,0,0) is not within 10 -> mapped to None
        vmap = heal.map_vertices(orig, new, used, tol=10)
        self.assertEqual(vmap, [0, None, None])

    def test_map_vertices_negative(self):
        orig = [(1000, 1000, 1000)]
        new = [(0, 0, 0)]
        used = {0}
        vmap = heal.map_vertices(orig, new, used, tol=10)
        self.assertEqual(vmap, [None])

    # ------------------------------------------------------------------ uv_lookup
    def test_uv_lookup_positive(self):
        prim = glb_core.Prim(
            name="p",
            node_path="/p",
            world_q16=glb_core.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            indices=[0, 1, 2],
            uvs_q16=[(0, 0), (1000, 0), (0, 1000)],
        )
        vmap = [0, 1, 2]
        res = heal.uv_lookup(prim, vmap)
        expected = {frozenset({0, 1, 2}): {0: (0, 0), 1: (1000, 0), 2: (0, 1000)}}
        self.assertEqual(res, expected)

    def test_uv_lookup_negative(self):
        # When vmap maps a vertex to None or causes duplicate vertices, uv_lookup ignores the face
        prim = glb_core.Prim(
            name="p",
            node_path="/p",
            world_q16=glb_core.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            indices=[0, 1, 2],
            uvs_q16=[(0, 0), (1000, 0), (0, 1000)],
        )
        vmap_with_none = [0, None, 2]
        res = heal.uv_lookup(prim, vmap_with_none)
        self.assertEqual(res, {})

    # ------------------------------------------------------------------ _atlas_uv
    def test_atlas_uv_positive(self):
        # slot 0 in 2x2 grid (n=2), u0=0
        # size = Q16 // 2 = 32768, m = min(256, 4096) = 256
        # cx=0, cy=0 -> x0=256, y0=256, s=32256
        # vertices: ((256, 256), (32512, 256), (256, 32512))
        res = heal._atlas_uv(0, 2, 0)
        self.assertEqual(res, ((256, 256), (32512, 256), (256, 32512)))

    def test_atlas_uv_negative(self):
        # Division by zero if grid dimension n is 0
        with self.assertRaises(ZeroDivisionError):
            heal._atlas_uv(0, 0, 0)

    # ------------------------------------------------------------------ _isqrt_ceil
    def test_isqrt_ceil_positive_range(self):
        for n in range(1, 10001):
            expected = math.ceil(math.sqrt(n))
            actual = heal._isqrt_ceil(n)
            self.assertEqual(actual, expected, f"Failed for n={n}")

    @unittest.expectedFailure
    def test_isqrt_ceil_zero_bug(self):
        # math.ceil(sqrt(0)) == 0, but _isqrt_ceil(0) returns 1 due to r initialized to 1
        self.assertEqual(heal._isqrt_ceil(0), 0)

    @unittest.expectedFailure
    def test_isqrt_ceil_refuses_negatives_bug(self):
        # _isqrt_ceil(-1) returns 1 instead of raising ValueError or refusing negative input
        with self.assertRaises((ValueError, ArithmeticError)):
            heal._isqrt_ceil(-1)


if __name__ == "__main__":
    unittest.main()
