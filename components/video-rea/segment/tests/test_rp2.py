# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from segment.geometry import Detection, Surface, detect, local, visible_boxes
from segment.provider import element
from segment.temporal import compare


class BorderedPrecisionTests(unittest.TestCase):
    def test_parent_record_projects_onto_visible_pixels(self):
        box = (20, 54, 160, 32)
        visible = visible_boxes(box, (52, 30, 118, 100))
        ocr = Mock()
        ocr.read.return_value = None, "tessdata_absent"
        frame = {"provenance": {}}
        original = element(Detection("button", box, "parent"), frame, ocr, None)
        clipped = element(Detection("button", box, "parent", visible=visible), frame, ocr, None)
        self.assertEqual(clipped["record"]["bbox"], (20, 54, 32, 32))
        self.assertEqual(clipped["record"]["element_id"], original["record"]["element_id"])
        self.assertEqual(clipped["visible_regions"], visible)
        self.assertTrue(clipped["small"])
        self.assertEqual(original["record"]["bbox"], box)

    def test_compact_pointer_hole_cannot_turn_field_into_list(self):
        rgb = np.full((160, 240, 3), (150, 174, 180), np.uint8)
        field = Surface((18, 56, 204, 65), (238, 246, 232), True)
        pointer_hole = Surface((117, 87, 18, 17), (235, 245, 236), False)
        for surface in (field, pointer_hole):
            x, y, w, h = surface.box
            rgb[y:y+h, x:x+w] = surface.fill
        with patch("segment.geometry.proposals", return_value=[field, pointer_hole]):
            rows = local(rgb, (0, 0, 240, 160))
        self.assertEqual([(row.kind, row.bbox) for row in rows if row.kind != "text"],
                         [("text_field", field.box)])

    def test_glyph_proposal_remains_part_of_field_ink(self):
        rgb = np.full((180, 260, 3), (171, 201, 209), np.uint8)
        field = Surface((15, 51, 230, 45), (240, 244, 240), True)
        letter = Surface((41, 68, 8, 8), (43, 39, 52), False)
        rgb[51:96, 15:245] = field.fill
        rgb[67:77, 29:35] = letter.fill
        rgb[68:76, 41:49] = letter.fill
        rgb[67:77, 53:59] = letter.fill
        with patch("segment.geometry.proposals", return_value=[field, letter]):
            rows = local(rgb, (0, 0, 260, 180))
        self.assertEqual([(row.kind, row.bbox) for row in rows if row.kind != "text"],
                         [("text_field", field.box)])
        self.assertEqual(rows[0].ink, (29, 67, 30, 10))

    def test_compact_matte_island_needs_an_independent_edge(self):
        rgb = np.full((120, 180, 3), (155, 170, 181), np.uint8)
        fill = (240, 244, 246)
        rgb[35:67, 12:151] = fill
        rgb[35:67, 70:74] = (31, 40, 48)
        field = Surface((74, 35, 77, 32), fill, True)
        island = Surface((62, 44, 8, 8), fill, False)
        button = Surface((29, 83, 13, 12), (27, 51, 61), False)
        rgb[83:95, 29:42] = button.fill
        with patch("segment.geometry.proposals", return_value=[field, island, button]):
            rows = local(rgb, (0, 0, 180, 120))
        self.assertNotIn(island.box, [row.bbox for row in rows if row.kind != "text"])
        self.assertIn(button.box, [row.bbox for row in rows if row.kind == "button"])

    def test_modal_fit_rejects_rim_that_spills_into_dimmed_backdrop(self):
        backdrop = np.full((180, 260, 3), (210, 198, 184), np.uint8)
        backdrop[49:145, 158:173] = (152, 170, 190)
        image = (backdrop.astype(np.uint16) * 60 // 100).astype(np.uint8)
        image[49:145, 52:158] = (235, 220, 213)
        dialog = Surface((52, 49, 121, 96), (235, 220, 213), True)
        with patch("segment.geometry.proposals", return_value=[dialog]):
            rows = detect(image, backdrop)
        self.assertEqual([row.bbox for row in rows if row.kind == "dialog"], [(52, 49, 106, 96)])

    def test_moving_pointer_over_ink_is_not_a_boundary(self):
        base = np.full((110, 190, 3), (45, 55, 65), np.uint8)
        for x in range(54, 129, 12):
            cv2.rectangle(base, (x, 48), (x + 5, 62), (213, 218, 214), 2)

        def cursor(x, y, ring=False):
            image = base.copy()
            if ring:
                halo = np.zeros(image.shape[:2], np.uint8)
                cv2.circle(halo, (x, y), 10, 255, -1)
                image[halo != 0] = cv2.add(image, (45, 30, 20, 0))[halo != 0]
                cv2.circle(image, (x, y), 9, (255, 193, 96), 2)
            polygon = np.array(((x, y), (x+1, y+14), (x+5, y+10), (x+9, y+10)))
            cv2.fillConvexPoly(image, polygon, (251, 250, 243))
            cv2.polylines(image, [polygon], True, (12, 21, 30), 1)
            return image

        before, after = cursor(72, 47, True), cursor(52, 56)
        label = Detection("text", (50, 41, 80, 27), "foreground", (50, 41, 80, 27))
        self.assertFalse(compare(before, after, [label], [label])["boundary"])
        after[48:61, 107:131] = (251, 250, 243)
        self.assertTrue(compare(before, after, [label], [label])["boundary"])


if __name__ == "__main__":
    unittest.main()
