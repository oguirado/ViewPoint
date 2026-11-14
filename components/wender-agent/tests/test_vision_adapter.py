"""Unit tests for wender.perceive.vision_adapter (RAM-only synthetic frames, stdlib only)."""
from dataclasses import FrozenInstanceError
import re
import unittest

from wender.perceive.vision_adapter import (
    FakeVisionEngine,
    Frame,
    VisionEngineAdapter,
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


class TestVisionAdapter(unittest.TestCase):
    def test_frame_validation_and_immutability(self):
        frame = make_frame(4, 3)
        self.assertEqual((frame.width, frame.height, len(frame.data)), (4, 3, 36))
        with self.assertRaises(FrozenInstanceError):
            frame.width = 5
        for w, h, data in (
            (0, 4, b""),
            (-1, 4, b""),
            (4, 0, b""),
            (4, -2, b""),
            (True, 1, b"\x00\x00\x00"),
            (1, True, b"\x00\x00\x00"),
            (2, 2, b"\x00" * 11),
            (2, 2, b"\x00" * 13),
            (1, 1, bytearray(3)),
            (1, 1, "abc"),
        ):
            with self.subTest(w=w, h=h, data_type=type(data).__name__):
                with self.assertRaises(ValueError):
                    Frame(w, h, data)

    def test_abstract_base_contract(self):
        with self.assertRaises(TypeError):
            VisionEngineAdapter()

        class DummyAdapter(VisionEngineAdapter):
            def screen_identity(self, frame):
                return super().screen_identity(frame)

            def changed_regions(self, prev, cur):
                return super().changed_regions(prev, cur)

        dummy = DummyAdapter()
        f = make_frame(4, 4)
        with self.assertRaises(NotImplementedError):
            dummy.screen_identity(f)
        with self.assertRaises(NotImplementedError):
            dummy.changed_regions(f, f)

    def test_screen_identity_equal_and_changed_tile(self):
        engine = FakeVisionEngine(tile=16, threshold=8)
        f1 = make_frame(32, 32, bg=(10, 20, 30))
        f2 = make_frame(32, 32, bg=(10, 20, 30))
        f3 = make_frame(32, 32, bg=(10, 20, 30), patches=[(16, 0, 16, 16, (50, 20, 30))])
        f_diff_size = make_frame(16, 64, bg=(10, 20, 30))

        id1 = engine.screen_identity(f1)
        id2 = engine.screen_identity(f2)
        id3 = engine.screen_identity(f3)
        id_size = engine.screen_identity(f_diff_size)

        self.assertRegex(id1, re.compile(r"^[0-9a-f]{16}$"))
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertNotEqual(id1, id_size)

    def test_single_changed_pixel_block_yields_one_region(self):
        engine = FakeVisionEngine(tile=16, threshold=8)
        prev = make_frame(48, 48, bg=(0, 0, 0))
        cur = make_frame(48, 48, bg=(0, 0, 0), patches=[(16, 32, 16, 16, (8, 8, 8))])
        regions = engine.changed_regions(prev, cur)
        self.assertEqual(regions, [{"x": 16, "y": 32, "w": 16, "h": 16, "tiles": 1}])

        # Below threshold (mean diff 7 < 8) yields no region.
        sub_threshold = make_frame(48, 48, bg=(0, 0, 0), patches=[(16, 32, 16, 16, (7, 7, 7))])
        self.assertEqual(engine.changed_regions(prev, sub_threshold), [])

    def test_two_far_blocks_and_four_connectivity_ordering(self):
        engine = FakeVisionEngine(tile=16, threshold=8)
        prev = make_frame(64, 64, bg=(0, 0, 0))
        # Two far blocks: one at (32, 0) and one at (0, 32), plus a 2-tile merged block at (32, 32)..(48, 32)
        cur = make_frame(
            64,
            64,
            bg=(0, 0, 0),
            patches=[
                (0, 32, 16, 16, (20, 20, 20)),
                (32, 0, 16, 16, (20, 20, 20)),
            ],
        )
        regions = engine.changed_regions(prev, cur)
        self.assertEqual(
            regions,
            [
                {"x": 32, "y": 0, "w": 16, "h": 16, "tiles": 1},
                {"x": 0, "y": 32, "w": 16, "h": 16, "tiles": 1},
            ],
        )

        # 4-connectivity merges edge-adjacent tiles, not diagonal-only tiles.
        diag_and_adj = make_frame(
            64,
            64,
            bg=(0, 0, 0),
            patches=[
                (0, 0, 16, 16, (30, 30, 30)),
                (16, 16, 32, 16, (30, 30, 30)),
            ],
        )
        self.assertEqual(
            engine.changed_regions(prev, diag_and_adj),
            [
                {"x": 0, "y": 0, "w": 16, "h": 16, "tiles": 1},
                {"x": 16, "y": 16, "w": 32, "h": 16, "tiles": 2},
            ],
        )

    def test_partial_edge_tiles_and_mismatched_dimensions(self):
        engine = FakeVisionEngine(tile=16, threshold=8)
        # 20x18 frame has a 4x16 right-edge tile, 16x2 bottom-edge tile, and 4x2 bottom-right corner tile.
        prev = make_frame(20, 18, bg=(0, 0, 0))
        cur = make_frame(20, 18, bg=(0, 0, 0), patches=[(16, 16, 4, 2, (10, 10, 10))])
        regions = engine.changed_regions(prev, cur)
        self.assertEqual(regions, [{"x": 16, "y": 16, "w": 4, "h": 2, "tiles": 1}])

        # Merging a full interior tile (0, 16 -> 16x2) with the corner tile (16, 16 -> 4x2) clips to width 20, height 18.
        cur_merged = make_frame(20, 18, bg=(0, 0, 0), patches=[(0, 16, 20, 2, (12, 12, 12))])
        self.assertEqual(
            engine.changed_regions(prev, cur_merged),
            [{"x": 0, "y": 16, "w": 20, "h": 2, "tiles": 2}],
        )

        with self.assertRaises(ValueError):
            engine.changed_regions(prev, make_frame(20, 19))
        with self.assertRaises(ValueError):
            engine.changed_regions(prev, make_frame(21, 18))

    def test_fuse_regions_with_built_state_and_dpr_scaling(self):
        pruned_nodes = [
            {"id": "0000000000000001", "role": "heading", "name": "Title", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000002", "role": "button", "name": "Save", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000003", "role": "button", "name": "Cancel", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000004", "role": "button", "name": "Apply", "depth": 0, "ordinal": 0, "attrs": ()},
            {"id": "0000000000000005", "role": "dialog", "name": "Modal", "depth": 0, "ordinal": 0, "attrs": ()},
        ]
        boxes = {
            "0000000000000001": [10, 10, 30, 20],  # non-actionable, larger overlap (30x20=600)
            "0000000000000002": [10, 10, 10, 10],  # actionable, overlap 100
            "0000000000000003": [10, 10, 20, 10],  # actionable, overlap 200
            "0000000000000004": [10, 10, 20, 10],  # actionable, overlap 200 (tie-broken by node id)
            # node 5 has no box (null) -> skipped
        }
        state = schema.build_state(
            pruned_nodes,
            kind="cdp",
            url="https://example.test",
            title="Test",
            ax_sha256="a" * 64,
            ax_stable_sha256="b" * 64,
            prune={"name": "prune_ax_v1", "version": 1},
            raw_count=5,
            boxes=boxes,
            viewport={"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 2000000},
            actionable_roles={"button"},
        )

        # Device-pixel regions at dpr_ppm=2000000 (2x DPR):
        # Region 0: device [20, 20, 60, 40] -> CSS [10, 10, 30, 20]
        # Region 1: device [200, 200, 32, 32] -> CSS [100, 100, 16, 16] (unmatched)
        regions = [
            {"x": 20, "y": 20, "w": 60, "h": 40, "tiles": 4},
            {"x": 200, "y": 200, "w": 32, "h": 32, "tiles": 4},
        ]
        fused = fuse_regions(state, regions, dpr_ppm=2000000)
        self.assertEqual(
            fused,
            {
                "hits": [
                    {"region": 0, "node": "0000000000000003", "overlap": 200, "actionable": True},
                    {"region": 0, "node": "0000000000000004", "overlap": 200, "actionable": True},
                    {"region": 0, "node": "0000000000000002", "overlap": 100, "actionable": True},
                    {"region": 0, "node": "0000000000000001", "overlap": 600, "actionable": False},
                ],
                "unmatched_regions": [1],
            },
        )
        dumped = schema.dumps(fused)
        self.assertTrue(dumped.startswith('{"hits":['))
        self.assertNotIn(".", dumped)


if __name__ == "__main__":
    unittest.main()
