"""Targeted A5 malformed-adapter and zero-area-fusion boundary tests for vision_adapter."""
import unittest

from wender.perceive.vision_adapter import (
    FakeVisionEngine,
    Frame,
    fuse_regions,
)
from wender.state import schema


def make_frame(width, height, bg=(0, 0, 0), patches=()):
    """Build a tiny synthetic RGB24 Frame in RAM with rectangular `(x, y, w, h, rgb)` patches."""
    buf = bytearray(bytes(bg) * (width * height))
    stride = width * 3
    for px, py, pw, ph, rgb in patches:
        row_bytes = bytes(rgb) * pw
        for y in range(py, py + ph):
            start = y * stride + px * 3
            buf[start:start + pw * 3] = row_bytes
    return Frame(width, height, bytes(buf))


def make_valid_state(nodes_data):
    """Helper to construct a minimal schema-valid state."""
    pruned_nodes = []
    boxes = {}
    for item in nodes_data:
        nid = item["id"]
        pruned_nodes.append({
            "id": nid,
            "role": item.get("role", "button"),
            "name": item.get("name", "Btn"),
            "depth": 0,
            "ordinal": 0,
            "attrs": (),
        })
        if "box" in item:
            boxes[nid] = item["box"]

    return schema.build_state(
        pruned_nodes,
        kind="cdp",
        url="https://example.test",
        title="Test",
        ax_sha256="a" * 64,
        ax_stable_sha256="b" * 64,
        prune={"name": "prune_ax_v1", "version": 1},
        raw_count=len(nodes_data),
        boxes=boxes,
        viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 1000000},
        actionable_roles={"button"},
    )


