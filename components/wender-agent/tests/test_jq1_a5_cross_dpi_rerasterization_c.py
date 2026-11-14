# @module {"id":"wa.tests.test_jq1_a5_cross_dpi_rerasterization_c","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Adversarial Test C for fractional-DPR fusion drift without assuming exact area equality."""
import unittest

from wender.perceive.vision_adapter import fuse_regions
from wender.state import schema


def _round_nearest(coord, dpr_ppm):
    """Integer nearest rounding: floor((coord * dpr_ppm + 500000) / 1000000)."""
    return (coord * dpr_ppm + 500000) // 1000000


def _rerasterize_box(box, dpr_ppm):
    """Rerasterize box endpoints separately and derive physical width and height."""
    x, y, w, h = box
    x0_p = _round_nearest(x, dpr_ppm)
    y0_p = _round_nearest(y, dpr_ppm)
    x1_p = _round_nearest(x + w, dpr_ppm)
    y1_p = _round_nearest(y + h, dpr_ppm)
    return {
        "x": x0_p,
        "y": y0_p,
        "w": x1_p - x0_p,
        "h": y1_p - y0_p,
        "tiles": 1,
    }


# @anchor:wa.tests.test_jq1_a5_cross_dpi_rerasterization_c.TestCrossDpiRerasterizationC
class TestCrossDpiRerasterizationC(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            {"id": "0000000000000001", "role": "button", "name": "Submit", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000002", "role": "textbox", "name": "Input", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000003", "role": "checkbox", "name": "Agree", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        self.css_boxes = {
            "0000000000000001": [11, 13, 47, 23],
            "0000000000000002": [75, 13, 121, 23],
            "0000000000000003": [11, 51, 15, 15],
        }

    def test_cross_dpi_rerasterization_controls(self):
        dpr_scales = [1000000, 1250000, 2000000]
        results = {}

        for dpr in dpr_scales:
            state = schema.build_state(
                self.nodes,
                kind="cdp",
                url="https://example.test",
                title="Cross DPI Test",
                ax_sha256="a" * 64,
                ax_stable_sha256="b" * 64,
                prune={"name": "prune_ax_v1", "version": 1},
                raw_count=3,
                boxes=self.css_boxes,
                viewport={
                    "w": 800,
                    "h": 600,
                    "scroll_x": 0,
                    "scroll_y": 0,
                    "doc_w": 800,
                    "doc_h": 600,
                    "dpr_ppm": dpr,
                },
                actionable_roles={"button", "textbox", "checkbox"},
            )

            regions = [
                _rerasterize_box(self.css_boxes["0000000000000001"], dpr),
                _rerasterize_box(self.css_boxes["0000000000000002"], dpr),
                _rerasterize_box(self.css_boxes["0000000000000003"], dpr),
            ]

            fused = fuse_regions(state, regions, dpr_ppm=dpr)
            results[dpr] = fused

            self.assertEqual(fused["unmatched_regions"], [])
            hit_nodes = [h["node"] for h in fused["hits"]]
            self.assertEqual(
                hit_nodes,
                ["0000000000000001", "0000000000000002", "0000000000000003"],
            )

            # Independent endpoint-normalization reference to bound CSS edge drift by <= 1 pixel
            for reg_idx, node_id in enumerate(
                ["0000000000000001", "0000000000000002", "0000000000000003"]
            ):
                reg = regions[reg_idx]
                box = self.css_boxes[node_id]

                # Endpoint-normalized CSS bounds from physical endpoints
                norm_rx0 = (reg["x"] * 1000000) // dpr
                norm_rx1 = ((reg["x"] + reg["w"]) * 1000000) // dpr
                norm_ry0 = (reg["y"] * 1000000) // dpr
                norm_ry1 = ((reg["y"] + reg["h"]) * 1000000) // dpr

                css_x0, css_y0 = box[0], box[1]
                css_x1, css_y1 = box[0] + box[2], box[1] + box[3]

                self.assertLessEqual(abs(norm_rx0 - css_x0), 1)
                self.assertLessEqual(abs(norm_rx1 - css_x1), 1)
                self.assertLessEqual(abs(norm_ry0 - css_y0), 1)
                self.assertLessEqual(abs(norm_ry1 - css_y1), 1)

        # Require identical hit identifiers and canonical ordering across all scales
        for dpr in [1250000, 2000000]:
            self.assertEqual(
                [h["node"] for h in results[dpr]["hits"]],
                [h["node"] for h in results[1000000]["hits"]],
            )
            self.assertEqual(
                [h["region"] for h in results[dpr]["hits"]],
                [h["region"] for h in results[1000000]["hits"]],
            )

    def test_a5_d3_regression_collapsing_width_recovery(self):
        # A5-D3 regression using physical region x 16, y 0, w 1, h 5 at dpr_ppm 1250000
        # against a one-pixel CSS node x 12, y 0, w 1, h 4.
        node = [
            {"id": "0000000000000004", "role": "button", "name": "Tiny", "depth": 0, "ordinal": 0, "attrs": ()}
        ]
        boxes = {"0000000000000004": [12, 0, 1, 4]}
        dpr = 1250000

        state = schema.build_state(
            node,
            kind="cdp",
            url="https://example.test",
            title="A5-D3 Test",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=1,
            boxes=boxes,
            viewport={
                "w": 800,
                "h": 600,
                "scroll_x": 0,
                "scroll_y": 0,
                "doc_w": 800,
                "doc_h": 600,
                "dpr_ppm": dpr,
            },
            actionable_roles={"button"},
        )

        physical_region = [{"x": 16, "y": 0, "w": 1, "h": 5, "tiles": 1}]

        # Endpoint normalization reference arithmetic check (passing assertions)
        # Endpoint floors map physical endpoints 16 and 17 to CSS 12 and 13 and recover overlap 4.
        norm_x0 = (16 * 1000000) // dpr
        norm_x1 = (17 * 1000000) // dpr
        norm_y0 = (0 * 1000000) // dpr
        norm_y1 = (5 * 1000000) // dpr

        self.assertEqual((norm_x0, norm_x1), (12, 13))
        self.assertEqual((norm_y0, norm_y1), (0, 4))
        # Recovered overlap: (13 - 12) * (4 - 0) = 4
        self.assertEqual((norm_x1 - norm_x0) * (norm_y1 - norm_y0), 4)

        fused = fuse_regions(state, physical_region, dpr_ppm=dpr)

        # A5-D3: Independent floors of origin and width used to collapse width to zero
        # (rx = (16*1000000)//1250000 = 12, rw = (1*1000000)//1250000 = 0 -> rx1 = 12, overlap = 0).
        # Endpoint-floor fusion must recover the hit:
        self.assertEqual(
            fused["hits"],
            [{"region": 0, "node": "0000000000000004", "overlap": 4, "actionable": True}],
        )

    def test_negative_endpoint_residues_and_empty_or_collapsed_axes(self):
        state = {"nodes": [{"id": "n", "box": [-4, -4, 8, 8], "actionable": False}]}
        regions = [
            {"x": -4, "y": -4, "w": 1, "h": 1},  # CSS [-4, -4, 1, 1]
            {"x": 0, "y": 0, "w": 1, "h": 1},    # legitimate physical-span collapse
            {"x": 1, "y": 1, "w": 0, "h": 3},    # already-empty x axis
            {"x": 1, "y": 1, "w": 3, "h": 0},    # already-empty y axis
            {"x": 1, "y": 1, "w": 1, "h": 1},    # CSS [0, 0, 1, 1]
        ]
        self.assertEqual(fuse_regions(state, regions, 1250000), {
            "hits": [
                {"region": 0, "node": "n", "overlap": 1, "actionable": False},
                {"region": 4, "node": "n", "overlap": 1, "actionable": False},
            ],
            "unmatched_regions": [1, 2, 3],
        })


if __name__ == "__main__":
    unittest.main()
