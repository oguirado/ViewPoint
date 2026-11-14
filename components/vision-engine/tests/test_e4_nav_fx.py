"""FX W5 r1 JW5-6: task routing for compaction and GUI frames (synthetic catalogue; no dependence on the live one)."""
import json
import os
import tempfile
import unittest

from tools import route_code

ROWS = [
    {"anchor": "vm.compact.compact", "doc": "-> dict(active, archive, retired, summaries, live_blobs, dropped_blobs, fg_ids_sha256).",
     "line": 57, "path": "vmem/compact.py", "symbol": "compact"},
    {"anchor": "va.gui.frames", "doc": "Collect screen image files into the frame manifest, keeping nested relative paths.",
     "line": 141, "path": "vadapt/gui/from_screens.py", "symbol": "GuiAdapter.frames"},
    {"anchor": "vc.s4.regions", "doc": "Connected dirty regions of a tile grid.", "line": 10, "path": "visioncore/regions.py",
     "symbol": "regions_of"},
]


class RouteFx(unittest.TestCase):
    def _route(self, q):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "catalogue"))
            with open(os.path.join(d, "catalogue", "symbols.jsonl"), "w") as f:
                for r in ROWS:
                    f.write(json.dumps(r) + "\n")
            return route_code.route(q, 3, d)

    def test_compaction_foreground_routes_to_compact(self):
        r = self._route("compaction preserving foreground")
        self.assertFalse(r["abstain"]["flag"], r)
        self.assertEqual(r["r"][0][:3], ["vm.compact.compact", "vmem/compact.py", 57])

    def test_gui_screenshot_collection_routes_to_frames(self):
        r = self._route("screenshot manifest builder")
        self.assertFalse(r["abstain"]["flag"], r)
        self.assertEqual(r["r"][0][:3], ["va.gui.frames", "vadapt/gui/from_screens.py", 141])

    def test_unrelated_queries_still_abstain(self):
        for q in ("quantum banana pudding recipe", "foreground banana pudding", "screenshot banana quantum pudding"):
            self.assertTrue(self._route(q)["abstain"]["flag"], q)


if __name__ == "__main__":
    unittest.main()