class TestA5AdapterValidationAndBoundaryFusion(unittest.TestCase):
    def test_fake_vision_engine_param_validation(self):
        # Invalid tile values: zero, negative, bool
        for bad_tile in (0, -4, True):
            with self.subTest(tile=bad_tile):
                with self.assertRaises(ValueError):
                    FakeVisionEngine(tile=bad_tile)

        # Invalid threshold values: negative, bool
        for bad_thresh in (-1, False):
            with self.subTest(threshold=bad_thresh):
                with self.assertRaises(ValueError):
                    FakeVisionEngine(threshold=bad_thresh)

    def test_fake_vision_engine_non_frame_inputs(self):
        engine = FakeVisionEngine(tile=16, threshold=8)
        valid_frame = make_frame(16, 16)

        # Non-Frame for screen_identity
        for bad_input in ("not a frame", None, 123, [valid_frame]):
            with self.subTest(screen_identity_input=bad_input):
                with self.assertRaises(ValueError):
                    engine.screen_identity(bad_input)

        # Non-Frame for changed_regions (prev or cur, including None)
        for bad_prev, bad_cur in (
            (None, valid_frame),
            (valid_frame, None),
            ("not a frame", valid_frame),
            (valid_frame, "not a frame"),
            (None, None),
        ):
            with self.subTest(prev=bad_prev, cur=bad_cur):
                with self.assertRaises(ValueError):
                    engine.changed_regions(bad_prev, bad_cur)

    def test_threshold_zero_guard_total_diff(self):
        engine = FakeVisionEngine(tile=16, threshold=0)
        f_base = make_frame(16, 16, bg=(10, 20, 30))
        f_identical = make_frame(16, 16, bg=(10, 20, 30))
        f_diff = make_frame(16, 16, bg=(10, 20, 30), patches=[(0, 0, 16, 16, (10, 20, 31))])

        # Identical in-memory frames must produce no changed region
        self.assertEqual(engine.changed_regions(f_base, f_identical), [])

        # Nonzero RGB difference must change (verifying total_diff > 0 guard)
        regions = engine.changed_regions(f_base, f_diff)
        self.assertEqual(regions, [{"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1}])

    def test_fuse_regions_validation_attacks(self):
        valid_state = make_valid_state([{"id": "0000000000000001", "box": [0, 0, 10, 10]}])
        valid_regions = [{"x": 0, "y": 0, "w": 5, "h": 5, "tiles": 1}]

        # dpr_ppm zero and True
        for bad_dpr in (0, True, -1000000, 1.5):
            with self.subTest(dpr_ppm=bad_dpr):
                with self.assertRaises(ValueError):
                    fuse_regions(valid_state, valid_regions, dpr_ppm=bad_dpr)

        # State without nodes
        for bad_state in ({}, {"nodes": None}, {"schema": "wender.state/v1"}, "not a dict", None):
            with self.subTest(state=bad_state):
                with self.assertRaises(ValueError):
                    fuse_regions(bad_state, valid_regions)

        # Schema-keyed invalid state -> raises schema.StateError
        schema_invalid_state = {
            "schema": "wender.state/v1",
            "nodes": [],
            # Missing required top-level keys
        }
        with self.assertRaises(schema.StateError):
            fuse_regions(schema_invalid_state, valid_regions)

        # Non-dict region
        for bad_region in (None, 123, "dict", [1, 2, 3]):
            with self.subTest(region=bad_region):
                with self.assertRaises(ValueError):
                    fuse_regions(valid_state, [bad_region])

        # Bool or float coordinates
        bad_coords_regions = [
            {"x": True, "y": 0, "w": 10, "h": 10},
            {"x": 0, "y": False, "w": 10, "h": 10},
            {"x": 0, "y": 0, "w": True, "h": 10},
            {"x": 0, "y": 0, "w": 10, "h": False},
            {"x": 1.5, "y": 0, "w": 10, "h": 10},
            {"x": 0, "y": 2.0, "w": 10, "h": 10},
            {"x": 0, "y": 0, "w": 3.14, "h": 10},
            {"x": 0, "y": 0, "w": 10, "h": 0.5},
        ]
        for reg in bad_coords_regions:
            with self.subTest(bad_coord_region=reg):
                with self.assertRaises(ValueError):
                    fuse_regions(valid_state, [reg])

        # Negative width or height
        for bad_dim_region in (
            {"x": 0, "y": 0, "w": -1, "h": 10},
            {"x": 0, "y": 0, "w": 10, "h": -5},
            {"x": 0, "y": 0, "w": -1, "h": -1},
        ):
            with self.subTest(negative_dim_region=bad_dim_region):
                with self.assertRaises(ValueError):
                    fuse_regions(valid_state, [bad_dim_region])

    def test_zero_area_boundary_fusion_and_touching_nodes(self):
        # Node at x=10, y=0, w=10, h=10
        state = make_valid_state([{"id": "0000000000000001", "box": [10, 0, 10, 10]}])

        # 1. Region x=0, y=0, w=10, h=10 touches node at x=10 -> no hits, unmatched region 0
        touching_region = [{"x": 0, "y": 0, "w": 10, "h": 10}]
        res_touching = fuse_regions(state, touching_region)
        self.assertEqual(res_touching, {"hits": [], "unmatched_regions": [0]})

        # 2. One-pixel inward shift (x=1, y=0, w=10, h=10) overlaps node x=10..20 on x=10..11 -> positive overlap
        shifted_region = [{"x": 1, "y": 0, "w": 10, "h": 10}]
        res_shifted = fuse_regions(state, shifted_region)
        self.assertEqual(
            res_shifted,
            {
                "hits": [{
                    "region": 0,
                    "node": "0000000000000001",
                    "overlap": 10,  # 1 (width in 10..11) * 10 (height in 0..10)
                    "actionable": True,
                }],
                "unmatched_regions": [],
            },
        )

        # 3. Zero-width region (w=0) -> no hits, unmatched region 0
        zero_w_regions = [
            [{"x": 0, "y": 0, "w": 0, "h": 10}],
            [{"x": 10, "y": 0, "w": 0, "h": 10}],
            [{"x": 12, "y": 0, "w": 0, "h": 5}],
        ]
        for reg in zero_w_regions:
            with self.subTest(zero_width_region=reg):
                res = fuse_regions(state, reg)
                self.assertEqual(res, {"hits": [], "unmatched_regions": [0]})

        # 4. Zero-height region (h=0) -> no hits, unmatched region 0
        zero_h_regions = [
            [{"x": 0, "y": 0, "w": 10, "h": 0}],
            [{"x": 10, "y": 0, "w": 10, "h": 0}],
            [{"x": 12, "y": 0, "w": 5, "h": 0}],
        ]
        for reg in zero_h_regions:
            with self.subTest(zero_height_region=reg):
                res = fuse_regions(state, reg)
                self.assertEqual(res, {"hits": [], "unmatched_regions": [0]})


if __name__ == "__main__":
    unittest.main()
