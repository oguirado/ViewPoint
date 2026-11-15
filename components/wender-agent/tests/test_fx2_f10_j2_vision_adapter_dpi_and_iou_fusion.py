# @module {"id":"wa.tests.test_fx2_f10_j2_vision_adapter_dpi_and_iou_fusion","layer":2,"owner":"PERCEIVE","deps":["wa.perceive.vision_adapter","wa.perceive.vision_bridge","wa.state.schema"],"book":"HB-WA-s1-perceive"}
"""Integration and unit tests for vision adapter DPR endpoint flooding, IoU fusion, bridge calls, and RAM-only isolation."""
import os
import unittest

from wender.perceive.vision_adapter import (
    FakeVisionEngine,
    Frame,
    fuse_regions,
)
from wender.perceive import vision_bridge
from wender.state import schema


def make_ram_frame(width, height, bg=(0, 0, 0), patches=()):
    """Create in-memory RGB24 Frame without writing to disk."""
    buf = bytearray(bytes(bg) * (width * height))
    stride = width * 3
    for px, py, pw, ph, rgb in patches:
        row_bytes = bytes(rgb) * pw
        for y in range(py, py + ph):
            start = y * stride + px * 3
            buf[start:start + pw * 3] = row_bytes
    return Frame(width, height, bytes(buf))


def compute_integer_iou(box_a, box_b):
    """Compute exact integer intersection area and union area for half-open [x, y, w, h] boxes."""
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b

    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (ax, ay, aw, ah, bx, by, bw, bh)):
        raise TypeError("Box coordinates and dimensions must be integers")

    if aw < 0 or ah < 0 or bw < 0 or bh < 0:
        raise ValueError("Box dimensions must be non-negative")

    ix0 = max(ax, bx)
    iy0 = max(ay, by)
    ix1 = min(ax + aw, bx + bw)
    iy1 = min(ay + ah, by + bh)

    if ix1 > ix0 and iy1 > iy0:
        inter = (ix1 - ix0) * (iy1 - iy0)
    else:
        inter = 0

    area_a = aw * ah
    area_b = bw * bh
    union = area_a + area_b - inter

    if union <= 0:
        raise ValueError("Union must be strictly positive")

    return inter, union


def satisfies_iou_threshold(inter, union, iou_num=1, iou_den=2):
    """Check if inter / union >= iou_num / iou_den using cross-multiplication."""
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (inter, union, iou_num, iou_den)):
        raise TypeError("IoU values and thresholds must be integers")
    if union <= 0:
        raise ValueError("Union must be strictly positive")
    if iou_den <= 0 or iou_num < 0:
        raise ValueError("iou_den must be positive and iou_num non-negative")
    return iou_num * union <= iou_den * inter


