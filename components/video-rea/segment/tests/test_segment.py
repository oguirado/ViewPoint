# SPDX-License-Identifier: AGPL-3.0-only
import unittest

import numpy as np

from segment.geometry import Detection, area, detect, overlap, visible_boxes
from segment.ocr import normalize
from segment.temporal import compare


class SegmentTests(unittest.TestCase):
    def test_separates_small_widget_and_overlay(self):
        rgb = np.full((360, 640, 3), (230, 238, 243), np.uint8)
        rgb[50:80, 24:62] = (32, 60, 91)
        rgb[50:190, 400:615] = (32, 60, 91)
        rgb[110:147, 410:605] = (44, 89, 119)
        controls = detect(rgb)
        self.assertIn(('menu', (24, 50, 38, 30), True),
                      [(d.kind, d.bbox, d.small) for d in controls])
        self.assertIn('menu_item', [d.kind for d in controls])

    def test_boundary_and_caret_only(self):
        before = np.full((360, 640, 3), (230, 238, 243), np.uint8)
        caret = before.copy()
        caret[34:52, 19:20] = 5
        after = before.copy()
        after[70:250, 120:530] = (21, 87, 119)
        self.assertFalse(compare(before, caret, detect(before), detect(caret))['boundary'])
        self.assertTrue(compare(before, after, detect(before), detect(after))['boundary'])

    def test_caret_does_not_erase_other_tile_evidence(self):
        before = np.zeros((48, 64, 3), np.uint8)
        after = before.copy()
        after[2:15, 1:2] = 255
        caret = compare(before, after, [], [])
        after[5:12, 8:14] = 255
        both = compare(before, after, [], [])
        self.assertFalse(both["boundary"])
        self.assertEqual(caret["dirty_tiles"], 1)
        self.assertGreater(both["changed_pixels"], caret["changed_pixels"])

    def test_small_threshold_strict_and_occlusion_fragments_disjoint(self):
        self.assertFalse(Detection("button", (0, 0, 44, 24), "foreground").small)
        self.assertTrue(Detection("button", (0, 0, 43, 24), "foreground").small)
        self.assertTrue(Detection("button", (0, 0, 44, 23), "foreground").small)
        dialog = (20, 20, 10, 10)
        fragments = visible_boxes((10, 10, 30, 30), dialog)
        self.assertEqual(sum(map(area, fragments)), 800)
        self.assertTrue(all(not area(overlap(region, dialog)) for region in fragments))

    def test_dialog_geometry_and_dimmed_parent(self):
        backdrop = np.full((360, 640, 3), (230, 238, 242), np.uint8)
        backdrop[50:80, 24:62] = (32, 60, 90)
        rgb = backdrop // 2
        rgb[100:250, 120:390] = (179, 153, 136)
        rgb[104:246, 124:386] = (246, 249, 250)
        rgb[161:190, 151:351] = (140, 171, 194)
        rgb[163:188, 153:349] = (253, 253, 253)
        detected = {(r.kind, r.layer) for r in detect(rgb, backdrop, detect(backdrop))}
        self.assertEqual(detected, {("button", "parent"), ("dialog", "foreground"),
                                    ("text_field", "foreground")})

    def test_normalization_preserves_case(self):
        self.assertEqual(normalize('  Cafe\u0301\tProjects\n'), 'Café Projects')
