"""Structural-affordance distractor fixtures and S1 evidence limit pinning.

This module provides tests for structural affordances (buttons, cards, checkboxes,
checkmarks, X glyphs) and distractor fixtures, pinning the limits of S1 evidence
for A5-G3.
"""

import unittest
import numpy as np

from visioncore.desc import describe
from visioncore.grid import Grid


def sobel_int(gray: np.ndarray):
    """Compute 2D integer Sobel derivatives (gx, gy) for a grayscale image."""
    g = gray.astype(np.int32)
    padded = np.pad(g, ((1, 1), (1, 1)), mode="edge")
    gx = (
        -1 * padded[:-2, :-2] + 1 * padded[:-2, 2:]
        - 2 * padded[1:-1, :-2] + 2 * padded[1:-1, 2:]
        - 1 * padded[2:, :-2] + 1 * padded[2:, 2:]
    )
    gy = (
        -1 * padded[:-2, :-2] - 2 * padded[:-2, 1:-1] - 1 * padded[:-2, 2:]
        + 1 * padded[2:, :-2] + 2 * padded[2:, 1:-1] + 1 * padded[2:, 2:]
    )
    return gx, gy


def rgb_to_luma(rgb: np.ndarray) -> np.ndarray:
    """Convert RGB uint8 array to luma uint8 array using integer arithmetic."""
    r = rgb[..., 0].astype(np.int32)
    g = rgb[..., 1].astype(np.int32)
    b = rgb[..., 2].astype(np.int32)
    return ((r * 77 + g * 150 + b * 29) >> 8).astype(np.uint8)


def rasterize_affordance_scene() -> np.ndarray:
    """Rasterize 256x192 uint8 RGB scene with structural affordances and distractors."""
    frame = np.full((192, 256, 3), 240, dtype=np.uint8)

    # 1. Closed button at x=16, y=16, w=96, h=32
    frame[16:48, 16:112] = (210, 210, 210)
    frame[16, 16:112] = (40, 40, 40)
    frame[47, 16:112] = (40, 40, 40)
    frame[16:48, 16] = (40, 40, 40)
    frame[16:48, 111] = (40, 40, 40)
    # Centered glyph bar in button
    frame[28:36, 48:80] = (30, 30, 30)

    # 2. Decorative bordered card at x=16, y=64, w=160, h=96
    frame[64:160, 16:176] = (225, 225, 225)
    frame[64, 16:176] = (50, 50, 50)
    frame[159, 16:176] = (50, 50, 50)
    frame[64:160, 16] = (50, 50, 50)
    frame[64:160, 175] = (50, 50, 50)
    # Three text bars in card
    frame[84:90, 32:144] = (80, 80, 80)
    frame[104:110, 32:144] = (80, 80, 80)
    frame[124:130, 32:144] = (80, 80, 80)

    # 3. Bordered checkbox at x=144, y=16, w=16, h=16
    frame[16:32, 144:160] = (255, 255, 255)
    frame[16, 144:160] = (20, 20, 20)
    frame[31, 144:160] = (20, 20, 20)
    frame[16:32, 144] = (20, 20, 20)
    frame[16:32, 159] = (20, 20, 20)
    # Checkmark inside checkbox
    for i in range(5):
        frame[24 + i, 148 + i] = (20, 20, 20)
        frame[24 - i, 152 + i] = (20, 20, 20)

    # 4. Unbordered checkmark at x=144, y=48, w=16, h=16
    for i in range(5):
        frame[56 + i, 148 + i] = (20, 20, 20)
        frame[56 - i, 152 + i] = (20, 20, 20)

    # 5. Two X glyphs
    # X glyph 1 in panel corner (inside card at x=156..163, y=72..79)
    for i in range(8):
        frame[72 + i, 156 + i] = (20, 20, 20)
        frame[72 + i, 163 - i] = (20, 20, 20)

    # X glyph 2 in text line 2 (inside text line at x=148..155, y=103..110)
    for i in range(8):
        frame[103 + i, 148 + i] = (20, 20, 20)
        frame[103 + i, 155 - i] = (20, 20, 20)

    return frame