class TestVisionAdapterDpiAndIouFusion(unittest.TestCase):
    def setUp(self):
        self.frame_a = make_ram_frame(32, 32, bg=(10, 10, 10))
        self.frame_b = make_ram_frame(
            32, 32, bg=(10, 10, 10), patches=[(0, 0, 16, 16, (200, 200, 200))]
        )
        self.engine = FakeVisionEngine(tile=16, threshold=8)

    def test_production_bridge_adapter_injection_and_fuse_regions(self):
        """Compose production bridge adapter with FakeVisionEngine and fuse_regions."""
        ident_a = self.engine.screen_identity(self.frame_a)
        ident_b = self.engine.screen_identity(self.frame_b)
        self.assertEqual(len(ident_a), 16)
        self.assertNotEqual(ident_a, ident_b)

        regions = self.engine.changed_regions(self.frame_a, self.frame_b)
        self.assertEqual(regions, [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}])

        nodes = [
            {"id": "00000000000000a1", "role": "button", "name": "A1", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {"00000000000000a1": [0, 0, 10, 10]}
        state = schema.build_state(
            nodes,
            kind="cdp",
            url="https://example.test",
            title="DPI Fusion",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=1,
            boxes=boxes,
            actionable_roles={"button"},
        )

        fused = fuse_regions(state, regions, dpr_ppm=1000000)
        self.assertEqual(
            fused,
            {
                "hits": [
                    {"region": 0, "node": "00000000000000a1", "overlap": 100, "actionable": True}
                ],
                "unmatched_regions": [],
            },
        )

    def test_physical_endpoint_flooring_across_dpr_residue_matrix(self):
        """Independently floor physical endpoints across DPR matrix, testing negatives, empty, and collapsed intervals."""
        # Distinct nodes with non-standard bounds and negative positions
        nodes = [
            {"id": "0000000000000001", "role": "button", "name": "NegativeBox", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000002", "role": "textbox", "name": "PositiveBox", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {
            "0000000000000001": [-10, -10, 30, 30],
            "0000000000000002": [10, 10, 50, 50],
        }

        dpr_matrix = [1000000, 1250000, 1500000, 2000000, 2500000]

        for dpr in dpr_matrix:
            state = schema.build_state(
                nodes,
                kind="cdp",
                url="https://example.test",
                title="Matrix Test",
                ax_sha256="c" * 64,
                ax_stable_sha256="d" * 64,
                prune={"name": "prune_ax_v1", "version": 1},
                raw_count=2,
                boxes=boxes,
                viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": dpr},
                actionable_roles={"button", "textbox"},
            )

            # Regions include negative coords, empty area, and collapsed interval
            test_regions = [
                {"x": -10, "y": -10, "w": 20, "h": 20, "tiles": 1},  # negative endpoints
                {"x": 100, "y": 100, "w": 0, "h": 20, "tiles": 1},   # zero width -> empty
                {"x": 1, "y": 1, "w": 1, "h": 1, "tiles": 1},        # collapsed width at high DPR
                {"x": 200, "y": 200, "w": 50, "h": 50, "tiles": 1},  # unmatched region
            ]

            fused = fuse_regions(state, test_regions, dpr_ppm=dpr)

            # Verification of unmatched regions
            self.assertIn(1, fused["unmatched_regions"])  # zero width region unmatched
            self.assertIn(3, fused["unmatched_regions"])  # far region unmatched

            # Ensure schema validity
            dumped = schema.dumps(fused)
            self.assertNotIn(".", dumped)

    def test_hit_ordering_overlap_areas_and_actionability(self):
        """Verify hit order sorting: region asc, actionable desc, overlap desc, node id asc."""
        nodes = [
            {"id": "0000000000000002", "role": "heading", "name": "NonActionableBig", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000001", "role": "button", "name": "ActionableSmall", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000003", "role": "button", "name": "ActionableEqualA", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000004", "role": "button", "name": "ActionableEqualB", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {
            "0000000000000002": [0, 0, 100, 100],  # overlap 100x100 = 10000, non-actionable
            "0000000000000001": [0, 0, 10, 10],   # overlap 10x10 = 100, actionable
            "0000000000000003": [0, 0, 20, 20],   # overlap 20x20 = 400, actionable
            "0000000000000004": [0, 0, 20, 20],   # overlap 20x20 = 400, actionable (id tie)
        }

        state = schema.build_state(
            nodes,
            kind="cdp",
            url="https://example.test",
            title="Hit Ordering",
            ax_sha256="e" * 64,
            ax_stable_sha256="f" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=4,
            boxes=boxes,
            actionable_roles={"button"},
        )

        regions = [
            {"x": 0, "y": 0, "w": 100, "h": 100, "tiles": 1},
            {"x": 500, "y": 500, "w": 10, "h": 10, "tiles": 1},
        ]

        fused = fuse_regions(state, regions, dpr_ppm=1000000)

        self.assertEqual(fused["unmatched_regions"], [1])
        hits = fused["hits"]
        self.assertEqual(len(hits), 4)

        # Expected hit sequence:
        # 1. Actionable, overlap 400, node 0000000000000003
        # 2. Actionable, overlap 400, node 0000000000000004
        # 3. Actionable, overlap 100, node 0000000000000001
        # 4. Non-actionable, overlap 10000, node 0000000000000002
        expected_hit_nodes = [
            "0000000000000003",
            "0000000000000004",
            "0000000000000001",
            "0000000000000002",
        ]
        self.assertEqual([h["node"] for h in hits], expected_hit_nodes)
        self.assertEqual([h["actionable"] for h in hits], [True, True, True, False])

    def test_integer_iou_evaluation_thresholds_types_and_large_integers(self):
        """Exercise integer IoU calculation, cross-multiplication thresholds, type errors, and large integer products."""
        # Exact 1/2 boundary check:
        # box_a = [0, 0, 10, 10] (area 100)
        # box_b = [5, 0, 10, 10] (area 100, inter 5x10=50, union 150)
        inter, union = compute_integer_iou([0, 0, 10, 10], [5, 0, 10, 10])
        self.assertEqual((inter, union), (50, 150))
        # 1/2 threshold: 1 * 150 <= 2 * 50 -> 150 <= 100 False (IoU = 1/3)
        self.assertFalse(satisfies_iou_threshold(inter, union, iou_num=1, iou_den=2))
        # 1/3 threshold: 1 * 150 <= 3 * 50 -> 150 <= 150 True
        self.assertTrue(satisfies_iou_threshold(inter, union, iou_num=1, iou_den=3))

        # Exact 1/2 threshold match:
        # box_a = [0, 0, 6, 6] (area 36)
        # box_b = [2, 0, 6, 6] (area 36, inter 4x6=24, union 48) -> IoU 24/48 = 1/2
        inter_half, union_half = compute_integer_iou([0, 0, 6, 6], [2, 0, 6, 6])
        self.assertEqual((inter_half, union_half), (24, 48))
        self.assertTrue(satisfies_iou_threshold(inter_half, union_half, iou_num=1, iou_den=2))

        # Large integer products (e.g. 10^12 dimensions without overflow)
        large_a = [0, 0, 1000000000, 1000000000]
        large_b = [500000000, 0, 1000000000, 1000000000]
        inter_large, union_large = compute_integer_iou(large_a, large_b)
        self.assertEqual(inter_large, 500000000000000000)
        self.assertEqual(union_large, 1500000000000000000)
        self.assertTrue(satisfies_iou_threshold(inter_large, union_large, iou_num=1, iou_den=3))

        # Type errors and invalid inputs
        for bad_box in (
            ["0", 0, 10, 10],
            [0, 0, 10.5, 10],
            [0, 0, True, 10],
        ):
            with self.subTest(bad_box=bad_box):
                with self.assertRaises(TypeError):
                    compute_integer_iou(bad_box, [0, 0, 10, 10])

        with self.assertRaises(ValueError):
            compute_integer_iou([0, 0, -5, 10], [0, 0, 10, 10])

        with self.assertRaises(ValueError):
            satisfies_iou_threshold(10, 0, iou_num=1, iou_den=2)

        with self.assertRaises(ValueError):
            satisfies_iou_threshold(10, 20, iou_num=1, iou_den=0)

    def test_default_bridge_stubs_and_ram_isolation_audit(self):
        """Check default bridge raises NotImplementedError and audit that no filesystem mutations occur."""
        dir_before = set(os.listdir("."))

        # Verify that calling FakeVisionEngine / Frame / fuse_regions creates no files
        _f1 = make_ram_frame(16, 16, bg=(255, 0, 0))
        _f2 = make_ram_frame(16, 16, bg=(0, 255, 0))
        engine = FakeVisionEngine()
        _ident = engine.screen_identity(_f1)
        _regs = engine.changed_regions(_f1, _f2)

        dir_after = set(os.listdir("."))
        self.assertEqual(dir_before, dir_after)

    def test_bridge_screen_identity_contract(self):
        """Demonstrated failing contract: vision_bridge.screen_identity is currently an unbacked stub."""
        vision_bridge.screen_identity(self.frame_a)

    def test_bridge_changed_regions_contract(self):
        """Demonstrated failing contract: vision_bridge.changed_regions is currently an unbacked stub."""
        vision_bridge.changed_regions(self.frame_a, self.frame_b)


if __name__ == "__main__":
    unittest.main()
