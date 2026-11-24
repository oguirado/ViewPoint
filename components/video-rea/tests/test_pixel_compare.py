# SPDX-License-Identifier: AGPL-3.0-only
"""Public synthetic controls for the RGB8 checkpoint comparator."""

from fractions import Fraction
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


# Load the adjacent component by path so discovery also works with python -I.
spec = spec_from_file_location("pixel_compare", Path(__file__).resolve().parents[1] / "pixel_compare.py")
pixel_compare = module_from_spec(spec)
spec.loader.exec_module(pixel_compare)
RGB8Frame = pixel_compare.RGB8Frame
Region = pixel_compare.Region
compare_checkpoints = pixel_compare.compare_checkpoints


class PixelComparisonTests(unittest.TestCase):
    @staticmethod
    def scene(sprite_x=5, *, watermark=False):
        background = bytes((9, 17, 33))
        sprite = bytes((190, 43, 11))
        mark = bytes((220, 220, 220))
        rows = []
        for y in range(16):
            row = [background] * 16
            if y in (6, 7):
                row[sprite_x:sprite_x + 2] = [sprite, sprite]
            if watermark and y == 5:
                row[10] = mark
            rows.append(b"".join(row))
        return RGB8Frame(16, 16, b"".join(rows))

    def test_matching_foreground_passes(self):
        report = compare_checkpoints(self.scene(), self.scene(), Region(4, 5, 6, 4),
                                     max_mismatch_ppm=0)
        self.assertEqual(report.verdict, "PASS")
        self.assertEqual(report.compared_pixels, 24)
        self.assertEqual(report.mismatched_pixels, 0)
        self.assertEqual(report.mismatch_fraction, Fraction(0))

    def test_displaced_sprite_fails_on_unchanged_background(self):
        report = compare_checkpoints(self.scene(), self.scene(sprite_x=7), Region(4, 5, 6, 4),
                                     max_mismatch_ppm=0)
        self.assertEqual(report.verdict, "FAIL")
        self.assertEqual(report.compared_pixels, 24)
        self.assertEqual(report.mismatched_pixels, 8)
        self.assertEqual(report.mismatch_fraction, Fraction(1, 3))

    def test_watermark_outside_region_has_no_effect(self):
        reference = self.scene()
        candidate = self.scene(watermark=True)
        region = Region(4, 5, 6, 4)
        self.assertEqual(
            compare_checkpoints(reference, reference, region, max_mismatch_ppm=0),
            compare_checkpoints(reference, candidate, region, max_mismatch_ppm=0),
        )
        covering_mark = compare_checkpoints(reference, candidate, Region(10, 5, 1, 1),
                                            max_mismatch_ppm=0)
        self.assertEqual((covering_mark.verdict, covering_mark.mismatched_pixels), ("FAIL", 1))

    def test_channel_delta_is_inclusive_and_checks_each_channel(self):
        reference = RGB8Frame(1, 1, bytes((100, 100, 100)))
        region = Region(0, 0, 1, 1)
        for rgb in ((110, 100, 100), (100, 90, 100), (100, 100, 110), (110, 90, 110)):
            with self.subTest(rgb=rgb):
                candidate = RGB8Frame(1, 1, bytes(rgb))
                at_limit = compare_checkpoints(reference, candidate, region,
                                               max_mismatch_ppm=0, max_channel_delta=10)
                below_limit = compare_checkpoints(reference, candidate, region,
                                                  max_mismatch_ppm=0, max_channel_delta=9)
                self.assertEqual((at_limit.verdict, at_limit.mismatched_pixels), ("PASS", 0))
                self.assertEqual((below_limit.verdict, below_limit.mismatched_pixels), ("FAIL", 1))

    def test_ppm_uses_exact_inclusive_cutoff(self):
        reference = RGB8Frame(3, 1, b"\x00" * 9)
        candidate = RGB8Frame(3, 1, b"\x00" * 8 + b"\xff")
        for width, limit, verdict in ((3, 333_333, "FAIL"), (3, 333_334, "PASS"),
                                      (2, 499_999, "FAIL"), (2, 500_000, "PASS"),
                                      (1, 1_000_000, "PASS")):
            with self.subTest(width=width, limit=limit):
                region = Region(3 - width, 0, width, 1)
                report = compare_checkpoints(reference, candidate, region, max_mismatch_ppm=limit)
                self.assertEqual(report.verdict, verdict)
                self.assertEqual(report.mismatch_fraction, Fraction(1, width))

    def test_rejects_mismatched_geometry_with_equal_payload_length(self):
        reference = RGB8Frame(2, 1, bytes(6))
        candidate = RGB8Frame(1, 2, bytes(6))
        with self.assertRaisesRegex(ValueError, "dimensions must match"):
            compare_checkpoints(reference, candidate, Region(0, 0, 1, 1), max_mismatch_ppm=0)

    def test_rejects_invalid_frame(self):
        for width, height, pixels in ((1, 1, bytes(2)), (1, 1, bytes(4)), (1, 1, "rgb"),
                                      (0, 1, b""), (1, -1, b""), (True, 1, bytes(3)),
                                      (1, 1.0, bytes(3))):
            with self.subTest(width=width, height=height, pixels=pixels), self.assertRaises(ValueError):
                RGB8Frame(width, height, pixels)

    def test_rejects_invalid_region(self):
        for rectangle in ((0, 0, 0, 1), (0, 0, 1, 0), (0, 0, -1, 1),
                          (-1, 0, 1, 1), (0, -1, 1, 1), (0.0, 0, 1, 1), (0, True, 1, 1)):
            with self.subTest(rectangle=rectangle), self.assertRaises(ValueError):
                Region(*rectangle)

        frame = self.scene()
        for region in (Region(15, 0, 2, 1), Region(0, 15, 1, 2)):
            with self.subTest(region=region), self.assertRaisesRegex(ValueError, "fit within"):
                compare_checkpoints(frame, frame, region, max_mismatch_ppm=0)
        with self.assertRaisesRegex(ValueError, "Region is required"):
            compare_checkpoints(frame, frame, None, max_mismatch_ppm=0)

    def test_rejects_invalid_thresholds(self):
        frame = self.scene()
        region = Region(0, 0, 1, 1)
        for limit in (-1, 1_000_001, 0.0, True, "0", None):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "max_mismatch_ppm"):
                compare_checkpoints(frame, frame, region, max_mismatch_ppm=limit)
        for delta in (-1, 256, 0.0, True, "0", None):
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, "max_channel_delta"):
                compare_checkpoints(frame, frame, region, max_mismatch_ppm=0, max_channel_delta=delta)


if __name__ == "__main__":
    unittest.main()