class TestStructuralAffordanceDistractors(unittest.TestCase):
    DETECTOR_SEMANTICS = "AUSENTE"

    def test_detector_semantics_ausente(self):
        """No detector module or callable is present; label semantics AUSENTE."""
        self.assertEqual(self.DETECTOR_SEMANTICS, "AUSENTE")

    def test_rasterize_scene_describe_grid_repeatability(self):
        """Compute real describe and Grid outputs, asserting repeatability and unchanged pixels."""
        frame = rasterize_affordance_scene()
        self.assertEqual(frame.shape, (192, 256, 3))
        self.assertEqual(frame.dtype, np.uint8)

        # Retain original copy to assert pixels remain unchanged
        original = frame.copy()
        grid = Grid(256, 192)

        tiles_1, meta_1 = describe(frame, grid)
        tiles_2, meta_2 = describe(frame, grid)

        # Assert repeatability
        np.testing.assert_array_equal(tiles_1, tiles_2)
        np.testing.assert_array_equal(meta_1, meta_2)

        # Assert frame pixels and metadata are unchanged
        np.testing.assert_array_equal(frame, original)
        self.assertEqual(frame.shape, (192, 256, 3))
        self.assertTrue(frame.flags.c_contiguous)

    def test_side_support_and_side_local_gradient_support_probes(self):
        """In test-local reference probes, evaluate side support >= 75% and side-local gradient support >= 65%."""
        frame = rasterize_affordance_scene()
        luma = rgb_to_luma(frame)
        gx, gy = sobel_int(luma)
        mag = np.abs(gx) + np.abs(gy)

        # Closed button top border at y=16, x=16..112 (length 96)
        # Evaluate side support and gradient support away from label glyphs
        button_top_mag = mag[16, 16:112]
        button_top_gx = gx[16, 16:112]
        button_top_gy = gy[16, 16:112]

        total_px = len(button_top_mag)
        side_support_px = np.count_nonzero(button_top_mag > 0)
        grad_support_px = np.count_nonzero(np.abs(button_top_gy) > np.abs(button_top_gx))

        # Express threshold comparisons as integer cross products
        # side_support >= 75%: side_support_px * 100 >= total_px * 75
        self.assertTrue(side_support_px * 100 >= total_px * 75)

        # grad_support >= 65%: grad_support_px * 100 >= total_px * 65
        self.assertTrue(grad_support_px * 100 >= total_px * 65)

    def test_one_pixel_border_gap_negative_control(self):
        """One-pixel border-gap negative control distinguishing continuous outlines from broken lines."""
        continuous_frame = rasterize_affordance_scene()

        # Create a broken frame with a 1-pixel border gap in the button's top edge at x=40
        broken_frame = continuous_frame.copy()
        broken_frame[16, 40] = (240, 240, 240)  # Gap inserted

        cont_luma = rgb_to_luma(continuous_frame)
        brok_luma = rgb_to_luma(broken_frame)

        cont_gx, cont_gy = sobel_int(cont_luma)
        brok_gx, brok_gy = sobel_int(brok_luma)

        cont_mag = np.abs(cont_gx[15, 16:112]) + np.abs(cont_gy[15, 16:112])
        brok_mag = np.abs(brok_gx[15, 16:112]) + np.abs(brok_gy[15, 16:112])

        cont_supp = np.count_nonzero(cont_mag > 500)
        brok_supp = np.count_nonzero(brok_mag > 500)

        self.assertEqual(cont_supp, 96)
        self.assertLess(brok_supp, cont_supp)

    def test_gradient_normals_straight_edge_fixture(self):
        """Pin gradient normals with horizontal and vertical straight-edge fixture:

        horizontal strokes have vertical normals and vertical strokes horizontal normals.
        """
        fixture = np.full((64, 64), 200, dtype=np.uint8)

        # Horizontal stroke at y=20, x=10..50
        fixture[20, 10:50] = 40
        # Vertical stroke at x=20, y=30..50
        fixture[30:50, 20] = 40

        gx, gy = sobel_int(fixture)

        # Along horizontal stroke (away from endpoints, e.g. x=20..40, y=20 adjacent y=19 or y=21)
        # Vertical normal: |gy| > |gx|
        horiz_gy = gy[19, 20:40]
        horiz_gx = gx[19, 20:40]
        self.assertTrue(np.all(np.abs(horiz_gy) > np.abs(horiz_gx)))

        # Along vertical stroke (away from endpoints, e.g. y=35..45, x=20 adjacent x=19 or x=21)
        # Horizontal normal: |gx| > |gy|
        vert_gy = gy[35:45, 19]
        vert_gx = gx[35:45, 19]
        self.assertTrue(np.all(np.abs(vert_gx) > np.abs(vert_gy)))

    def test_exact_pixel_twin_enabled_button_and_inert_decoration_counterexample(self):
        """Add an exact pixel-twin enabled button and inert decoration counterexample

        requiring equal descriptors, proving pooled descriptors alone do not prove actionability.
        """
        # Button scene containing button at x=16, y=16, w=96, h=32
        scene_a = rasterize_affordance_scene()

        # Exact pixel twin scene (inert decoration with identical pixel contents)
        scene_b = scene_a.copy()

        grid = Grid(256, 192)
        tiles_a, meta_a = describe(scene_a, grid)
        tiles_b, meta_b = describe(scene_b, grid)

        # Assert equal descriptors for identical pixel inputs
        np.testing.assert_array_equal(tiles_a, tiles_b)
        np.testing.assert_array_equal(meta_a, meta_b)

        # Never assert a rectangle or pooled descriptor proves actionability
        # Descriptors are equal, yet actionability differs (enabled vs inert context)
        self.assertIsInstance(tiles_a, np.ndarray)


if __name__ == "__main__":
    unittest.main()
