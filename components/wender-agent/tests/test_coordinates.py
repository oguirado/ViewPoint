"""Unit tests for wender.act.coordinates module."""
import unittest

from wender.act import coordinates
from wender.perceive import ax
from wender.state import schema


class TestCoordinates(unittest.TestCase):
    def test_box_center_odd_sizes(self):
        # Even size: 10 + 20 // 2 = 20, 10 + 30 // 2 = 25
        self.assertEqual(coordinates.box_center([10, 10, 20, 30]), (20, 25))
        # Odd size: 10 + 15 // 2 = 17, 5 + 7 // 2 = 8 (floor division)
        self.assertEqual(coordinates.box_center([10, 5, 15, 7]), (17, 8))
        self.assertEqual(coordinates.box_center([0, 0, 1, 1]), (0, 0))

        with self.assertRaises(coordinates.CoordRefused):
            coordinates.box_center(None)
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.box_center([1, 2])

    def test_css_to_device(self):
        # dpr 1.5 -> 1500000 ppm
        # 100 CSS px * 1500000 // 1000000 = 150 device px
        self.assertEqual(coordinates.css_to_device(100, 200, 1500000), (150, 300))
        self.assertEqual(coordinates.css_to_device(0, 0, 1000000), (0, 0))
        self.assertEqual(coordinates.css_to_device(10, 10, 2000000), (20, 20))

    def _make_sample_state(self, boxes=None, viewport=None):
        raw_nodes = [
            {
                "nodeId": "1",
                "role": {"value": "RootWebArea"},
                "name": {"value": "root"},
                "childIds": ["2", "3"],
                "ignored": False,
            },
            {
                "nodeId": "2",
                "role": {"value": "button"},
                "name": {"value": "Click Me"},
                "childIds": [],
                "ignored": False,
            },
            {
                "nodeId": "3",
                "role": {"value": "textbox"},
                "name": {"value": "Input Field"},
                "childIds": [],
                "ignored": False,
            },
        ]
        pruned = ax.prune(raw_nodes)
        ax_sha = ax.snapshot_sha(pruned, with_state=True)
        ax_stable_sha = ax.snapshot_sha(pruned, with_state=False)

        prune_info = {"name": ax.PRUNE_NAME, "version": ax.PRUNE_VERSION}

        return schema.build_state(
            pruned,
            kind="cdp",
            url="https://example.com",
            title="Example Page",
            ax_sha256=ax_sha,
            ax_stable_sha256=ax_stable_sha,
            prune=prune_info,
            raw_count=3,
            boxes=boxes,
            viewport=viewport,
            actionable_roles=ax.ACTIONABLE_ROLES,
        )

    def test_plan_click_success(self):
        # Viewport 800x600, un-scrolled
        vp = {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 2000, "doc_h": 2000, "dpr_ppm": 1000000}
        st = self._make_sample_state(viewport=vp)
        btn_id = st["nodes"][1]["id"]
        st["nodes"][1]["box"] = [100, 100, 50, 20]  # center = (125, 110)

        events = coordinates.plan_click(st, btn_id)
        self.assertEqual(events, [
            {"type": "mouseMoved", "x": 125, "y": 110},
            {"type": "mousePressed", "button": "left", "clickCount": 1, "x": 125, "y": 110},
            {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": 125, "y": 110},
        ])

    def test_plan_click_viewport_edges_and_scroll(self):
        vp = {"w": 800, "h": 600, "scroll_x": 100, "scroll_y": 200, "doc_w": 2000, "doc_h": 2000, "dpr_ppm": 1000000}
        st = self._make_sample_state(viewport=vp)
        btn_id = st["nodes"][1]["id"]

        # Viewport bounds: x in [100, 900), y in [200, 800)
        # Center exactly at top-left edge: cx=100, cy=200 -> box=[100, 200, 1, 1] -> center=(100, 200) -> valid
        st["nodes"][1]["box"] = [100, 200, 1, 1]
        self.assertEqual(len(coordinates.plan_click(st, btn_id)), 3)

        # Center exactly at right edge: cx=900 -> outside
        st["nodes"][1]["box"] = [900, 200, 1, 1]
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id)

        # Center just inside right/bottom edge: cx=899, cy=799 -> box=[899, 799, 1, 1] -> valid
        st["nodes"][1]["box"] = [899, 799, 1, 1]
        self.assertEqual(len(coordinates.plan_click(st, btn_id)), 3)

        # Center exactly at bottom edge: cy=800 -> outside
        st["nodes"][1]["box"] = [899, 800, 1, 1]
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id)

    def test_plan_click_refusals(self):
        vp = {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 1000000}
        st = self._make_sample_state(viewport=vp)
        btn_id = st["nodes"][1]["id"]
        st["nodes"][1]["box"] = [100, 100, 50, 20]

        # Irreversible refusal
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id, irreversible=True)

        # Missing node refusal
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, "nonexistent_id")

        # Null box refusal
        st["nodes"][1]["box"] = None
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id)

        # Zero size box refusal (w=0 or h=0)
        st["nodes"][1]["box"] = [100, 100, 0, 20]
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id)

        st["nodes"][1]["box"] = [100, 100, 20, 0]
        with self.assertRaises(coordinates.CoordRefused):
            coordinates.plan_click(st, btn_id)

    def test_plan_type(self):
        res = coordinates.plan_type("hello world")
        self.assertEqual(res, [{"type": "insertText", "text": "hello world"}])

    def test_canonical_dumps(self):
        vp = {"w": 800, "h": 600, "scroll_x": 0, "scroll_y": 0, "doc_w": 800, "doc_h": 600, "dpr_ppm": 1000000}
        st = self._make_sample_state(viewport=vp)
        dumped = schema.dumps(st)
        self.assertIsInstance(dumped, str)
        self.assertTrue(dumped.startswith("{"))


if __name__ == "__main__":
    unittest.main()
