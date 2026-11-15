"""Unit tests for asset normalisation helper functions in blender_visual_qa/assets/normalize.py."""
import math
import unittest

from blender_visual_qa.assets import glb_core, normalize


class TestNormalizeHelpersUnit(unittest.TestCase):
    # ------------------------------------------------------------------- _div_round
    def test_div_round(self):
        # Round half away from zero (positive numbers)
        self.assertEqual(normalize._div_round(5, 2), 3)
        self.assertEqual(normalize._div_round(4, 2), 2)
        self.assertEqual(normalize._div_round(1, 2), 1)
        self.assertEqual(normalize._div_round(0, 2), 0)
        # Round half away from zero (negative numbers - negative case)
        self.assertEqual(normalize._div_round(-5, 2), -3)
        self.assertEqual(normalize._div_round(-4, 2), -2)
        self.assertEqual(normalize._div_round(-1, 2), -1)

    # ------------------------------------------------------------------- det3_q16
    def test_det3_q16(self):
        # Identity matrix: det = Q16^3 = 281474976710656
        q16_cube = normalize.Q16 ** 3
        self.assertEqual(normalize.det3_q16(normalize.IDENTITY_Q16), q16_cube)

        # Scaled matrix (sx=2, sy=3, sz=4)
        m_scaled = (
            2 * normalize.Q16, 0, 0, 0,
            0, 3 * normalize.Q16, 0, 0,
            0, 0, 4 * normalize.Q16, 0,
            0, 0, 0, normalize.Q16
        )
        self.assertEqual(normalize.det3_q16(m_scaled), 24 * q16_cube)

        # Mirrored matrix with negative determinant (sx=-1, sy=1, sz=1) -> negative case
        m_mirrored = (
            -normalize.Q16, 0, 0, 0,
            0, normalize.Q16, 0, 0,
            0, 0, normalize.Q16, 0,
            0, 0, 0, normalize.Q16
        )
        self.assertEqual(normalize.det3_q16(m_mirrored), -q16_cube)

        # Specific non-diagonal matrix matching exact rational reference
        m_custom = (
            1, 2, 3, 0,
            0, 4, 5, 0,
            1, 0, 6, 0,
            0, 0, 0, 1
        )
        # Det = 1*(4*6 - 5*0) - 2*(0*6 - 5*1) + 3*(0*0 - 4*1) = 24 + 10 - 12 = 22
        self.assertEqual(normalize.det3_q16(m_custom), 22)

    # ------------------------------------------------------------------- _cofactor
    def test_cofactor(self):
        # Identity matrix cofactor
        q16_sq = normalize.Q16 ** 2
        cof_id = normalize._cofactor(normalize.IDENTITY_Q16)
        expected_id = [
            [q16_sq, 0, 0],
            [0, q16_sq, 0],
            [0, 0, q16_sq]
        ]
        self.assertEqual(cof_id, expected_id)

        # Non-diagonal matrix
        m_custom = (
            1, 2, 3, 0,
            0, 4, 5, 0,
            1, 0, 6, 0,
            0, 0, 0, 1
        )
        cof_custom = normalize._cofactor(m_custom)
        expected_custom = [
            [24, 5, -4],
            [-12, 3, 2],
            [-2, -5, 4]
        ]
        self.assertEqual(cof_custom, expected_custom)

        # Singular / linearly dependent matrix (negative case: singular 3x3)
        m_singular = (
            1, 2, 3, 0,
            2, 4, 6, 0,
            1, 0, 6, 0,
            0, 0, 0, 1
        )
        cof_sing = normalize._cofactor(m_singular)
        expected_sing = [
            [24, -6, -4],
            [-12, 3, 2],
            [0, 0, 0]
        ]
        self.assertEqual(cof_sing, expected_sing)

    # ------------------------------------------------------------------- bake_normal
    def test_bake_normal(self):
        q16_sq = normalize.Q16 ** 2
        cof_id = [[q16_sq, 0, 0], [0, q16_sq, 0], [0, 0, q16_sq]]

        # Unit normal along Y
        n_y = (0, normalize.Q16, 0)
        baked_y = normalize.bake_normal(cof_id, n_y)
        self.assertEqual(baked_y, (0, normalize.Q16, 0))

        # Arbitrary directional normal: keeps unit length within 1 q16 step
        n_diag = (38836, 38836, 38836)  # Approx unit normal in Q16
        baked_diag = normalize.bake_normal(cof_id, n_diag)
        length = math.isqrt(sum(c * c for c in baked_diag))
        self.assertLessEqual(abs(length - normalize.Q16), 1)

        # Mirrored matrix: cofactor flips X axis
        cof_mirror = [[-q16_sq, 0, 0], [0, q16_sq, 0], [0, 0, q16_sq]]
        n_x = (normalize.Q16, 0, 0)
        baked_mirror = normalize.bake_normal(cof_mirror, n_x)
        self.assertEqual(baked_mirror, (-normalize.Q16, 0, 0))

        # Zero normal (negative case)
        baked_zero = normalize.bake_normal(cof_id, (0, 0, 0))
        self.assertEqual(baked_zero, (0, 0, 0))

    # ------------------------------------------------------------------- is_helper
    def test_is_helper(self):
        def make_prim(name):
            return glb_core.Prim(
                name=name,
                node_path=name,
                world_q16=normalize.IDENTITY_Q16,
                positions_um=[(0, 0, 0)],
                indices=[0, 0, 0]
            )

        # Documented helper suffixes
        self.assertTrue(normalize.is_helper(make_prim("mesh_collision")))
        self.assertTrue(normalize.is_helper(make_prim("box-col")))
        self.assertTrue(normalize.is_helper(make_prim("hull-convcol")))

        # Non-helper names (negative cases)
        self.assertFalse(normalize.is_helper(make_prim("mesh")))
        self.assertFalse(normalize.is_helper(make_prim("collision_mesh")))
        self.assertFalse(normalize.is_helper(make_prim("box-col-2")))

    # ------------------------------------------------------------------- visual_bounds
    def test_visual_bounds(self):
        p_visual = glb_core.Prim(
            name="main_mesh",
            node_path="main_mesh",
            world_q16=normalize.IDENTITY_Q16,
            positions_um=[(0, 0, 0), (1000000, 2000000, 3000000)],
            indices=[0, 0, 0]
        )
        p_helper = glb_core.Prim(
            name="box_collision",
            node_path="box_collision",
            world_q16=normalize.IDENTITY_Q16,
            positions_um=[(-5000000, -5000000, -5000000), (5000000, 5000000, 5000000)],
            indices=[0, 0, 0]
        )

        # Ignores helper prims when visual prims are present
        bounds = normalize.visual_bounds([p_visual, p_helper])
        self.assertEqual(bounds, ((0, 1000000), (0, 2000000), (0, 3000000)))

        # Fallback to helper prims when ONLY helpers exist
        bounds_helper_only = normalize.visual_bounds([p_helper])
        self.assertEqual(bounds_helper_only, ((-5000000, 5000000), (-5000000, 5000000), (-5000000, 5000000)))

        # Empty prims or prims without positions (negative case)
        p_empty = glb_core.Prim(
            name="empty",
            node_path="empty",
            world_q16=normalize.IDENTITY_Q16,
            positions_um=[],
            indices=[]
        )
        self.assertIsNone(normalize.visual_bounds([]))
        self.assertIsNone(normalize.visual_bounds([p_empty]))

    # ------------------------------------------------------------------- scale_ppm
    def test_scale_ppm(self):
        bounds = ((0, 1000000), (0, 2000000), (0, 3000000))  # height = 2,000,000 um

        # Target height is None -> returns unit_scale_ppm
        self.assertEqual(normalize.scale_ppm(bounds, None, 1000000), 1000000)

        # Target height 1,000,000 um -> scale_ppm = _div_round(1000000 * 1000000, 2000000) = 500,000
        self.assertEqual(normalize.scale_ppm(bounds, 1000000, 1000000), 500000)

        # Zero or negative height (negative case) -> raises NormalizeError with RC_INCOMPLETE and "E_FLAT"
        flat_bounds = ((0, 1000000), (500, 500), (0, 3000000))
        with self.assertRaises(normalize.NormalizeError) as cm:
            normalize.scale_ppm(flat_bounds, 1000000, 1000000)
        self.assertEqual(cm.exception.rc, normalize.RC_INCOMPLETE)
        self.assertEqual(cm.exception.code, "E_FLAT")

    # ------------------------------------------------------------------- check_post
    def test_check_post(self):
        p_src = glb_core.Prim(
            name="mesh",
            node_path="mesh",
            world_q16=normalize.IDENTITY_Q16,
            positions_um=[(-500000, 0, -500000), (500000, 1000000, 500000)],
            indices=[0, 1, 0]
        )
        scene_src = glb_core.Scene(prims=[p_src])

        # Valid post scene: centered X/Z (centre=0), grounded Y (pivot_y=0), frozen matrix, same tris
        p_valid = glb_core.Prim(
            name="mesh",
            node_path="mesh",
            world_q16=normalize.IDENTITY_Q16,
            positions_um=[(-500000, 0, -500000), (500000, 1000000, 500000)],
            indices=[0, 1, 0]
        )
        scene_valid = glb_core.Scene(prims=[p_valid])
        self.assertEqual(normalize.check_post(scene_src, scene_valid, normalize.PPM), [])

        # Failed checks (negative case): off-pivot Y, off-center XZ, unfrozen transform, tri count mismatch
        p_bad = glb_core.Prim(
            name="mesh",
            node_path="mesh",
            world_q16=(normalize.Q16, 0, 0, 0, 0, normalize.Q16, 0, 0, 0, 0, normalize.Q16, 0, 0, 0, 0, 1),
            positions_um=[(1000000, 1000000, 1000000), (2000000, 2000000, 2000000)],
            indices=[0, 1, 0, 0, 1, 0]
        )
        scene_bad = glb_core.Scene(prims=[p_bad])
        failures = normalize.check_post(scene_src, scene_bad, normalize.PPM)
        self.assertIn("pivot_y", failures)
        self.assertIn("centre_xz", failures)
        self.assertIn("transforms_not_frozen", failures)
        self.assertIn("tri_count", failures)
        self.assertEqual(set(failures), {"pivot_y", "centre_xz", "transforms_not_frozen", "tri_count"})


if __name__ == "__main__":
    unittest.main()
